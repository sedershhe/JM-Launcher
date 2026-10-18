using System.ComponentModel;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Data;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using System.Windows.Media.Media3D;
using JML.Bridge;
using JML.Bridge.Dto;
using JML.Infrastructure;

namespace JML.Pages;

/// <summary>
/// 插件结构化页面宿主（技术方案第三节方案 A）：按 <c>template</c> 渲染
/// list / browser / action 三种模板，**不含任何业务逻辑**。
///
/// 数据纪律：
/// <list type="bullet">
/// <item>模板类型、标题来自 Python（manifest 决定），未知模板只显示错误；</item>
/// <item>列定义 / 行数据 / 文件清单 / 状态文案 / 动作按钮 / 失败判断全部来自 Python；</item>
/// <item>图片走 <c>plugin_page_image</c> 取原始 bytes，<see cref="BitmapImage"/>
/// 只在 UI 线程创建（旧缺陷 #7）；</item>
/// <item>失败只显示 <c>[error_code] error_message</c>，不弹全局框、不抛异常。</item>
/// </list>
///
/// 顶部「← 返回 + 页面名」由 MainWindow 的内页标题栏负责，本页只管内容区。
/// </summary>
public partial class PluginPageHost : UserControl
{
    private readonly IPluginPageGateway _gateway;
    private readonly List<PluginPageFileItemDto> _fileItems = [];
    private readonly List<PluginPageFileItemDto> _actionItems = [];

    private bool _loadedOnce;
    private string _template = "";
    private string _title = "";

    // list 模板分页状态
    private int _listPage = 1;
    private int _listTotalPages = 1;

    // list 模板筛选下拉（阶段 4-4 协议扩展：data.filters）
    private readonly Dictionary<string, string> _listFilterValues = [];
    private readonly Dictionary<string, List<PluginPageFilterOptionDto>> _listFilterOptions = [];
    private bool _rebuildingListFilters;

    // list 模板行双击动作（阶段 4-4 协议扩展：data.row_double_click_action）
    private string _rowDoubleClickAction = string.Empty;

    // list 模板封面列（阶段 4-4 协议扩展：行内 thumb_book_id）
    // 只在行容器被虚拟化创建（= 当前可见/已加载）时取图；同一 book_id 全局去重缓存。
    private DataGridTemplateColumn? _coverColumn;
    private readonly Dictionary<string, ImageSource> _coverCache = new(StringComparer.Ordinal);
    private readonly HashSet<string> _coverFailed = new(StringComparer.Ordinal);
    private readonly Dictionary<string, Task<byte[]?>> _coverRequests = new(StringComparer.Ordinal);

    // browser 模板状态
    private int _filePage = 1;
    private int _fileTotalPages = 1;
    private int _itemIndex = -1;
    private int _imageIndex;
    private int _imageCount;
    private bool _viewingImage;
    private bool _goingBackToList;

    // browser / action 文件夹选择（阶段 4-2 / 4-3 协议小扩展：allow_folder_choice / folder）
    private string _folder = string.Empty;

    // action 模板：当前可打开的网址（空串 = 不显示「在浏览器中打开」按钮）
    private string _actionUrl = string.Empty;

    // 最近一次「动作成功后自动唤起浏览器」的网址：同一 url 成功打开后不再重复，
    // 失败（或页面已不再指向该 url）时清空，允许用户重试。
    private string _autoOpenedUrl = string.Empty;

    // 动作是否正在执行：同一时刻只允许一个动作（防连点/双击把刚起的服务换掉端口，
    // 让界面上刚显示的地址变成死地址）。
    private bool _actionInFlight;

    // 单级注入打开器（BrowserOpener 的后备字段；设置时优先于三级链）。
    private Func<string, bool>? _browserOpener;

    /// <summary>本页所属插件 id（由 MainWindow 传入）。</summary>
    public string PluginId { get; }

    /// <summary>本页的 manifest 页面 id（由 MainWindow 传入）。</summary>
    public string PageId { get; }

    /// <summary>页面标题（优先取 Python 返回的 manifest 标题）。</summary>
    public string PageTitle { get; private set; }

    /// <summary>
    /// 文件夹选择器（生产用 <see cref="Microsoft.Win32.OpenFolderDialog"/>；
    /// STA 测试注入假实现，避免真的弹系统对话框）。返回 null / 空串 = 用户取消。
    /// </summary>
    internal Func<string?>? FolderPicker { get; set; }

    /// <summary>
    /// 外部浏览器打开器（生产用 <see cref="Process.Start(ProcessStartInfo)"/> 转发；
    /// STA 测试注入假实现，避免真的唤起系统浏览器）。返回是否已转发。
    ///
    /// 设置后只走这一级（旧测试依赖“失败即停”，注入的假实现不会触发真实的
    /// cmd / rundll32 兜底）；要完整验证三级链请注入 <see cref="BrowserOpeners"/>。
    /// </summary>
    internal Func<string, bool>? BrowserOpener
    {
        get => _browserOpener;
        set
        {
            _browserOpener = value;
            if (value is not null)
            {
                BrowserOpeners = null;
            }
        }
    }

    /// <summary>
    /// 可注入的三级浏览器打开器链。生产默认：
    /// ① <c>Process.Start(UseShellExecute=true)</c> → ② <c>cmd.exe /c start</c> →
    /// ③ <c>rundll32 url.dll,FileProtocolHandler</c>；测试注入假实现断言顺序。
    /// </summary>
    internal IReadOnlyList<BrowserOpenAttempt>? BrowserOpeners { get; set; }

    /// <summary>
    /// 剪贴板写入器（生产用 <see cref="Clipboard.SetText"/>；STA 测试注入假实现，
    /// 避免真的污染本机剪贴板）。
    /// </summary>
    internal Action<string>? ClipboardWriter { get; set; }

    /// <summary>
    /// 页面动作 <c>intent</c> 的转发器（阶段 4-4）：生产默认由所属
    /// <see cref="MainWindow"/> 实现（<c>Window.GetWindow(this)</c> 解析）；
    /// STA 测试注入假实现断言"只转发、不做业务"。
    /// </summary>
    internal IUiIntentHandler? IntentHandler { get; set; }

    /// <summary>
    /// 封面取图器（阶段 4-4 协议扩展）：生产默认 <see cref="SearchBridge.FetchCover"/>
    /// （内部已 <c>Task.Run</c>），STA 测试注入假实现，避免真的调用 Python。
    /// 只返回原始 bytes；<see cref="BitmapImage"/> 一律回 UI 线程再创建。
    /// </summary>
    internal Func<string, Task<byte[]?>>? CoverFetcher { get; set; }

    /// <summary>当前 list 模板是否渲染了封面列（行内带 thumb_book_id 时为 true）。</summary>
    internal bool HasCoverColumn => _coverColumn is not null;

    /// <summary>当前 list 模板的双击动作 id（空串 = 双击无动作）。</summary>
    internal string RowDoubleClickAction => _rowDoubleClickAction;

    /// <summary>当前 list 模板渲染出的筛选下拉（测试用；顺序与 Python 一致）。</summary>
    internal IReadOnlyList<ComboBox> ListFilterCombos =>
        PanelListFilters.Children.OfType<ComboBox>().ToList();

    /// <summary>当前已选文件夹（Python 的 <c>data.folder</c>，回传时带上）。</summary>
    internal string CurrentFolder => _folder;

    /// <summary>action 模板当前的 <c>data.url</c>（空串 = 无网址）。</summary>
    internal string CurrentActionUrl => _actionUrl;

    /// <summary>「选择文件夹…」按钮是否可见（由 Python 的 allow_folder_choice 决定）。</summary>
    internal bool IsFolderChoiceVisible =>
        BtnChooseFolder.Visibility == Visibility.Visible;

    /// <summary>action 模板的「选择文件夹…」按钮是否可见（同样由 Python 决定）。</summary>
    internal bool IsActionFolderChoiceVisible =>
        BtnActionChooseFolder.Visibility == Visibility.Visible;

    /// <summary>action 模板的「在浏览器中打开」按钮是否可见（只看 data.url）。</summary>
    internal bool IsOpenInBrowserVisible =>
        BtnActionOpenBrowser.Visibility == Visibility.Visible;

    /// <summary>action 模板的「复制链接」按钮是否可见（data.url 存在时始终可见）。</summary>
    internal bool IsCopyUrlVisible =>
        BtnActionCopyUrl.Visibility == Visibility.Visible;

