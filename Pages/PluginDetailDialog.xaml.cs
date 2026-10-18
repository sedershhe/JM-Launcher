using System.Windows;
using System.Windows.Media;
using JML.Bridge;
using JML.Bridge.Dto;
using JML.Infrastructure;

namespace JML.Pages;

/// <summary>
/// 插件状态文案的唯一格式化入口（管理列表与详情窗共用，避免两处文案漂移）。
/// 只把 Python 的 status / reason 翻成中文，不做任何业务判断。
/// </summary>
internal static class PluginStatusDisplay
{
    /// <summary>loaded → 已加载；disabled → 已停用；其余 → 加载失败（原因）。</summary>
    public static string Format(string? status, string? reason)
    {
        return status switch
        {
            PluginStatuses.Loaded => "已加载",
            PluginStatuses.Disabled => "已停用",
            _ => string.IsNullOrWhiteSpace(reason)
                ? "加载失败"
                : $"加载失败（{LogSanitizer.Sanitize(reason)}）",
        };
    }
}

/// <summary>
/// 插件详情窗的展示模型（全部字段已转成可直接显示的文本）。
///
/// 来源边界：状态 / 说明 / dependents 来自 Python 管理状态；
/// 功能页 / 依赖 / 提供所需服务来自 manifest 只读展示（见
/// <see cref="PluginManifestReader"/>），不参与启用停用判断。
/// </summary>
internal sealed record PluginDetailInfo
{
    public string Id { get; init; } = "";

    public string Name { get; init; } = "";

    public string Version { get; init; } = "";

    /// <summary>Python 状态码：loaded / disabled / failed。</summary>
    public string Status { get; init; } = PluginStatuses.Failed;

    public string Description { get; init; } = "";

    public bool Enabled { get; init; }

    /// <summary>Python 给的失败/停用原因（原样，展示时再脱敏）。</summary>
    public string Reason { get; init; } = "";

    /// <summary>依赖本插件的插件显示名（停用确认框用）。</summary>
    public IReadOnlyList<string> Dependents { get; init; } = [];

    /// <summary>本插件声明的功能页（含标题 / id / 模板）。</summary>
    public IReadOnlyList<string> Pages { get; init; } = [];

    /// <summary>本插件依赖的插件显示名。</summary>
    public IReadOnlyList<string> Dependencies { get; init; } = [];

    public IReadOnlyList<string> Provides { get; init; } = [];

    public IReadOnlyList<string> Requires { get; init; } = [];

    /// <summary>
    /// 管理状态 DTO + manifest 展示信息 → 详情模型。
    /// <paramref name="nameLookup"/> 把插件 id 换成显示名（缺省直接用 id）。
    /// </summary>
    internal static PluginDetailInfo From(
        PluginManagementItemDto item,
        PluginManifestInfo? manifest,
        Func<string, string>? nameLookup = null)
    {
        Func<string, string> lookup = nameLookup ?? (id => id);
        return new PluginDetailInfo
        {
            Id = item.Id,
            Name = string.IsNullOrWhiteSpace(item.Name) ? item.Id : item.Name,
            Version = item.Version ?? string.Empty,
            Status = item.Status,
            Description = item.Description ?? string.Empty,
            Enabled = item.Enabled,
            Reason = item.Reason ?? string.Empty,
            Dependents = (item.Dependents ?? []).Select(lookup).Distinct(StringComparer.Ordinal).ToList(),
            Pages = (manifest?.Pages ?? []).Select(FormatPage).ToList(),
            Dependencies = (manifest?.Dependencies ?? []).Select(lookup).ToList(),
            Provides = (manifest?.Provides ?? []).ToList(),
            Requires = (manifest?.Requires ?? []).ToList(),
        };
    }

    /// <summary>功能页展示：标题（页面 id / 模板）。</summary>
    internal static string FormatPage(PluginManifestPageInfo page)
    {
        string title = string.IsNullOrWhiteSpace(page.Title) ? page.Id : page.Title;
        string template = string.IsNullOrWhiteSpace(page.Template)
            ? string.Empty
            : $" / {page.Template}";
        return $"{title}（{page.Id}{template}）";
    }
}

