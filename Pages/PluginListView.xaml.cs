using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using JML.Bridge;
using JML.Bridge.Dto;
using JML.Controls;
using JML.Infrastructure;

namespace JML.Pages;

/// <summary>
/// 插件管理视图（阶段 4-5：启用/停用/依赖提示全部由 Python 侧拥有）。
///
/// 数据来源：
/// 1. 首次进入 → <c>PluginManageBridge.Bootstrap</c>（装配宿主）；
/// 2. 重新扫描 → <c>ListPlugins</c>（只扫描不加载）；
/// 3. 两种情况都再取 <c>get_plugin_management_state</c> 渲染状态列。
///
/// 约束：
/// 1. 本类**不做**"是否停用 / 是否可加载"的业务判断，只渲染 Python 返回的
///    <c>status</c> / <c>reason</c> / <c>dependents</c>；
/// 2. 所有桥接调用经网关在后台线程执行，绝不阻塞 UI 线程；
/// 3. 停用前若 dependents 非空，必须弹确认框列出受影响插件（计划书 8.2/10.4）。
/// </summary>
public partial class PluginListView : UserControl
{
    /// <summary>
    /// 管理视图的一行（Python management state 的展示模型）。
    /// 字段保持 internal 仅供测试断言，业务判断不在这里发生。
    /// </summary>
    internal sealed record PluginRow(
        string Id,
        string Name,
        string Version,
        string Status,
        string Description,
        bool Enabled,
        IReadOnlyList<string> Dependents,
        string StatusCode,
        string Reason)
    {
        // ---- PCL2 资源行展示字段（Controls\JmlControls.xaml 的 JmlResourceRowTemplate） ----

        public string TitleText => Name;

        public string SubtitleText =>
            string.IsNullOrWhiteSpace(Version) ? string.Empty : "v" + Version;

        /// <summary>标签只取状态短句（"已加载" / "已停用" / "加载失败"），原因留在状态栏。</summary>
        public IReadOnlyList<string> Tags => [Status.Split('（')[0]];

        public string DescriptionText => Description;

        public IReadOnlyList<JmlResourceMetaItem> MetaItems =>
            [new JmlResourceMetaItem(JmlResourceIcons.Source, $"插件 ID：{Id}")];

        public IReadOnlyList<JmlResourceAction> Actions => [];

        public ImageSource? Cover => null;

        public Visibility LogoVisibility => Visibility.Visible;

        public Visibility PlaceholderVisibility => Visibility.Collapsed;

        public Visibility FailureVisibility => Visibility.Collapsed;

        public Visibility CoverVisibility => Visibility.Collapsed;
    }

    private readonly IPluginManageGateway _gateway;
    private bool _loadedOnce;
    private bool _busy;
    private string? _pluginsDir;
    private bool _manifestsLoaded;
    private IReadOnlyDictionary<string, PluginManifestInfo> _manifests =
        new Dictionary<string, PluginManifestInfo>(StringComparer.Ordinal);

    /// <summary>详情窗工厂（测试注入假网关；生产用真实网关 + manifest 展示信息）。</summary>
    internal Func<PluginRow, PluginDetailDialog> DetailDialogFactory { get; set; }

    /// <summary>显示详情窗（生产是模态 ShowDialog；测试替换为不阻塞的假展示）。</summary>
    internal Action<PluginDetailDialog> ShowDetailDialog { get; set; }

    public PluginListView()
        : this(PluginManageGateway.Instance)
    {
    }

    /// <summary>测试注入假网关；生产使用默认构造（Python 真调用）。</summary>
    internal PluginListView(IPluginManageGateway gateway)
    {
        _gateway = gateway;
        InitializeComponent();
        DetailDialogFactory = row => new PluginDetailDialog(_gateway, BuildDetailInfo(row));
        ShowDetailDialog = dialog => dialog.ShowDialog();
        Loaded += async (_, _) =>
        {
            if (!_loadedOnce)
            {
                _loadedOnce = true;
                await RefreshAsync(useBootstrap: true);
            }
        };
    }

    /// <summary>
    /// 停用确认器（测试注入以避免真弹 MessageBox）。
    /// 参数：(正文, 标题)，返回 true 表示继续停用。
    /// </summary>
    internal Func<string, string, bool> ConfirmDisable { get; set; } =
        (text, caption) =>
            MessageBox.Show(
                text,
                caption,
                MessageBoxButton.YesNo,
                MessageBoxImage.Warning) == MessageBoxResult.Yes;

    /// <summary>当前选中行（无选中为 null）。</summary>
    internal PluginRow? SelectedRow => GridPlugins.SelectedItem as PluginRow;

    /// <summary>最近一次渲染的行（测试/诊断用只读快照）。</summary>
    internal IReadOnlyList<PluginRow> Rows =>
        (GridPlugins.ItemsSource as IEnumerable<PluginRow>)?.ToList() ?? [];

    /// <summary>重新扫描按钮：走 list_plugins（只扫描不加载）。</summary>
    private void BtnRescan_Click(object sender, RoutedEventArgs e)
    {
        _ = RefreshAsync(useBootstrap: false);
    }