    /// <summary>最近一次浏览器唤起链的逐次尝试回执（方法名 / 异常 / HResult / Message）。</summary>
    internal IReadOnlyList<BrowserOpenRecord> LastBrowserOpenRecords { get; private set; } = [];

    /// <summary>当前实际会使用的打开器链（生产应为三级；测试注入后按注入内容）。</summary>
    internal IReadOnlyList<BrowserOpenAttempt> CurrentOpenAttempts => ResolveOpenAttempts();

    /// <summary>设计器 / 默认为空页（未传 plugin_id/page_id 时不发任何调用）。</summary>
    public PluginPageHost()
        : this(string.Empty, string.Empty, string.Empty, null)
    {
    }

    /// <summary>按插件 + 页面构造宿主（生产入口）。</summary>
    public PluginPageHost(string pluginId, string pageId, string title)
        : this(pluginId, pageId, title, null)
    {
        // 生产不设置 BrowserOpener：ResolveOpenAttempts 会使用三级兜底链
        // （Process.Start → cmd start → rundll32）。STA 测试走内部构造函数并注入
        // fake，绝不真的唤起本机浏览器。
    }

    /// <summary>按插件 + 页面构造宿主；<paramref name="gateway"/> 仅测试注入假实现。</summary>
    internal PluginPageHost(
        string pluginId,
        string pageId,
        string title,
        IPluginPageGateway? gateway)
    {
        PluginId = pluginId ?? string.Empty;
        PageId = pageId ?? string.Empty;
        PageTitle = title ?? string.Empty;
        _gateway = gateway ?? PluginPageGateway.Instance;

        InitializeComponent();

        // 行容器被虚拟化创建时才知道“这一行当前可见/已加载”：封面取图在这里触发，
        // 不随 page.Rows 一次性发请求（大收藏夹不会瞬间打满网络）。
        GridRows.LoadingRow += OnListRowLoading;

        Loaded += async (_, _) =>
        {
            if (_loadedOnce)
            {
                return;
            }

            _loadedOnce = true;
            await LoadAsync(1);
        };
    }

    // ------------------------------------------------------------------
    // 载入 / 刷新
    // ------------------------------------------------------------------

    /// <summary>载入指定列表页（list/browser 共用 page 参数；action 忽略页码）。</summary>
    public async Task LoadAsync(int page)
    {
        if (string.IsNullOrWhiteSpace(PluginId) || string.IsNullOrWhiteSpace(PageId))
        {
            TextError.Text = "插件页面缺少 plugin_id / page_id，无法载入。";
            TextError.Visibility = Visibility.Visible;
            return;
        }

        try
        {
            int requested = page < 1 ? 1 : page;
            var parameters = new Dictionary<string, object?>
            {
                ["page"] = requested,
            };
            AddFolderParameter(parameters);
            AddListFilterParameters(parameters);
            ShowStatus("正在加载页面…");
            PluginPageLoadResultDto result =
                await _gateway.LoadAsync(PluginId, PageId, parameters);
            ApplyLoad(result);
            if (result.Ok)
            {
                ClearStatus();
            }

            if (result.Ok && string.Equals(result.Template, "browser", StringComparison.Ordinal)
                && _viewingImage)
            {
                await LoadImageAsync();
            }
        }
        catch (Exception ex)
        {
            // 网关已收口；这里兜底，保证页面不因宿主异常崩掉。
            ShowError("UNKNOWN_ERROR", LogSanitizer.Sanitize(ex.Message));
        }
    }

    /// <summary>
    /// 按一次 <c>plugin_page_load</c> 的结果重建界面（内部可见：STA 测试直接喂 DTO）。
    /// </summary>
    internal void ApplyLoad(PluginPageLoadResultDto? result)
    {
        if (result is null)
        {
            ShowError("UNKNOWN_ERROR", "插件页面返回为空");
            return;
        }

        if (!result.Ok)
        {
            ShowError(result.ErrorCode, result.ErrorMessage);
            return;
        }

        ClearError();
        _template = result.Template ?? string.Empty;
        if (!string.IsNullOrWhiteSpace(result.Title))
        {
            _title = result.Title;
            PageTitle = result.Title;
        }

        switch (_template)
        {
            case "list":
                RenderList(result.Data);
                break;
            case "browser":
                RenderBrowser(result.Data);
                break;
            case "action":
                RenderAction(result.Data);
                break;
            case "form":
                // 方案 A 的 form 模板由设置页 / 账号窗负责渲染，功能页不重复实现。
                ShowError(
                    "NOT_IMPLEMENTED",
                    "form 模板由设置与账号界面渲染，插件功能页暂不渲染该模板");
                break;
            default:
                ShowError("INVALID_DATA", $"插件返回了未知页面模板: {_template}");
                break;
        }
    }

    // ------------------------------------------------------------------
    // list 模板
    // ------------------------------------------------------------------

    private void RenderList(JsonElement data)
    {
        PluginListPageDto? page = PluginPageData.ToList(data);
        if (page is null)
        {
            ShowError("INVALID_DATA", "list 模板数据不是 JSON 对象");
            return;
        }

        ShowOnly(PanelList);

        _listPage = page.Page > 0 ? page.Page : 1;
        _listTotalPages = ResolveTotalPages(
            page.TotalPages, page.Total, page.PageSize, page.Rows.Count);
        TextListPage.Text = $"第 {_listPage} 页 / 共 {_listTotalPages} 页";
        BtnListPrev.IsEnabled = _listPage > 1;
        BtnListNext.IsEnabled = _listPage < _listTotalPages;

        // 双击动作 id 完全来自 Python；空串 = 本页双击不响应（旧插件行为不变）。
        _rowDoubleClickAction = (page.RowDoubleClickAction ?? string.Empty).Trim();

        BuildListColumns(page);
        BuildListFilters(page);

        bool empty = page.Rows.Count == 0;
        GridRows.ItemsSource = page.Rows
            .Select((row, index) => new PluginRowView(row, index))
            .ToList();
        TextListEmpty.Text = string.IsNullOrWhiteSpace(page.EmptyText)
            ? "暂无数据"
            : page.EmptyText;
        TextListEmpty.Visibility = empty ? Visibility.Visible : Visibility.Collapsed;
        GridRows.Visibility = empty ? Visibility.Collapsed : Visibility.Visible;
        PanelListPager.Visibility = empty ? Visibility.Collapsed : Visibility.Visible;
    }

    private void BuildListColumns(PluginListPageDto page)
    {
        GridRows.Columns.Clear();
        _coverColumn = null;

        // 阶段 4-4 协议扩展：只要有一行带 thumb_book_id 就补最左侧封面列
        // （40×56、圆角 3）。没有该字段的插件列结构完全不变。
        if (page.Rows.Any(row =>
                !string.IsNullOrEmpty(PluginPageRowFields.GetThumbBookId(row))))
        {
            _coverColumn = new DataGridTemplateColumn
            {
                Header = string.Empty,
                CellTemplate = (DataTemplate)FindResource("PluginRowCoverTemplate"),
                Width = new DataGridLength(56),
                CanUserResize = false,
                IsReadOnly = true,
            };
            GridRows.Columns.Add(_coverColumn);
        }

        foreach (PluginPageColumnDto column in page.Columns)
        {
            if (string.IsNullOrEmpty(column.Key))
            {
                continue;
            }

            var textColumn = new DataGridTextColumn
            {
                Header = string.IsNullOrWhiteSpace(column.Title) ? column.Key : column.Title,
                IsReadOnly = true,
                Binding = new Binding($"[{column.Key}]")
                {
                    Mode = BindingMode.OneWay,
                    Converter = JsonElementDisplayConverter.Instance,
                },
            };

            textColumn.Width = column.Width is > 0
                ? new DataGridLength(column.Width.Value)
                : DataGridLength.Auto;
            GridRows.Columns.Add(textColumn);
        }

        foreach (PluginPageRowActionDto action in page.RowActions)
        {
            AddRowActionColumn(action);
        }
    }

