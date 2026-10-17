using System.IO;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using JML.Bridge;
using JML.Bridge.Dto;
using JML.Infrastructure;
using Microsoft.Win32;

namespace JML.Pages;

/// <summary>
/// 设置页（阶段 3）：账号状态 / 账号修改窗口入口 / 已保存账号登录 / 下载目录记忆。
///
/// 纪律：
/// - 账号凭据不经过本页持久化，本页只显示打码用户名；
/// - 有保存凭据但未登录时显示「已保存账号：ab***cd（未登录）」并给出免密登录按钮；
/// - 启动时只查询登录态与本地“是否已保存凭据”，不静默自动登录（避免无谓网络与失败弹框），
///   登录由用户点击或后续插件按需触发；
/// - 打开 <see cref="AccountDialog"/> 是模态，关闭后刷新账号状态；
/// - 下载目录只写入 <see cref="AppSettings.DownloadDir"/>（纯路径），不存凭据。
/// </summary>
public partial class SettingsPage : UserControl
{
    private readonly AppSettings _settings;
    private readonly ISavedCredentialStore _store;
    private readonly IAccountGateway _gateway;
    private bool _busy;
    private bool _hasSaved;
    private bool _loggedIn;

    public SettingsPage()
        : this(new CredentialStoreAdapter(), AccountGateway.Instance)
    {
    }

    /// <summary>测试可注入假凭据存储与假账号网关；生产使用默认实例。</summary>
    internal SettingsPage(ISavedCredentialStore store, IAccountGateway gateway)
    {
        _store = store;
        _gateway = gateway;
        InitializeComponent();
        _settings = AppSettings.Load();
        ApplyDownloadDirectory();
        Loaded += OnLoaded;
    }

    /// <summary>
    /// 账号状态文案（测试锚点）：
    /// 已登录只展示打码用户名；未登录但有保存凭据时展示「已保存账号：ab***cd（未登录）」；
    /// 都没有时固定“未登录”。
    /// </summary>
    public static string BuildAccountStatusText(
        bool loggedIn,
        string? usernameMasked,
        string? savedUsername = null)
    {
        if (loggedIn)
        {
            // 即使上游误传明文，这里也只输出打码形态；MaskUsername 对已打码值幂等。
            string masked = AccountBridge.MaskUsername((usernameMasked ?? string.Empty).Trim());
            return string.IsNullOrEmpty(masked)
                ? "已登录：***"
                : $"已登录：{masked}";
        }

        if (!string.IsNullOrWhiteSpace(savedUsername))
        {
            string masked = AccountBridge.MaskUsername(savedUsername.Trim());
            return $"已保存账号：{(string.IsNullOrEmpty(masked) ? "***" : masked)}（未登录）";
        }

        return "未登录";
    }

    private async void OnLoaded(object sender, RoutedEventArgs e)
    {
        ApplyDownloadDirectory();
        await RefreshAccountStatusAsync();
    }

    /// <summary>从 Python 查一次账号状态；失败只显示可读错误，不影响页面其它功能。</summary>
    internal async Task RefreshAccountStatusAsync()
    {
        try
        {
            AccountStatusDto status = await _gateway.Status();
            if (!status.Ok)
            {
                // 状态读不到时仍可判断本地是否存过凭据，从而决定是否给免密登录入口；
                // 但绝不在这里自动登录。
                _hasSaved = SafeExists();
                TextAccountStatus.Foreground = (Brush)FindResource("ColorBrushError");
                TextAccountStatus.Text =
                    $"账号状态读取失败：[{LogSanitizer.Sanitize(status.ErrorCode)}] " +
                    LogSanitizer.Sanitize(status.ErrorMessage);
                UpdateSavedLoginButton();
                return;
            }

            ApplyAccountStatus(status);
        }
        catch (Exception ex)
        {
            _hasSaved = SafeExists();
            TextAccountStatus.Foreground = (Brush)FindResource("ColorBrushError");
            TextAccountStatus.Text =
                $"账号状态读取失败：{LogSanitizer.Sanitize(ex.GetType().Name)}";
            UpdateSavedLoginButton();
        }
    }

