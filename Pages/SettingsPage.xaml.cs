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
/// 设置页（阶段 3）：账号状态 / 账号修改窗口入口 / 下载目录记忆。
///
/// 纪律：
/// - 账号凭据不经过本页，本页只显示 Python 返回的打码用户名；
/// - 打开 <see cref="AccountDialog"/> 是模态，关闭后刷新账号状态；
/// - 下载目录只写入 <see cref="AppSettings.DownloadDir"/>（纯路径），不存凭据。
/// </summary>
public partial class SettingsPage : UserControl
{
    private readonly AppSettings _settings;

    public SettingsPage()
    {
        InitializeComponent();
        _settings = AppSettings.Load();
        ApplyDownloadDirectory();
        Loaded += OnLoaded;
    }

    /// <summary>
    /// 账号状态文案（测试锚点）：未登录固定“未登录”，已登录必须只展示打码用户名。
    /// </summary>
    public static string BuildAccountStatusText(bool loggedIn, string? usernameMasked)
    {
        if (!loggedIn)
        {
            return "未登录";
        }

        // 即使上游误传明文，这里也只输出打码形态；MaskUsername 对已打码值幂等。
        string masked = AccountBridge.MaskUsername((usernameMasked ?? string.Empty).Trim());
        return string.IsNullOrEmpty(masked)
            ? "已登录：***"
            : $"已登录：{masked}";
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
            AccountStatusDto status = await AccountBridge.Status();
            if (!status.Ok)
            {
                TextAccountStatus.Foreground = (Brush)FindResource("ColorBrushError");
                TextAccountStatus.Text =
                    $"账号状态读取失败：[{LogSanitizer.Sanitize(status.ErrorCode)}] " +
                    LogSanitizer.Sanitize(status.ErrorMessage);
                return;
            }

            ApplyAccountStatus(status);
        }
        catch (Exception ex)
        {
            TextAccountStatus.Foreground = (Brush)FindResource("ColorBrushError");
            TextAccountStatus.Text =
                $"账号状态读取失败：{LogSanitizer.Sanitize(ex.GetType().Name)}";
        }
    }

    internal void ApplyAccountStatus(AccountStatusDto status)
    {
        TextAccountStatus.Foreground = (Brush)FindResource(
            status.LoggedIn ? "ColorBrush3" : "ColorBrush1");
        TextAccountStatus.Text = BuildAccountStatusText(
            status.LoggedIn,
            status.UsernameMasked);
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