    /// <summary>
    /// list 模板可选筛选下拉（阶段 4-4）：选项、标签、当前值全部来自 Python
    /// 的 <c>data.filters</c>；C# 只把用户选中的 value 作为加载参数回传。
    /// 没有 filters 时整条筛选栏隐藏（旧插件行为不变）。
    /// </summary>
    private void BuildListFilters(PluginListPageDto page)
    {
        _rebuildingListFilters = true;
        try
        {
            PanelListFilters.Children.Clear();
            _listFilterValues.Clear();
            _listFilterOptions.Clear();

            int rendered = 0;
            foreach (PluginPageFilterDto filter in page.Filters)
            {
                if (string.IsNullOrWhiteSpace(filter.Key) || filter.Options.Count == 0)
                {
                    continue;
                }

                if (rendered > 0)
                {
                    PanelListFilters.Children.Add(new Border { Width = 16 });
                }

                PanelListFilters.Children.Add(new TextBlock
                {
                    Text = string.IsNullOrWhiteSpace(filter.Label) ? filter.Key : filter.Label,
                    VerticalAlignment = VerticalAlignment.Center,
                    Margin = new Thickness(0, 0, 6, 0),
                    FontSize = 13,
                    Foreground = TryFindResource("ColorBrushGray3") as Brush,
                });

                var combo = new ComboBox
                {
                    MinWidth = 120,
                    Margin = new Thickness(0, 0, 8, 0),
                    Tag = filter.Key,
                };

                int selected = 0;
                for (int index = 0; index < filter.Options.Count; index++)
                {
                    PluginPageFilterOptionDto option = filter.Options[index];
                    combo.Items.Add(option.Label);
                    if (string.Equals(option.Value, filter.Value, StringComparison.Ordinal))
                    {
                        selected = index;
                    }
                }

                combo.SelectedIndex = selected;
                combo.SelectionChanged += OnListFilterChanged;

                _listFilterOptions[filter.Key] = filter.Options;
                _listFilterValues[filter.Key] = filter.Options[selected].Value;

                PanelListFilters.Children.Add(combo);
                rendered++;
            }

            PanelListFilters.Visibility = rendered > 0
                ? Visibility.Visible
                : Visibility.Collapsed;
        }
        finally
        {
            _rebuildingListFilters = false;
        }
    }

    /// <summary>
    /// 筛选变更：只回传 <c>{page: 1, &lt;key&gt;: &lt;value&gt;}</c> 并重载，
    /// 页面内容、排序与空态全部由 Python 决定（C# 不做业务判断）。
    /// </summary>
    private void OnListFilterChanged(object sender, SelectionChangedEventArgs e)
    {
        if (_rebuildingListFilters
            || sender is not ComboBox combo
            || combo.Tag is not string key
            || combo.SelectedIndex < 0
            || !_listFilterOptions.TryGetValue(key, out List<PluginPageFilterOptionDto>? options)
            || combo.SelectedIndex >= options.Count)
        {
            return;
        }

        string value = options[combo.SelectedIndex].Value;
        if (_listFilterValues.TryGetValue(key, out string? current)
            && string.Equals(current, value, StringComparison.Ordinal))
        {
            return;
        }

        _listFilterValues[key] = value;
        _ = LoadAsync(1);
    }

    /// <summary>把当前筛选值作为加载参数带给 Python（只对 list 模板生效）。</summary>
    private void AddListFilterParameters(Dictionary<string, object?> parameters)
    {
        if (!string.Equals(_template, "list", StringComparison.Ordinal))
        {
            return;
        }

        foreach (KeyValuePair<string, string> pair in _listFilterValues)
        {
            parameters[pair.Key] = pair.Value;
        }
    }

    // ------------------------------------------------------------------
    // list 模板：封面（阶段 4-4 协议扩展：行内 thumb_book_id）
    // ------------------------------------------------------------------

    /// <summary>
    /// 行容器被虚拟化创建（= 当前可见/已加载）时才按需取封面：不对整页 rows
    /// 一次性发请求。同一 book_id 全局去重缓存；失败只把该行标成暗红底 X，
    /// 不影响列表渲染、不弹框。
    /// </summary>
    private void OnListRowLoading(object? sender, DataGridRowEventArgs e)
    {
        if (e.Row?.DataContext is PluginRowView row)
        {
            _ = EnsureRowCoverAsync(row);
        }
    }

    /// <summary>
    /// 取（或复用缓存）某行封面。内部可见：STA 测试直接喂行视图，断言
    /// “同一 book_id 只取一次 / 失败显示 X”。
    /// </summary>
    internal async Task EnsureRowCoverAsync(PluginRowView row)
    {
        string bookId = row.ThumbBookId;
        if (string.IsNullOrEmpty(bookId) || row.CoverSettled)
        {
            return;
        }

        if (_coverCache.TryGetValue(bookId, out ImageSource? cached))
        {
            row.SetCover(cached);
            return;
        }

        if (_coverFailed.Contains(bookId))
        {
            row.SetCoverFailed();
            return;
        }

        if (!_coverRequests.TryGetValue(bookId, out Task<byte[]?>? request))
        {
            request = RequestCoverBytesAsync(bookId);
            _coverRequests[bookId] = request;
        }

        byte[]? bytes;
        try
        {
            bytes = await request;
        }
        catch (Exception ex)
        {
            // 取图失败不得影响列表渲染、不弹框：只登记失败并显示 X。
            LogSanitizer.Log($"插件页面封面获取失败：{ex.GetType().Name}: {ex.Message}");
            bytes = null;
        }

        // 同一 book_id 的多行共用同一个请求：先完成的那次已写缓存，后续直接复用。
        if (_coverCache.TryGetValue(bookId, out ImageSource? shared))
        {
            row.SetCover(shared);
            return;
        }

        if (_coverFailed.Contains(bookId))
        {
            row.SetCoverFailed();
            return;
        }

        ApplyCoverBytes(row, bookId, bytes);
    }

    private async Task<byte[]?> RequestCoverBytesAsync(string bookId)
    {
        try
        {
            return CoverFetcher is { } fetcher
                ? await fetcher(bookId)
                : await SearchBridge.FetchCover(bookId);
        }
        catch (Exception ex)
        {
            LogSanitizer.Log($"插件页面封面获取失败：{ex.GetType().Name}: {ex.Message}");
            return null;
        }
    }

    /// <summary>UI 线程解码 bytes → BitmapImage（CacheOption=OnLoad）；失败显示 X。</summary>
    private void ApplyCoverBytes(PluginRowView row, string bookId, byte[]? bytes)
    {
        BitmapImage? image = bytes is { Length: > 0 } ? CreateCoverImage(bytes) : null;
        if (image is null)
        {
            _coverFailed.Add(bookId);
            row.SetCoverFailed();
            return;
        }

        _coverCache[bookId] = image;
        row.SetCover(image);
    }

    /// <summary>
    /// 在 UI 线程用字节创建 BitmapImage（旧缺陷 #7）：CacheOption=OnLoad 保证
    /// 流释放后仍可显示；Freeze 后可安全持有。失败返回 null（不抛）。
    /// </summary>
    private static BitmapImage? CreateCoverImage(byte[] bytes)
    {
        try
        {
            using var stream = new MemoryStream(bytes, writable: false);
            var image = new BitmapImage();
            image.BeginInit();
            image.CacheOption = BitmapCacheOption.OnLoad;
            image.StreamSource = stream;
            image.EndInit();
            image.Freeze();
            return image;
        }
        catch (Exception ex)
        {
            LogSanitizer.Log($"插件页面封面解码失败：{ex.GetType().Name}: {ex.Message}");
            return null;
        }
    }

    /// <summary>
    /// 行操作列：每行同一组按钮，点击时把该行原始 JSON 回传给 Python
    /// （C# 不解释行字段，也不做可见性判断）。
    /// </summary>
    private void AddRowActionColumn(PluginPageRowActionDto action)
    {
        var panel = new FrameworkElementFactory(typeof(StackPanel));
        panel.SetValue(StackPanel.OrientationProperty, Orientation.Horizontal);

        var button = new FrameworkElementFactory(typeof(Button));
        button.SetValue(
            FrameworkElement.StyleProperty,
            FindResource(action.Primary ? "PrimaryButton" : "SecondaryButton"));
        button.SetValue(FrameworkElement.MarginProperty, new Thickness(4, 0, 0, 0));
        button.SetValue(FrameworkElement.HeightProperty, 26.0);
        button.SetValue(Button.MinWidthProperty, 56.0);
        button.SetValue(ContentControl.ContentProperty, action.Title);
        button.SetValue(FrameworkElement.TagProperty, action.Id);
        button.AddHandler(Button.ClickEvent, new RoutedEventHandler(OnRowActionClick));

        panel.AppendChild(button);
        GridRows.Columns.Add(new DataGridTemplateColumn
        {
            Header = string.Empty,
            CellTemplate = new DataTemplate { VisualTree = panel },
            Width = DataGridLength.Auto,
        });
    }

