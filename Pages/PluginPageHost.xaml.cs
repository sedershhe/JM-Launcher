using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Data;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Media.Imaging;
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
    /// </summary>
    internal Func<string, bool>? BrowserOpener { get; set; }

    /// <summary>
    /// 页面动作 <c>intent</c> 的转发器（阶段 4-4）：生产默认由所属
    /// <see cref="MainWindow"/> 实现（<c>Window.GetWindow(this)</c> 解析）；
    /// STA 测试注入假实现断言"只转发、不做业务"。
    /// </summary>
    internal IUiIntentHandler? IntentHandler { get; set; }

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

    /// <summary>设计器 / 默认为空页（未传 plugin_id/page_id 时不发任何调用）。</summary>
    public PluginPageHost()
        : this(string.Empty, string.Empty, string.Empty, null)
    {
    }

    /// <summary>按插件 + 页面构造宿主（生产入口）。</summary>
    public PluginPageHost(string pluginId, string pageId, string title)
        : this(pluginId, pageId, title, null)
    {
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
        PluginRowView? row = element.DataContext as PluginRowView;
        var payload = new Dictionary<string, object?>
        {
            ["row"] = row?.Row,
            ["row_index"] = row?.Index ?? -1,
            ["page"] = _listPage,
        };

        await RunActionAsync(actionId, payload);
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

        BtnActionOpenBrowser.Visibility = string.IsNullOrEmpty(_actionUrl)
            ? Visibility.Collapsed
            : Visibility.Visible;
        BtnActionOpenBrowser.IsEnabled = !string.IsNullOrEmpty(_actionUrl);

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

    /// <summary>
    /// 「在浏览器中打开」：**只转发** Python 给出的网址，不在 C# 里判断业务状态。
    /// 生产用 <see cref="Process.Start(ProcessStartInfo)"/>（UseShellExecute=true）
    /// 唤起系统默认浏览器；STA 测试注入 <see cref="BrowserOpener"/> 断言转发。
    /// </summary>
    private void BtnActionOpenBrowser_Click(object sender, RoutedEventArgs e)
    {
        string url = _actionUrl;
        if (string.IsNullOrWhiteSpace(url))
        {
            return;
        }

        try
        {
            if (BrowserOpener is { } opener)
            {
                _ = opener(url);
                return;
            }

            Process.Start(new ProcessStartInfo(url) { UseShellExecute = true });
        }
        catch (Exception ex)
        {
            ShowError("UNKNOWN_ERROR", LogSanitizer.Sanitize(ex.Message));
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
/// 行数据视图：DataGrid 的绑定源。保留插件原始行（<see cref="Row"/>）供动作
/// 回传，展示时用索引器取值（列 key 由 Python 决定）。
/// </summary>
internal sealed class PluginRowView
{
    public PluginRowView(Dictionary<string, JsonElement> row, int index)
    {
        Row = row;
        Index = index;
    }

    /// <summary>插件原始行（回传给 plugin_page_action 的 row 字段）。</summary>
    public Dictionary<string, JsonElement> Row { get; }

    /// <summary>行下标（0 起）。</summary>
    public int Index { get; }

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