    internal void ApplyAccountStatus(AccountStatusDto status)
    {
        _loggedIn = status.LoggedIn;
        _hasSaved = SafeExists();

        string savedUsername = "";
        if (_hasSaved && !status.LoggedIn)
        {
            savedUsername = TryLoadSavedUsername();
        }

        TextAccountStatus.Foreground = (Brush)FindResource(
            status.LoggedIn ? "ColorBrush3" : "ColorBrush1");
        TextAccountStatus.Text = BuildAccountStatusText(
            status.LoggedIn,
            status.UsernameMasked,
            savedUsername);
        UpdateSavedLoginButton();
    }

    /// <summary>
    /// 读取已保存账号的用户名用于打码展示；密码随元组在方法结束时丢弃，
    /// 不写字段、不写日志、不拼进任何界面文本。
    /// </summary>
    private string TryLoadSavedUsername()
    {
        try
        {
            (string Username, string Password)? saved = _store.Load();
            return saved?.Username ?? "";
        }
        catch
        {
            return "";
        }
    }

    private bool SafeExists()
    {
        try
        {
            return _store.Exists();
        }
        catch
        {
            return false;
        }
    }

    private void UpdateSavedLoginButton()
    {
        bool visible = _hasSaved && !_loggedIn;
        BtnLoginSaved.Visibility = visible ? Visibility.Visible : Visibility.Collapsed;
        BtnLoginSaved.IsEnabled = visible && !_busy;
    }

    private async void BtnLoginSaved_Click(object sender, RoutedEventArgs e) =>
        await LoginSavedAsync();

    /// <summary>
    /// 用已保存账号登录：Load → SetCredentials → Login（由 <see cref="SavedAccountLoginService"/> 固定）。
    /// 仅由用户点击触发；启动路径不会调用。失败文案已在服务内脱敏，不含密码原文。
    /// </summary>
    internal async Task LoginSavedAsync()
    {
        if (_busy)
        {
            return;
        }

        SetBusy(true);
        try
        {
            SavedLoginOutcome outcome =
                await new SavedAccountLoginService(_store, _gateway).LoginAsync();

            if (!outcome.Ok)
            {
                TextAccountStatus.Foreground = (Brush)FindResource("ColorBrushError");
                TextAccountStatus.Text = $"[{outcome.ErrorCode}] {outcome.ErrorMessage}";
                return;
            }

            ApplyAccountStatus(new AccountStatusDto
            {
                Ok = true,
                LoggedIn = true,
                UsernameMasked = outcome.UsernameMasked,
            });
        }
        catch (Exception ex)
        {
            TextAccountStatus.Foreground = (Brush)FindResource("ColorBrushError");
            TextAccountStatus.Text =
                $"登录操作异常：{LogSanitizer.Sanitize(ex.GetType().Name)}";
        }
        finally
        {
            SetBusy(false);
        }
    }

    private void SetBusy(bool busy)
    {
        _busy = busy;
        BtnAccount.IsEnabled = !busy;
        UpdateSavedLoginButton();
    }

    private void ApplyDownloadDirectory()
    {
        InputDownloadDir.Text = string.IsNullOrWhiteSpace(_settings.DownloadDir)
            ? Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory)
            : _settings.DownloadDir;
    }

    private void BtnAccount_Click(object sender, RoutedEventArgs e)
    {
        var dialog = new AccountDialog();
        Window? owner = Window.GetWindow(this);
        if (owner is not null)
        {
            dialog.Owner = owner;
        }

        // 模态窗口：关闭后无条件刷新，保证保存 / 清除 / 登出都能反映到状态文字。
        dialog.ShowDialog();
        _ = RefreshAccountStatusAsync();
    }

    private void BtnBrowseDir_Click(object sender, RoutedEventArgs e)
    {
        var dialog = new OpenFolderDialog
        {
            Title = "选择下载保存目录",
            Multiselect = false,
        };

        if (Directory.Exists(InputDownloadDir.Text))
        {
            dialog.InitialDirectory = InputDownloadDir.Text;
        }

        if (dialog.ShowDialog() != true || string.IsNullOrWhiteSpace(dialog.FolderName))
        {
            return;
        }

        _settings.DownloadDir = dialog.FolderName;
        _settings.Save();
        InputDownloadDir.Text = dialog.FolderName;
    }
}