    private async void OnRowActionClick(object sender, RoutedEventArgs e)
    {
        if (sender is not FrameworkElement element)
        {
            return;
        }

        string actionId = element.Tag as string ?? string.Empty;
        await RunActionAsync(actionId, BuildRowPayload(element.DataContext as PluginRowView));
    }

    // ------------------------------------------------------------------
    // list 模板：行双击（阶段 4-4 协议扩展：data.row_double_click_action）
    // ------------------------------------------------------------------

    /// <summary>
    /// 双击行：payload 与行按钮完全一致（<c>{row, row_index, page}</c>），action id
    /// 来自 Python 的 <c>data.row_double_click_action</c>；空串 = 不响应
    /// （旧插件行为不变）。双击行内按钮 / 表头不算“双击行”，避免重复派发。
    /// </summary>
    private async void GridRows_MouseDoubleClick(object sender, MouseButtonEventArgs e)
    {
        if (string.IsNullOrEmpty(_rowDoubleClickAction))
        {
            return;
        }

        if (HasAncestor<Button>(e.OriginalSource as DependencyObject)
            || HasAncestor<DataGridColumnHeader>(e.OriginalSource as DependencyObject))
        {
            return;
        }

        if (GridRows.SelectedItem is not PluginRowView row)
        {
            return;
        }

        await RunListRowDoubleClickAsync(row);
    }

    /// <summary>内部可见：STA 测试直接调用，验证“双击 → action → intent”的完整映射。</summary>
    internal async Task RunListRowDoubleClickAsync(PluginRowView row)
    {
        if (row is null || string.IsNullOrEmpty(_rowDoubleClickAction))
        {
            return;
        }

        await RunActionAsync(_rowDoubleClickAction, BuildRowPayload(row));
    }

    /// <summary>行动作 / 双击共用的参数：整行原始 JSON + 行下标 + 当前页码。</summary>
    private Dictionary<string, object?> BuildRowPayload(PluginRowView? row) => new()
    {
        ["row"] = row?.Row,
        ["row_index"] = row?.Index ?? -1,
        ["page"] = _listPage,
    };

    /// <summary>沿视觉/逻辑父链找祖先（OriginalSource 可能是 TextBlock / Border 等）。</summary>
    private static bool HasAncestor<T>(DependencyObject? source) where T : DependencyObject
    {
        DependencyObject? current = source;
        while (current is not null)
        {
            if (current is T)
            {
                return true;
            }

            if (current is ContentElement content)
            {
                current = ContentOperations.GetParent(content)
                    ?? (content as FrameworkContentElement)?.Parent;
                continue;
            }

            current = current is Visual or Visual3D
                ? VisualTreeHelper.GetParent(current)
                : LogicalTreeHelper.GetParent(current);
        }

        return false;
    }

    private void BtnListPrev_Click(object sender, RoutedEventArgs e)
    {
        _ = LoadAsync(_listPage - 1);
    }

    private void BtnListNext_Click(object sender, RoutedEventArgs e)
    {
        _ = LoadAsync(_listPage + 1);
    }

    // ------------------------------------------------------------------
    // browser 模板
    // ------------------------------------------------------------------

    private void RenderBrowser(JsonElement data)
    {
        PluginBrowserPageDto? page = PluginPageData.ToBrowser(data);
        if (page is null)
        {
            ShowError("INVALID_DATA", "browser 模板数据不是 JSON 对象");
            return;
        }

        ShowOnly(PanelBrowser);

        // 阶段 4-2 协议小扩展：是否显示「选择文件夹…」按钮、当前目录是什么，
        // 全部由 Python 决定（C# 只按布尔值选可见性，不判断业务状态）。
        bool allowFolderChoice = ReadJsonBool(data, "allow_folder_choice");
        if (TryReadJsonString(data, "folder", out string folder))
        {
            _folder = folder;
        }

        BtnChooseFolder.Visibility = allowFolderChoice
            ? Visibility.Visible
            : Visibility.Collapsed;

        _fileItems.Clear();
        _fileItems.AddRange(page.Items);
        BrowserFileList.ItemsSource = _fileItems.Select(item => item.Name).ToList();

        _filePage = page.Page > 0 ? page.Page : 1;
        _fileTotalPages = ResolveTotalPages(
            page.TotalPages, page.Total, page.PageSize, _fileItems.Count);
        TextFilePage.Text = $"第 {_filePage} 页 / 共 {_fileTotalPages} 页";
        BtnFilePrev.IsEnabled = _filePage > 1;
        BtnFileNext.IsEnabled = _filePage < _fileTotalPages;

        bool listEmpty = _fileItems.Count == 0;
        TextBrowserEmpty.Text = string.IsNullOrWhiteSpace(page.EmptyText)
            ? "暂无文件"
            : page.EmptyText;
        TextBrowserEmpty.Visibility = listEmpty
            ? Visibility.Visible
            : Visibility.Collapsed;
        PanelFilePager.Visibility = listEmpty
            ? Visibility.Collapsed
            : Visibility.Visible;

        _itemIndex = page.ItemIndex;
        _imageIndex = page.ImageIndex > 0 ? page.ImageIndex : 0;
        _imageCount = page.ImageCount > 0 ? page.ImageCount : 0;
        _viewingImage = _itemIndex >= 0 && _itemIndex < _fileItems.Count;

        BrowserFileList.SelectedIndex = _viewingImage ? _itemIndex : -1;

        PanelImageBar.Visibility = _viewingImage
            ? Visibility.Visible
            : Visibility.Collapsed;
        ImagePage.Source = null;
        ImagePage.Visibility = Visibility.Collapsed;

        if (_viewingImage)
        {
            UpdateImagePager();
            TextImageHint.Text = "正在加载图片…";
            TextImageHint.Visibility = Visibility.Visible;
        }
        else
        {
            TextImagePage.Text = string.Empty;
            TextImageHint.Text = listEmpty
                ? string.Empty
                : "双击左侧文件查看内容";
            TextImageHint.Visibility = listEmpty
                ? Visibility.Collapsed
                : Visibility.Visible;
        }
    }

    private void UpdateImagePager()
    {
        TextImagePage.Text = $"第 {_imageIndex + 1} 页 / 共 {_imageCount} 页";
        BtnImagePrev.IsEnabled = _imageIndex > 0;
        BtnImageNext.IsEnabled = _imageCount > 0 && _imageIndex + 1 < _imageCount;
    }

    /// <summary>
    /// 选定文件夹后以 <c>{page: 1, folder, item_index: -1}</c> 重新 load
    /// （阶段 4-2：列哪个目录、有没有 ZIP、空态文案全部由 Python 决定）。
    /// </summary>
    internal async Task LoadBrowserFolderAsync(string folder)
    {
        await LoadFolderAsync(folder);
    }

    /// <summary>
    /// browser / action 共用的文件夹加载：只回传路径与页码，内容由 Python 决定。
    /// </summary>
    private async Task LoadFolderAsync(string folder)
    {
        if (string.IsNullOrWhiteSpace(folder))
        {
            return;
        }

        try
        {
            _folder = folder.Trim();
            var parameters = new Dictionary<string, object?>
            {
                ["page"] = 1,
                ["folder"] = _folder,
                ["item_index"] = -1,
            };
            ShowStatus("正在加载页面…");
            PluginPageLoadResultDto result =
                await _gateway.LoadAsync(PluginId, PageId, parameters);
            ApplyLoad(result);
            if (result.Ok)
            {
                ClearStatus();
            }
        }
        catch (Exception ex)
        {
            ShowError("UNKNOWN_ERROR", LogSanitizer.Sanitize(ex.Message));
        }
    }

    /// <summary>选择文件夹（browser / action 共用；只把路径回传 Python）。</summary>
    private async Task HandleFolderChoiceAsync()
    {
        string? folder;
        try
        {
            // 生产弹系统文件夹选择框；STA 测试注入假实现（不真的弹窗）
            folder = FolderPicker is { } picker ? picker() : PickFolderWithDialog();
        }
        catch (Exception ex)
        {
            ShowError("UNKNOWN_ERROR", LogSanitizer.Sanitize(ex.Message));
            return;
        }

        await LoadFolderAsync(folder ?? string.Empty);
    }