/// <summary>
/// 插件详情窗（缺陷 2）：双击插件管理列表的一行打开。
///
/// 行为：
/// 1. 展示名称 / id / 版本 / 状态 / 完整说明（多行、可滚动）/ 功能页 /
///    依赖插件 / 提供与所需服务 / 失败原因；
/// 2. 按当前状态只显示「启用」或「停用」中可用的那个，点击调用
///    <see cref="IPluginManageGateway.SetEnabledAsync"/>（生产即
///    <see cref="PluginManageBridge.SetPluginEnabled"/>）；
/// 3. 成功后更新本窗状态并通过 <see cref="ChangedCallback"/> 通知主列表刷新；
///    失败在窗内红字显示 <c>[错误码] 原因</c>；
/// 4. 停用前若存在依赖本插件的插件，先用可注入的 <see cref="ConfirmDisable"/>
///    列名确认（测试不弹真 MessageBox）。
/// </summary>
public partial class PluginDetailDialog : Window
{
    private readonly IPluginManageGateway _gateway;
    private PluginDetailInfo _info;
    private bool _busy;

    /// <summary>
    /// 停用确认器（参数：正文、标题；返回 true 表示继续停用）。
    /// 默认弹 MessageBox，STA 测试注入假实现。
    /// </summary>
    internal Func<string, string, bool> ConfirmDisable { get; set; }

    /// <summary>启停成功后通知主列表刷新（由 PluginListView 注入）。</summary>
    internal Func<Task>? ChangedCallback { get; set; }

    /// <summary>当前展示模型（测试断言用）。</summary>
    internal PluginDetailInfo Info => _info;

    internal bool IsEnableVisible => BtnEnable.Visibility == Visibility.Visible;

    internal bool IsDisableVisible => BtnDisable.Visibility == Visibility.Visible;

    /// <summary>设计器 / 默认实例：不主动调用 Python，只有点按钮才走网关。</summary>
    public PluginDetailDialog()
        : this(PluginManageGateway.Instance, new PluginDetailInfo { Name = "插件详情" })
    {
    }

    /// <summary>测试可注入假网关与展示模型；生产由 PluginListView 调用。</summary>
    internal PluginDetailDialog(IPluginManageGateway gateway, PluginDetailInfo info)
    {
        _gateway = gateway;
        _info = info;
        InitializeComponent();
        ConfirmDisable = ShowDisableConfirm;
        Render();
    }

    // ------------------------------------------------------------------
    // 渲染
    // ------------------------------------------------------------------

    private void Render()
    {
        TextName.Text = string.IsNullOrWhiteSpace(_info.Name) ? _info.Id : _info.Name;
        TextIdVersion.Text =
            $"id：{_info.Id}    版本：{(string.IsNullOrWhiteSpace(_info.Version) ? "—" : _info.Version)}";
        TextStatus.Text = PluginStatusDisplay.Format(_info.Status, _info.Reason);
        TextDescription.Text = string.IsNullOrWhiteSpace(_info.Description)
            ? "（无说明）"
            : _info.Description;
        TextPages.Text = FormatItems(_info.Pages);
        TextDependencies.Text = FormatItems(_info.Dependencies);
        TextServices.Text = FormatServices();

        // 失败原因只在加载失败时显示；停用原因只体现在状态行里。
        bool showReason = _info.Status == PluginStatuses.Failed
            && !string.IsNullOrWhiteSpace(_info.Reason);
        PanelFailureReason.Visibility = showReason ? Visibility.Visible : Visibility.Collapsed;
        TextFailureReason.Text = showReason ? LogSanitizer.Sanitize(_info.Reason) : string.Empty;

        bool disabled = _info.Status == PluginStatuses.Disabled;
        BtnEnable.Visibility = disabled ? Visibility.Visible : Visibility.Collapsed;
        BtnDisable.Visibility = disabled ? Visibility.Collapsed : Visibility.Visible;
        BtnEnable.IsEnabled = !_busy;
        BtnDisable.IsEnabled = !_busy;
        BtnClose.IsEnabled = !_busy;
    }

    private static string FormatItems(IReadOnlyList<string> items)
    {
        return items.Count == 0
            ? "（无）"
            : string.Join(Environment.NewLine, items.Select(item => "· " + item));
    }

    private string FormatServices()
    {
        static string Inline(IReadOnlyList<string> items) =>
            items.Count == 0 ? "（无）" : string.Join("、", items);

        return "提供：" + Inline(_info.Provides)
            + Environment.NewLine
            + "所需：" + Inline(_info.Requires);
    }