    private void BtnEnable_Click(object sender, RoutedEventArgs e)
    {
        _ = EnableSelectedAsync();
    }

    private void BtnDisable_Click(object sender, RoutedEventArgs e)
    {
        _ = DisableSelectedAsync();
    }

    private void GridPlugins_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        UpdateButtons();
    }

    /// <summary>
    /// 刷新插件列表。
    /// useBootstrap=true：调用 Bootstrap（首次加载插件宿主）；
    /// useBootstrap=false：调用 ListPlugins（重新扫描目录）。
    /// </summary>
    internal async Task RefreshAsync(bool useBootstrap)
    {
        string? pluginsDir = PluginBridge.LocatePluginsDirectory();
        if (pluginsDir is null)
        {
            GridPlugins.ItemsSource = Array.Empty<PluginRow>();
            TextStatus.Text = "未找到插件目录（plugins/）。请将插件放入程序目录下的 plugins 文件夹后点击「重新扫描」。";
            UpdateButtons();
            return;
        }

        _pluginsDir = pluginsDir;
        _manifestsLoaded = false;
        _busy = true;
        BtnRescan.IsEnabled = false;
        UpdateButtons();
        TextStatus.Text = useBootstrap ? "正在加载插件宿主…" : "正在重新扫描插件…";

        try
        {
            BootstrapResultDto result = useBootstrap
                ? await _gateway.BootstrapAsync(pluginsDir)
                : await _gateway.ListAsync(pluginsDir);
            if (!result.Ok)
            {
                GridPlugins.ItemsSource = Array.Empty<PluginRow>();
                TextStatus.Text = FormatFailure("插件操作失败", result.ErrorCode, result.ErrorMessage);
                return;
            }

            PluginManagementStateDto state = await _gateway.GetManagementStateAsync(pluginsDir);
            RenderManagementState(state);
        }
        catch (Exception ex)
        {
            GridPlugins.ItemsSource = Array.Empty<PluginRow>();
            TextStatus.Text = "插件操作失败：" + LogSanitizer.Sanitize(ex.Message);
        }
        finally
        {
            _busy = false;
            BtnRescan.IsEnabled = true;
            UpdateButtons();
        }
    }

    /// <summary>把 Python management state 渲染为表格行（不重新判断任何状态）。</summary>
    internal void RenderManagementState(PluginManagementStateDto? result)
    {
        var rows = new List<PluginRow>();
        if (result is null || !result.Ok)
        {
            GridPlugins.ItemsSource = rows;
            TextStatus.Text = result is null
                ? "插件操作失败：没有返回状态"
                : FormatFailure("读取插件状态失败", result.ErrorCode, result.ErrorMessage);
            UpdateButtons();
            return;
        }

        foreach (PluginManagementItemDto item in result.Plugins ?? [])
        {
            rows.Add(
                new PluginRow(
                    item.Id,
                    string.IsNullOrWhiteSpace(item.Name) ? item.Id : item.Name,
                    item.Version,
                    BuildStatusText(item),
                    item.Description,
                    item.Enabled,
                    item.Dependents ?? [],
                    item.Status,
                    item.Reason));
        }

        GridPlugins.ItemsSource = rows;
        int loaded = rows.Count(row => row.Status == "已加载");
        int disabled = rows.Count(row => row.Status == "已停用");
        int failed = rows.Count - loaded - disabled;
        TextStatus.Text = $"已加载 {loaded} 个插件，已停用 {disabled} 个，加载失败 {failed} 个。";
        UpdateButtons();
    }

    /// <summary>状态列文案：已加载 / 已停用 / 加载失败（原因）。</summary>
    internal static string BuildStatusText(PluginManagementItemDto item) =>
        PluginStatusDisplay.Format(item.Status, item.Reason);

    // ------------------------------------------------------------------
    // 双击行 → 插件详情窗（缺陷 2）
    // ------------------------------------------------------------------

    private void GridPlugins_MouseDoubleClick(object sender, MouseButtonEventArgs e) =>
        OpenDetailForSelectedRow();

    /// <summary>
    /// 打开当前选中行的详情窗（模态）。窗口里的启停成功回调会刷新主列表。
    /// 内部可见：STA 测试直接调用，验证“双击 → 打开”的完整映射。
    /// </summary>
    internal void OpenDetailForSelectedRow()
    {
        if (SelectedRow is not { } row)
        {
            return;
        }

        try
        {
            PluginDetailDialog dialog = DetailDialogFactory(row);
            dialog.ChangedCallback = () => RefreshAsync(useBootstrap: false);
            ShowDetailDialog(dialog);
        }
        catch (Exception ex)
        {
            // 详情窗构造/展示失败只写状态栏，不让双击把整个页面带崩。
            TextStatus.Text = "打开插件详情失败：" + LogSanitizer.Sanitize(ex.Message);
        }
    }

    /// <summary>
    /// 行 → 详情窗展示模型：状态字段来自 Python，功能页 / 依赖 / 服务来自
    /// 只读 manifest；插件 id 一律换成显示名便于阅读。
    /// </summary>
    internal PluginDetailInfo BuildDetailInfo(PluginRow row)
    {
        IReadOnlyDictionary<string, PluginManifestInfo> manifests = EnsureManifests();
        manifests.TryGetValue(row.Id, out PluginManifestInfo? manifest);
        var item = new PluginManagementItemDto
        {
            Id = row.Id,
            Name = row.Name,
            Version = row.Version,
            Description = row.Description,
            Enabled = row.Enabled,
            Status = row.StatusCode,
            Reason = row.Reason,
            Dependents = row.Dependents.ToList(),
        };

        return PluginDetailInfo.From(item, manifest, LookupName);
    }

    /// <summary>
    /// 首次打开详情窗时读取一次 manifest 展示信息（几个很小的本地 JSON）。
    /// 不做 Task.Run：列表刷新可能被同步等待（STA 测试与按钮链路），
    /// 异步续体回到 UI 线程会死锁；这里直接在调用线程读，失败返回空表。
    /// </summary>
    private IReadOnlyDictionary<string, PluginManifestInfo> EnsureManifests()
    {
        if (!_manifestsLoaded)
        {
            _manifests = PluginManifestReader.ReadAll(_pluginsDir);
            _manifestsLoaded = true;
        }

        return _manifests;
    }

    /// <summary>停用选中插件（按钮入口）。</summary>
    internal Task DisableSelectedAsync() =>
        SelectedRow is { } row ? DisableRowAsync(row) : Task.CompletedTask;

    /// <summary>停用指定行（测试直接调用；依赖确认逻辑在这里）。</summary>
    internal async Task DisableRowAsync(PluginRow row)
    {
        if (_busy || !row.Enabled || row.Status == "已停用")
        {
            return;
        }

        if (row.Dependents.Count > 0 &&
            !ConfirmDisable(BuildDisableConfirmText(row), "停用插件"))
        {
            return;
        }

        await SetPluginEnabledAsync(row.Id, enabled: false);
    }

    /// <summary>启用选中插件（按钮入口）。</summary>
    internal Task EnableSelectedAsync() =>
        SelectedRow is { } row ? EnableRowAsync(row) : Task.CompletedTask;

    /// <summary>启用指定行（不热加载，Python 侧提示重启后加载）。</summary>
    internal async Task EnableRowAsync(PluginRow row)
    {
        if (_busy || row.Status != "已停用")
        {
            return;
        }

        await SetPluginEnabledAsync(row.Id, enabled: true);
    }

    /// <summary>
    /// 调用 <c>set_plugin_enabled</c> 并刷新列表；失败把 <c>[错误码] 原因</c>
    /// 写到状态栏，不向 UI 抛异常。
    /// </summary>
    internal async Task<bool> SetPluginEnabledAsync(string pluginId, bool enabled)
    {
        if (_busy)
        {
            return false;
        }

        _busy = true;
        BtnRescan.IsEnabled = false;
        UpdateButtons();
        string action = enabled ? "启用" : "停用";

        try
        {
            SetPluginEnabledResultDto result =
                await _gateway.SetEnabledAsync(pluginId, enabled);
            if (!result.Ok)
            {
                TextStatus.Text = FormatFailure(
                    $"插件{action}失败",
                    result.ErrorCode,
                    result.ErrorMessage);
                return false;
            }

            await RefreshAsync(useBootstrap: false);
            TextStatus.Text = enabled
                ? "插件已启用，重启后加载。"
                : "插件已停用，重启后生效（依赖它的插件会被拒绝加载）。";
            return true;
        }
        catch (Exception ex)
        {
            TextStatus.Text =
                $"插件{action}失败：" + LogSanitizer.Sanitize(ex.Message);
            return false;
        }
        finally
        {
            _busy = false;
            BtnRescan.IsEnabled = true;
            UpdateButtons();
        }
    }

    /// <summary>停用确认框正文：列出所有直接/间接依赖它的插件。</summary>
    internal string BuildDisableConfirmText(PluginRow row)
    {
        List<string> dependents = row.Dependents
            .Select(id => LookupName(id))
            .Distinct(StringComparer.Ordinal)
            .ToList();
        string items = string.Join(
            Environment.NewLine,
            dependents.Select(name => "· " + name));
        return $"停用插件「{row.Name}」后，以下插件会因缺少依赖而无法加载："
            + Environment.NewLine
            + items
            + Environment.NewLine
            + Environment.NewLine
            + "是否仍要停用？（重启后生效）";
    }

    private string LookupName(string pluginId)
    {
        foreach (PluginRow row in Rows)
        {
            if (string.Equals(row.Id, pluginId, StringComparison.Ordinal))
            {
                return row.Name;
            }
        }

        return pluginId;
    }

    private void UpdateButtons()
    {
        PluginRow? row = SelectedRow;
        BtnEnable.IsEnabled = !_busy && row is { Status: "已停用" };
        BtnDisable.IsEnabled = !_busy && row is { Enabled: true, Status: not "已停用" };
    }

    private static string FormatFailure(string action, string errorCode, string message)
    {
        return $"{action}：[{LogSanitizer.Sanitize(errorCode)}] "
            + LogSanitizer.Sanitize(message);
    }
}