    private async void BtnChooseFolder_Click(object sender, RoutedEventArgs e)
    {
        await HandleFolderChoiceAsync();
    }

    private async void BtnActionChooseFolder_Click(object sender, RoutedEventArgs e)
    {
        await HandleFolderChoiceAsync();
    }

    /// <summary>系统文件夹选择框：只返回路径，不做任何业务判断。</summary>
    private static string? PickFolderWithDialog()
    {
        var dialog = new Microsoft.Win32.OpenFolderDialog
        {
            Title = "选择压缩包文件夹",
        };
        return dialog.ShowDialog() == true ? dialog.FolderName : null;
    }

    /// <summary>把当前目录作为 <c>folder</c> 参数带给 Python（未选目录时不带）。</summary>
    private void AddFolderParameter(Dictionary<string, object?> parameters)
    {
        if (!string.IsNullOrEmpty(_folder))
        {
            parameters["folder"] = _folder;
        }
    }

    /// <summary>打开文件列表中的某个条目（index = -1 表示回到列表）。</summary>
    internal async Task LoadBrowserItemAsync(int itemIndex, int imageIndex = 0)
    {
        try
        {
            var parameters = new Dictionary<string, object?>
            {
                ["page"] = _filePage,
                ["item_index"] = itemIndex,
                ["image_index"] = imageIndex,
            };
            AddFolderParameter(parameters);
            ShowStatus("正在加载页面…");
            PluginPageLoadResultDto result =
                await _gateway.LoadAsync(PluginId, PageId, parameters);
            ApplyLoad(result);
            if (result.Ok)
            {
                ClearStatus();
            }

            if (result.Ok && _viewingImage)
            {
                await LoadImageAsync();
            }
        }
        catch (Exception ex)
        {
            ShowError("UNKNOWN_ERROR", LogSanitizer.Sanitize(ex.Message));
        }
    }

    /// <summary>按当前 image_index 取图片并交给 UI 线程渲染。</summary>
    internal async Task LoadImageAsync()
    {
        if (!_viewingImage)
        {
            return;
        }

        TextImageHint.Text = "正在加载图片…";
        TextImageHint.Visibility = Visibility.Visible;

        byte[]? bytes;
        try
        {
            bytes = await _gateway.ImageAsync(PluginId, PageId, _imageIndex);
        }
        catch (Exception ex)
        {
            BridgeFailures.Log("获取插件页面图片", ex);
            bytes = null;
        }

        // 回 UI 线程后创建 BitmapImage：工作线程只产出 bytes（旧缺陷 #7）。
        ApplyImage(bytes);
    }

    /// <summary>把原始图片字节渲染到界面（内部可见：STA 测试直接喂 bytes）。</summary>
    internal void ApplyImage(byte[]? bytes)
    {
        if (bytes is null || bytes.Length == 0)
        {
            ImagePage.Source = null;
            ImagePage.Visibility = Visibility.Collapsed;
            TextImageHint.Text = "图片加载失败";
            TextImageHint.Visibility = Visibility.Visible;
            return;
        }

        try
        {
            var bitmap = new BitmapImage();
            using (var stream = new MemoryStream(bytes, writable: false))
            {
                bitmap.BeginInit();
                bitmap.CacheOption = BitmapCacheOption.OnLoad;
                bitmap.StreamSource = stream;
                bitmap.EndInit();
            }

            bitmap.Freeze();
            ImagePage.Source = bitmap;
            ImagePage.Visibility = Visibility.Visible;
            TextImageHint.Visibility = Visibility.Collapsed;
        }
        catch (Exception ex)
        {
            ImagePage.Source = null;
            ImagePage.Visibility = Visibility.Collapsed;
            TextImageHint.Text = "图片加载失败";
            TextImageHint.Visibility = Visibility.Visible;
            LogSanitizer.Log(
                $"插件页面图片解码失败：{ex.GetType().Name}: {ex.Message}");
        }
    }

    private async void BrowserFileList_MouseDoubleClick(
        object sender,
        MouseButtonEventArgs e)
    {
        int index = BrowserFileList.SelectedIndex;
        if (index < 0)
        {
            return;
        }

        await LoadBrowserItemAsync(index);
    }

    private async void BtnBrowserBack_Click(object sender, RoutedEventArgs e)
    {
        await BackToFileListAsync();
    }

    /// <summary>从图片视图回到文件列表（按钮与 Esc 共用）。</summary>
    internal async Task BackToFileListAsync()
    {
        if (!IsViewingImage || _goingBackToList)
        {
            return;
        }

        _goingBackToList = true;
        try
        {
            await LoadBrowserItemAsync(-1);
        }
        finally
        {
            _goingBackToList = false;
        }
    }

    /// <summary>当前是否停在 browser 的图片视图（Esc 返回列表的判定条件）。</summary>
    internal bool IsViewingImage =>
        PanelBrowser.Visibility == Visibility.Visible && _viewingImage;

    /// <summary>
    /// Esc 语义（计划书 4.6「返回列表（返回按钮或 Esc）」）：
    /// 只在图片视图里拦截；其余情况返回 false，不影响上层内页返回逻辑。
    /// MainWindow 的窗口级 Esc 也会调用本方法，保证焦点不在本页时仍然有效。
    /// </summary>
    public bool TryHandleEscape()
    {
        if (!IsViewingImage)
        {
            return false;
        }

        _ = BackToFileListAsync();
        return true;
    }

    private void BtnImagePrev_Click(object sender, RoutedEventArgs e)
    {
        if (_imageIndex <= 0)
        {
            return;
        }

        _imageIndex--;
        UpdateImagePager();
        _ = LoadImageAsync();
    }

    private void BtnImageNext_Click(object sender, RoutedEventArgs e)
    {
        if (_imageCount <= 0 || _imageIndex + 1 >= _imageCount)
        {
            return;
        }

        _imageIndex++;
        UpdateImagePager();
        _ = LoadImageAsync();
    }

    private void BtnFilePrev_Click(object sender, RoutedEventArgs e)
    {
        _ = LoadAsync(_filePage - 1);
    }

    private void BtnFileNext_Click(object sender, RoutedEventArgs e)
    {
        _ = LoadAsync(_filePage + 1);
    }

    private void Host_KeyDown(object sender, KeyEventArgs e)
    {
        if (e.Key != Key.Escape)
        {
            return;
        }

        // 图片视图里 Esc = 返回文件列表（计划书 4.6）；不在图片视图时不拦截，
        // 让上层（MainWindow）的内页返回逻辑自行处理。
        if (TryHandleEscape())
        {
            e.Handled = true;
        }
    }

    // ------------------------------------------------------------------
    // action 模板
    // ------------------------------------------------------------------