    // ------------------------------------------------------------------
    // 启用 / 停用
    // ------------------------------------------------------------------

    private async void BtnEnable_Click(object sender, RoutedEventArgs e) =>
        await SetEnabledAsync(true);

    private async void BtnDisable_Click(object sender, RoutedEventArgs e) =>
        await DisableWithConfirmationAsync();

    private void BtnClose_Click(object sender, RoutedEventArgs e) => Close();

    /// <summary>停用入口：有 dependents 时必须先确认（测试注入 ConfirmDisable）。</summary>
    internal async Task<bool> DisableWithConfirmationAsync()
    {
        if (_busy)
        {
            return false;
        }

        if (_info.Dependents.Count > 0
            && !ConfirmDisable(BuildDisableConfirmText(), "停用插件"))
        {
            return false;
        }

        return await SetEnabledAsync(false);
    }

    /// <summary>
    /// 调用 set_plugin_enabled；成功后按 Python 语义更新本窗状态并通知主列表刷新。
    /// 启用不热加载（重启后生效），因此本地状态显示为「加载失败（插件已启用，重启后加载）」，
    /// 与主列表刷新后的文案一致。
    /// </summary>
    internal async Task<bool> SetEnabledAsync(bool enabled)
    {
        if (_busy)
        {
            return false;
        }

        SetBusy(true);
        try
        {
            SetPluginEnabledResultDto result = await _gateway.SetEnabledAsync(_info.Id, enabled);
            if (!result.Ok)
            {
                ShowFailure(FormatFailure(result));
                return false;
            }

            _info = enabled
                ? _info with
                {
                    Enabled = true,
                    Status = PluginStatuses.Failed,
                    Reason = "插件已启用，重启后加载",
                }
                : _info with
                {
                    Enabled = false,
                    Status = PluginStatuses.Disabled,
                    Reason = "插件已停用",
                };

            Render();
            ShowSuccess(enabled ? "插件已启用，重启后加载。" : "插件已停用，重启后生效。");

            if (ChangedCallback is { } callback)
            {
                await callback();
            }

            return true;
        }
        catch (Exception ex)
        {
            ShowFailure($"[UNKNOWN_ERROR] {LogSanitizer.Sanitize(ex.Message)}");
            return false;
        }
        finally
        {
            SetBusy(false);
        }
    }

    /// <summary>停用确认正文：列出所有直接/间接依赖它的插件。</summary>
    internal string BuildDisableConfirmText()
    {
        string items = string.Join(
            Environment.NewLine,
            _info.Dependents.Select(name => "· " + name));
        return $"停用插件「{_info.Name}」后，以下插件会因缺少依赖而无法加载："
            + Environment.NewLine
            + items
            + Environment.NewLine
            + Environment.NewLine
            + "是否仍要停用？（重启后生效）";
    }

    private bool ShowDisableConfirm(string text, string caption) =>
        MessageBox.Show(
            this,
            text,
            caption,
            MessageBoxButton.YesNo,
            MessageBoxImage.Warning) == MessageBoxResult.Yes;

    // ------------------------------------------------------------------
    // 展示辅助
    // ------------------------------------------------------------------

    private void SetBusy(bool busy)
    {
        _busy = busy;
        BtnEnable.IsEnabled = !busy;
        BtnDisable.IsEnabled = !busy;
        BtnClose.IsEnabled = !busy;
    }

    private void ShowSuccess(string text)
    {
        TextDialogStatus.Foreground = (Brush)FindResource("DialogSuccessBrush");
        TextDialogStatus.Text = LogSanitizer.Sanitize(text);
    }

    private void ShowFailure(string text)
    {
        TextDialogStatus.Foreground = (Brush)FindResource("ColorBrushError");
        TextDialogStatus.Text = LogSanitizer.Sanitize(text);
    }

    /// <summary>失败文案固定为 <c>[错误码] 原因</c>，两段都先脱敏。</summary>
    internal static string FormatFailure(SetPluginEnabledResultDto result)
    {
        string code = string.IsNullOrWhiteSpace(result.ErrorCode)
            ? "UNKNOWN_ERROR"
            : result.ErrorCode;
        return $"[{LogSanitizer.Sanitize(code)}] {LogSanitizer.Sanitize(result.ErrorMessage)}";
    }
}