    private void RenderAction(JsonElement data)
    {
        PluginActionPageDto? page = PluginPageData.ToAction(data);
        if (page is null)
        {
            ShowError("INVALID_DATA", "action 模板数据不是 JSON 对象");
            return;
        }

        ShowOnly(PanelAction);

        // 阶段 4-3 协议小扩展：action 模板也支持文件夹选择与外部浏览器打开。
        // 显隐、网址是否存在全部由 Python 决定，C# 只负责转发。
        bool allowFolderChoice = ReadJsonBool(data, "allow_folder_choice");
        if (TryReadJsonString(data, "folder", out string folder))
        {
            _folder = folder;
        }

        BtnActionChooseFolder.Visibility = allowFolderChoice
            ? Visibility.Visible
            : Visibility.Collapsed;

        _actionUrl = string.Empty;
        if (TryReadJsonString(data, "url", out string url) && IsHttpUrl(url))
        {
            _actionUrl = url;
        }

        // 页面刷新/重渲染只更新按钮，不自动弹浏览器；若页面已不再指向自动打开过的
        // 地址（停止服务后 url 变空、或换了新地址），清掉幂等标记，允许新操作重新打开。
        if (!string.Equals(_autoOpenedUrl, _actionUrl, StringComparison.Ordinal))
        {
            _autoOpenedUrl = string.Empty;
        }

        BtnActionOpenBrowser.Visibility = string.IsNullOrEmpty(_actionUrl)
            ? Visibility.Collapsed
            : Visibility.Visible;
        BtnActionOpenBrowser.IsEnabled = !string.IsNullOrEmpty(_actionUrl);

        // 复制链接是「浏览器唤起失败」时的手动兜底：url 存在就始终显示，
        // 不依赖打开是否成功（C# 只写剪贴板，不打开任何东西）。
        BtnActionCopyUrl.Visibility = string.IsNullOrEmpty(_actionUrl)
            ? Visibility.Collapsed
            : Visibility.Visible;
        BtnActionCopyUrl.IsEnabled = !string.IsNullOrEmpty(_actionUrl);

        _actionItems.Clear();
        _actionItems.AddRange(page.Items);
        ActionFileList.ItemsSource = _actionItems.Select(item => item.Name).ToList();

        bool empty = _actionItems.Count == 0;
        TextActionEmpty.Text = string.IsNullOrWhiteSpace(page.EmptyText)
            ? "暂无文件"
            : page.EmptyText;
        TextActionEmpty.Visibility = empty
            ? Visibility.Visible
            : Visibility.Collapsed;

        PanelActionButtons.Children.Clear();
        foreach (PluginPageActionDto action in page.Actions)
        {
            var button = new Button
            {
                Content = action.Title,
                Style = (Style)FindResource("SecondaryButton"),
                MinWidth = 76,
                Margin = new Thickness(0, 0, 8, 0),
                Tag = action.Id,
            };
            button.Click += OnActionButtonClick;
            PanelActionButtons.Children.Add(button);
        }

        TextActionStatus.Text = BuildActionStatusText(page);
    }

    /// <summary>状态文案来自 Python；port 只做展示（0 表示未启动）。</summary>
    private static string BuildActionStatusText(PluginActionPageDto page)
    {
        if (!string.IsNullOrWhiteSpace(page.StatusText))
        {
            return page.StatusText;
        }

        return page.Port > 0 ? $"服务端口：{page.Port}" : page.State;
    }

    private async void OnActionButtonClick(object sender, RoutedEventArgs e)
    {
        string actionId = (sender as FrameworkElement)?.Tag as string ?? string.Empty;
        int selected = ActionFileList.SelectedIndex;
        Dictionary<string, object?>? item = null;
        if (selected >= 0 && selected < _actionItems.Count)
        {
            item = new Dictionary<string, object?>
            {
                ["name"] = _actionItems[selected].Name,
                ["path"] = _actionItems[selected].Path,
            };
        }

        var payload = new Dictionary<string, object?>
        {
            ["item"] = item,
            ["item_index"] = selected,
        };
        await RunActionAsync(actionId, payload);
    }

    private void BtnActionRefresh_Click(object sender, RoutedEventArgs e)
    {
        _ = LoadAsync(1);
    }

    /// <summary>生产打开器 ①：系统 Shell（UseShellExecute=true）转发 http(s)。</summary>
    private static bool OpenWithSystemShell(string url)
    {
        Process.Start(new ProcessStartInfo(url) { UseShellExecute = true });
        return true;
    }

    /// <summary>生产打开器 ②：<c>cmd.exe /c start "" "&lt;url&gt;"</c>（CreateNoWindow）。</summary>
    private static bool OpenWithCmdStart(string url)
    {
        var info = new ProcessStartInfo("cmd.exe")
        {
            UseShellExecute = false,
            CreateNoWindow = true,
        };
        info.ArgumentList.Add("/c");
        info.ArgumentList.Add("start");
        info.ArgumentList.Add(""); // 窗口标题占位：start 会把第一个带引号参数当标题
        info.ArgumentList.Add(url);
        Process.Start(info);
        return true;
    }

    /// <summary>生产打开器 ③：<c>rundll32.exe url.dll,FileProtocolHandler "&lt;url&gt;"</c>。</summary>
    private static bool OpenWithRundll32(string url)
    {
        var info = new ProcessStartInfo("rundll32.exe")
        {
            UseShellExecute = false,
            CreateNoWindow = true,
        };
        info.ArgumentList.Add("url.dll,FileProtocolHandler");
        info.ArgumentList.Add(url);
        Process.Start(info);
        return true;
    }

    /// <summary>生产默认的三级打开器链（顺序即兜底顺序）。</summary>
    internal static IReadOnlyList<BrowserOpenAttempt> ProductionOpenAttempts() =>
    [
        new("Process.Start(UseShellExecute)", OpenWithSystemShell),
        new("cmd.exe /c start", OpenWithCmdStart),
        new("rundll32 url.dll,FileProtocolHandler", OpenWithRundll32),
    ];

    /// <summary>
    /// 「在浏览器中打开」按钮：备用入口。用户显式点击时总是转发一次，
    /// 不参与自动打开的幂等判断。
    /// </summary>
    private void BtnActionOpenBrowser_Click(object sender, RoutedEventArgs e)
    {
        if (string.IsNullOrWhiteSpace(_actionUrl))
        {
            return;
        }

        OpenBrowser(_actionUrl, userInitiated: true);
    }

    /// <summary>
    /// 「复制链接」按钮：url 存在时始终可用，用于浏览器唤起全部失败后手动粘贴打开。
    /// 只写剪贴板，不打开任何进程；剪贴板实现可注入（STA 测试不污染本机剪贴板）。
    /// </summary>
    private void BtnActionCopyUrl_Click(object sender, RoutedEventArgs e)
    {
        if (string.IsNullOrWhiteSpace(_actionUrl))
        {
            return;
        }

        try
        {
            if (ClipboardWriter is { } writer)
            {
                writer(_actionUrl);
            }
            else
            {
                Clipboard.SetText(_actionUrl);
            }

            ShowStatus($"链接已复制：{_actionUrl}");
        }
        catch (Exception ex)
        {
            ShowError(
                "COPY_FAILED",
                $"复制链接失败：{_actionUrl}（{ex.GetType().Name}: " +
                $"{LogSanitizer.Sanitize(ex.Message)}）");
        }
    }

    /// <summary>
    /// 动作成功后自动唤起浏览器：只打开**当前页面已渲染**的网址
    /// （<see cref="_actionUrl"/>），而不是动作返回里可能已被后续动作顶替的旧地址
    /// ——换包/重复 start 会关闭旧端口，打开旧地址只会得到"无法访问"的标签页。
    /// 页面刷新 / 重新渲染同一 url 不会再次弹浏览器。
    /// </summary>
    private void TryAutoOpenRenderedUrl()
    {
        if (!string.Equals(_template, "action", StringComparison.Ordinal)
            || string.IsNullOrEmpty(_actionUrl))
        {
            return;
        }

        OpenBrowser(_actionUrl, userInitiated: false);
    }

    /// <summary>
    /// 打开一次系统浏览器，按 ① Process.Start → ② cmd start → ③ rundll32 依次兜底。
    /// <paramref name="userInitiated"/>=true（用户点按钮）总是尝试；false（动作结果自动
    /// 唤起）时，同一 url 成功打开过后不再重复，失败则允许重试。
    ///
    /// 每次尝试都记录下来：方法名、异常类型、HResult、脱敏 Message。前一次成功即停止
    /// 后续尝试（避免同一地址被打开多次）；全部失败时状态区显示
    /// <c>[OPEN_FAILED] 三种方式均无法打开浏览器：url</c>，不静默。
    /// </summary>
    private bool OpenBrowser(string url, bool userInitiated)
    {
        if (!IsHttpUrl(url))
        {
            return false;
        }

        if (!userInitiated && string.Equals(url, _autoOpenedUrl, StringComparison.Ordinal))
        {
            return false;
        }

        IReadOnlyList<BrowserOpenAttempt> attempts = ResolveOpenAttempts();
        var records = new List<BrowserOpenRecord>(attempts.Count);
        foreach (BrowserOpenAttempt attempt in attempts)
        {
            BrowserOpenRecord record;
            try
            {
                record = attempt.Open(url)
                    ? BrowserOpenRecord.OpenSucceeded(attempt.Method)
                    : BrowserOpenRecord.NotForwarded(attempt.Method);
            }
            catch (Exception ex)
            {
                record = BrowserOpenRecord.Failed(attempt.Method, ex);
            }

            records.Add(record);
            AppendBrowserOpenDiagnostics(url, record);

            if (record.Succeeded)
            {
                LastBrowserOpenRecords = records;
                if (!userInitiated)
                {
                    _autoOpenedUrl = url;
                }

                // 成功时明确记录“已用方法 X 打开”（若前面有失败尝试一并说明）。
                int failedBefore = records.Count(item => !item.Succeeded);
                string fallbackNote = failedBefore > 0
                    ? $"（前 {failedBefore} 次尝试失败，已记录）"
                    : "";
                ShowStatus($"已用方法 {record.Method} 打开浏览器：{url}{fallbackNote}");
                return true;
            }
        }

        LastBrowserOpenRecords = records;
        if (!userInitiated)
        {
            _autoOpenedUrl = string.Empty;
        }

        ShowBrowserOpenError(url, records);
        return false;
    }

    /// <summary>注入链优先；单级注入打开器其次；生产默认三级链。</summary>
    private IReadOnlyList<BrowserOpenAttempt> ResolveOpenAttempts()
    {
        if (BrowserOpeners is { Count: > 0 } injected)
        {
            return injected;
        }

        return BrowserOpener is { } opener
            ? [new BrowserOpenAttempt("注入的打开器", opener)]
            : ProductionOpenAttempts();
    }

    /// <summary>
    /// 打开失败不静默：状态区显示 <c>[OPEN_FAILED] ...：url（逐次尝试明细）</c>，
    /// 明细包含方法名、异常类型、HResult 与脱敏 Message。
    /// </summary>
    private void ShowBrowserOpenError(string url, IReadOnlyList<BrowserOpenRecord> records)
    {
        string details = string.Join(
            "；", records.Select(record => record.Describe()));
        string prefix = records.Count >= ProductionOpenAttempts().Count
            ? "三种方式均无法打开浏览器"
            : "打开浏览器失败";
        ShowError("OPEN_FAILED", $"{prefix}：{url}（{details}）");
    }

    /// <summary>
    /// 每次浏览器打开尝试都追加到 <c>logs/startup-diagnostics.log</c>（脱敏、单行）。
    /// 诊断落盘失败不影响主流程。
    /// </summary>
    private static void AppendBrowserOpenDiagnostics(string url, BrowserOpenRecord record)
    {
        try
        {
            Directory.CreateDirectory(AppPaths.LogDirectory);
            string path = Path.Combine(AppPaths.LogDirectory, "startup-diagnostics.log");
            File.AppendAllText(
                path,
                $"[{DateTime.Now:yyyy-MM-dd HH:mm:ss}] "
                + LogSanitizer.Sanitize($"浏览器打开尝试：{record.Describe()}；url={url}")
                + Environment.NewLine,
                new System.Text.UTF8Encoding(encoderShouldEmitUTF8Identifier: false));
        }
        catch
        {
            // 诊断日志写不进去不影响打开流程。
        }
    }

    // ------------------------------------------------------------------
    // 动作结果
    // ------------------------------------------------------------------

    private async Task RunActionAsync(
        string actionId,
        Dictionary<string, object?> payload)
    {
        if (string.IsNullOrWhiteSpace(actionId))
        {
            return;
        }

        // 单飞：动作执行期间忽略后续点击。zip_web 的 start 会重建服务端口，
        // 连点时第二次 start 会把第一次刚显示/刚打开的地址停掉。
        if (_actionInFlight)
        {
            return;
        }

        _actionInFlight = true;
        SetActionButtonsEnabled(false);
        try
        {
            PluginPageActionResultDto result =
                await _gateway.ActionAsync(PluginId, PageId, actionId, payload);
            await ApplyActionResultAsync(result);
        }
        catch (Exception ex)
        {
            ShowError("UNKNOWN_ERROR", LogSanitizer.Sanitize(ex.Message));
        }
        finally
        {
            _actionInFlight = false;
            SetActionButtonsEnabled(true);
        }
    }

    /// <summary>动作执行期间禁用/恢复动作按钮（按钮由 RenderAction 每次重建）。</summary>
    private void SetActionButtonsEnabled(bool enabled)
    {
        foreach (object child in PanelActionButtons.Children)
        {
            if (child is System.Windows.Controls.Button button)
            {
                button.IsEnabled = enabled;
            }
        }
    }

    /// <summary>
    /// 处理 <c>plugin_page_action</c> 返回：失败显示 <c>[error_code] message</c>；
    /// 成功时展示 message，并按 refresh/data 决定整页重载还是就地换数据。
    /// </summary>
    internal async Task ApplyActionResultAsync(PluginPageActionResultDto? result)
    {
        if (result is null)
        {
            ShowError("UNKNOWN_ERROR", "插件页面动作返回为空");
            return;
        }

        if (!result.Ok)
        {
            ShowError(result.ErrorCode, result.ErrorMessage);
            return;
        }

        ClearError();
        if (!string.IsNullOrWhiteSpace(result.Message))
        {
            ShowStatus(result.Message);
            if (PanelAction.Visibility == Visibility.Visible)
            {
                TextActionStatus.Text = result.Message;
            }
        }

        // 阶段 4-4：插件用 intent 表达"详情 / 下载"，C# 只转发给主窗口的
        // 既有路径（打开 DetailWindow / 切下载页 + StartWithIds），不做业务。
        if (result.Intent is { } intent && !string.IsNullOrWhiteSpace(intent.Kind))
        {
            DispatchIntent(intent);
        }

        if (result.Refresh)
        {
            await LoadAsync(_template == "browser" ? _filePage : _listPage);
            // 重载会清掉临时错误，自动唤起放在重载之后，失败文案才不会被覆盖。
            TryAutoOpenRenderedUrl();
            return;
        }

        if (result.Data is { } data && PluginPageData.IsObject(data))
        {
            ApplyLoad(new PluginPageLoadResultDto
            {
                Ok = true,
                Template = _template,
                Title = _title,
                Data = data,
            });
        }

        // 动作成功且页面当前渲染出 http(s) 绝对地址 → 自动唤起一次浏览器。
        TryAutoOpenRenderedUrl();
    }

    /// <summary>
    /// 把插件返回的 intent 交给 <see cref="IUiIntentHandler"/>：
    /// 生产环境是所属 <see cref="MainWindow"/>（打开详情窗 / 切下载页），
    /// STA 测试注入假实现；未注入且不在主窗口里时只报协议提示，不崩。
    /// </summary>
    private void DispatchIntent(PluginPageIntentDto intent)
    {
        IUiIntentHandler? handler = IntentHandler ?? Window.GetWindow(this) as IUiIntentHandler;
        if (handler is null)
        {
            ShowError(
                "NOT_IMPLEMENTED",
                "当前窗口不支持插件页面动作意图（未提供 IUiIntentHandler）");
            return;
        }

        try
        {
            handler.HandleIntent(intent);
        }
        catch (Exception ex)
        {
            ShowError("UNKNOWN_ERROR", LogSanitizer.Sanitize(ex.Message));
        }
    }

    // ------------------------------------------------------------------
    // 通用
    // ------------------------------------------------------------------

    /// <summary>只接受 http/https 绝对地址（其它协议不显示「在浏览器中打开」）。</summary>
    private static bool IsHttpUrl(string url) =>
        Uri.TryCreate(url, UriKind.Absolute, out Uri? uri)
        && (uri.Scheme == Uri.UriSchemeHttp || uri.Scheme == Uri.UriSchemeHttps);

    /// <summary>读可选的布尔字段（缺省 / 类型不符 → fallback）。</summary>
    private static bool ReadJsonBool(JsonElement data, string name, bool fallback = false)
    {
        if (data.ValueKind != JsonValueKind.Object
            || !data.TryGetProperty(name, out JsonElement value))
        {
            return fallback;
        }

        return value.ValueKind switch
        {
            JsonValueKind.True => true,
            JsonValueKind.False => false,
            _ => fallback,
        };
    }

    /// <summary>读可选的字符串字段；字段缺失或不是字符串时返回 false。</summary>
    private static bool TryReadJsonString(JsonElement data, string name, out string value)
    {
        value = string.Empty;
        if (data.ValueKind != JsonValueKind.Object
            || !data.TryGetProperty(name, out JsonElement element)
            || element.ValueKind != JsonValueKind.String)
        {
            return false;
        }

        value = element.GetString() ?? string.Empty;
        return true;
    }

    /// <summary>列表总页数：优先用 Python 给的 total_pages，否则按 total/page_size 推算。</summary>
    private static int ResolveTotalPages(
        int totalPages,
        int total,
        int pageSize,
        int loadedCount)
    {
        if (totalPages > 0)
        {
            return totalPages;
        }

        int size = pageSize > 0 ? pageSize : loadedCount;
        if (total > 0 && size > 0)
        {
            return Math.Max(1, (int)Math.Ceiling(total / (double)size));
        }

        return 1;
    }

    private void ShowOnly(FrameworkElement panel)
    {
        PanelList.Visibility = ReferenceEquals(panel, PanelList)
            ? Visibility.Visible
            : Visibility.Collapsed;
        PanelBrowser.Visibility = ReferenceEquals(panel, PanelBrowser)
            ? Visibility.Visible
            : Visibility.Collapsed;
        PanelAction.Visibility = ReferenceEquals(panel, PanelAction)
            ? Visibility.Visible
            : Visibility.Collapsed;
    }

    internal void ShowError(string? errorCode, string? errorMessage)
    {
        string code = string.IsNullOrWhiteSpace(errorCode) ? "UNKNOWN_ERROR" : errorCode;
        TextError.Text =
            $"[{LogSanitizer.Sanitize(code)}] {LogSanitizer.Sanitize(errorMessage ?? "")}";
        TextError.Visibility = Visibility.Visible;
        TextStatus.Visibility = Visibility.Collapsed;
    }

    private void ClearError()
    {
        TextError.Text = string.Empty;
        TextError.Visibility = Visibility.Collapsed;
    }

    private void ShowStatus(string message)
    {
        TextStatus.Text = message;
        TextStatus.Visibility = string.IsNullOrWhiteSpace(message)
            ? Visibility.Collapsed
            : Visibility.Visible;
    }

    private void ClearStatus()
    {
        TextStatus.Text = string.Empty;
        TextStatus.Visibility = Visibility.Collapsed;
    }
}

/// <summary>
/// 插件页面动作 intent 的转发接口（阶段 4-4）。
/// 实现方（生产是 <see cref="MainWindow"/>，测试是假实现）只负责把
/// <c>detail</c> / <c>download</c> 交给现有界面路径，不做任何业务判断。
/// </summary>
internal interface IUiIntentHandler
{
    /// <summary>处理一次插件页面动作意图（detail / download）。</summary>
    void HandleIntent(PluginPageIntentDto intent);
}

/// <summary>
/// 浏览器打开链中的一级：方法名（写进诊断）+ 实际打开委托（返回是否已转发）。
/// 生产顺序见 <c>ProductionOpenAttempts</c>；测试注入假实现断言顺序与短路。
/// </summary>
internal sealed record BrowserOpenAttempt(string Method, Func<string, bool> Open);

/// <summary>
/// 一次浏览器打开尝试的回执：方法名、是否成功、异常类型 / HResult / 脱敏 Message。
/// 全部失败时明细会显示在页面状态区并写入 startup-diagnostics.log。
/// </summary>
internal sealed record BrowserOpenRecord(
    string Method,
    bool Succeeded,
    string ErrorType,
    int HResult,
    string Message)
{
    public static BrowserOpenRecord OpenSucceeded(string method) =>
        new(method, true, "", 0, "");

    public static BrowserOpenRecord NotForwarded(string method) =>
        new(method, false, "", 0, "打开器未转发（返回 false）");

    public static BrowserOpenRecord Failed(string method, Exception exception) =>
        new(
            method,
            false,
            exception.GetType().Name,
            exception.HResult,
            LogSanitizer.Sanitize(exception.Message ?? "")
                .Replace("\r", " ")
                .Replace("\n", " "));

    /// <summary>单行明细：成功 → “方法：已打开”；失败 → 方法 + 异常/HResult/Message。</summary>
    public string Describe()
    {
        if (Succeeded)
        {
            return $"{Method}：已打开";
        }

        if (string.IsNullOrEmpty(ErrorType))
        {
            return $"{Method}：{Message}";
        }

        return $"{Method}：{ErrorType} HResult=0x{HResult:X8} Message={Message}";
    }
}

/// <summary>
/// 行数据视图：DataGrid 的绑定源。保留插件原始行（<see cref="Row"/>）供动作
/// 回传，展示时用索引器取值（列 key 由 Python 决定）。
///
/// 封面状态（阶段 4-4 协议扩展：行内 <c>thumb_book_id</c>）也挂在这里：
/// 取图成功显示图片、失败显示暗红底 X、未完成显示灰底占位；任何状态都只
/// 影响本行，不影响列表渲染。
/// </summary>
internal sealed class PluginRowView : INotifyPropertyChanged
{
    private ImageSource? _cover;
    private bool _coverFailed;

    public PluginRowView(Dictionary<string, JsonElement> row, int index)
    {
        Row = row;
        Index = index;
        ThumbBookId = PluginPageRowFields.GetThumbBookId(row);
    }

    /// <summary>插件原始行（回传给 plugin_page_action 的 row 字段）。</summary>
    public Dictionary<string, JsonElement> Row { get; }

    /// <summary>行下标（0 起）。</summary>
    public int Index { get; }

    /// <summary>
    /// 行内可选封面编号（阶段 4-4 协议扩展）；空串 = 本行不显示封面，
    /// 也不发任何取图请求。
    /// </summary>
    public string ThumbBookId { get; }

    /// <summary>已解码的封面（未取到 / 失败时为 null）。</summary>
    public ImageSource? Cover => _cover;

    /// <summary>灰底占位：尚未开始或正在取图。</summary>
    public Visibility PlaceholderVisibility =>
        !CoverSettled ? Visibility.Visible : Visibility.Collapsed;

    /// <summary>暗红底 X：取图或解码失败（与搜索页一致）。</summary>
    public Visibility FailureVisibility =>
        _coverFailed ? Visibility.Visible : Visibility.Collapsed;

    public Visibility CoverVisibility =>
        _cover is not null ? Visibility.Visible : Visibility.Collapsed;

    /// <summary>封面流程是否已结束（成功或失败）；用于避免重复取图。</summary>
    public bool CoverSettled => _cover is not null || _coverFailed;

    public event PropertyChangedEventHandler? PropertyChanged;

    /// <summary>取图成功：换掉占位，显示图片。</summary>
    public void SetCover(ImageSource image)
    {
        if (ReferenceEquals(_cover, image))
        {
            return;
        }

        _cover = image;
        _coverFailed = false;
        Notify(nameof(Cover));
        Notify(nameof(CoverVisibility));
        Notify(nameof(PlaceholderVisibility));
        Notify(nameof(FailureVisibility));
        Notify(nameof(CoverSettled));
    }

    /// <summary>取图 / 解码失败：显示暗红底 X（不弹框、不影响其它行）。</summary>
    public void SetCoverFailed()
    {
        if (_cover is null && _coverFailed)
        {
            return;
        }

        _cover = null;
        _coverFailed = true;
        Notify(nameof(Cover));
        Notify(nameof(CoverVisibility));
        Notify(nameof(PlaceholderVisibility));
        Notify(nameof(FailureVisibility));
        Notify(nameof(CoverSettled));
    }

    private void Notify(string propertyName)
    {
        PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(propertyName));
    }

    /// <summary>供 WPF 绑定 <c>[key]</c> 使用；缺字段返回 Undefined（显示空串）。</summary>
    public JsonElement this[string key] =>
        Row.TryGetValue(key, out JsonElement value) ? value : default;
}

/// <summary>任意 JSON 标量 → 展示文本（不排序、不改写业务字段）。</summary>
internal sealed class JsonElementDisplayConverter : IValueConverter
{
    public static JsonElementDisplayConverter Instance { get; } = new();

    public object Convert(
        object? value,
        Type targetType,
        object? parameter,
        CultureInfo culture)
    {
        if (value is not JsonElement element)
        {
            return value?.ToString() ?? string.Empty;
        }

        return element.ValueKind switch
        {
            JsonValueKind.String => element.GetString() ?? string.Empty,
            JsonValueKind.True => "true",
            JsonValueKind.False => "false",
            JsonValueKind.Number => element.GetRawText(),
            JsonValueKind.Null or JsonValueKind.Undefined => string.Empty,
            _ => element.GetRawText(),
        };
    }

    public object ConvertBack(
        object? value,
        Type targetType,
        object? parameter,
        CultureInfo culture) =>
        throw new NotSupportedException("插件页面行数据为只读。");
}
