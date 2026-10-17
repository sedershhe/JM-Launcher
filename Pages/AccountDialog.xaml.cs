using System.Windows;
using System.Windows.Media;
using JML.Bridge;
using JML.Bridge.Dto;
using JML.Infrastructure;

namespace JML.Pages;

/// <summary>
/// 已保存凭据的只读抽象。
///
/// 生产由 <see cref="CredentialStoreAdapter"/> 转发到真实 <see cref="CredentialStore"/>；
/// 测试注入假实现，不碰真实 Windows 凭据管理器与 DPAPI 文件。
/// </summary>
internal interface ISavedCredentialStore
{
    bool Exists();

    (string Username, string Password)? Load();

    bool Save(string username, string password);

    bool Delete();

    string LastError { get; }
}

/// <summary>把真实 <see cref="CredentialStore"/> 适配成可注入接口；不复制任何存储逻辑。</summary>
internal sealed class CredentialStoreAdapter : ISavedCredentialStore
{
    private readonly CredentialStore _store;

    public CredentialStoreAdapter(CredentialStore? store = null) =>
        _store = store ?? CredentialStore.Default;

    public bool Exists() => _store.Exists();

    public (string Username, string Password)? Load() => _store.Load();

    public bool Save(string username, string password) => _store.Save(username, password);

    public bool Delete() => _store.Delete();

    public string LastError => _store.LastError;
}

/// <summary>
/// 「使用已保存账号登录」的一次结果。
/// 失败文案已在服务内部完成打码，<see cref="ErrorMessage"/> 不含密码原文。
/// </summary>
internal sealed record SavedLoginOutcome(
    bool Ok,
    string UsernameMasked,
    string ErrorCode,
    string ErrorMessage);

/// <summary>
/// 已保存账号登录流程（重启后免重输密码）。
///
/// 固定顺序：<see cref="ISavedCredentialStore.Load"/> →
/// <see cref="IAccountGateway.SetCredentials"/> → <see cref="IAccountGateway.Login"/>。
/// 密码只存在于本方法的局部变量，调用结束即丢弃，不写入字段、日志或界面文案。
/// </summary>
internal sealed class SavedAccountLoginService
{
    private readonly ISavedCredentialStore _store;
    private readonly IAccountGateway _gateway;

    public SavedAccountLoginService(ISavedCredentialStore store, IAccountGateway gateway)
    {
        _store = store;
        _gateway = gateway;
    }

    public async Task<SavedLoginOutcome> LoginAsync()
    {
        (string Username, string Password)? saved;
        try
        {
            saved = _store.Load();
        }
        catch (Exception ex)
        {
            return Failure("READ_FAILED", $"读取已保存账号失败：{ex.GetType().Name}");
        }

        if (saved is null)
        {
            string message = string.IsNullOrWhiteSpace(_store.LastError)
                ? "未读取到已保存账号，请重新输入用户名和密码。"
                : _store.LastError;
            return Failure("NO_SAVED_CREDENTIAL", message);
        }

        string username = saved.Value.Username;
        string password = saved.Value.Password;
        saved = null;
        try
        {
            AccountStatusDto setResult = await _gateway.SetCredentials(username, password);
            if (!setResult.Ok)
            {
                return FailureFrom(setResult, username, password);
            }

            AccountStatusDto loginResult = await _gateway.Login();
            if (!loginResult.Ok)
            {
                return FailureFrom(loginResult, username, password);
            }

            string masked = string.IsNullOrWhiteSpace(loginResult.UsernameMasked)
                ? AccountBridge.MaskUsername(username)
                : AccountBridge.MaskUsername(loginResult.UsernameMasked);
            return new SavedLoginOutcome(true, masked, "", "");
        }
        catch (Exception ex)
        {
            // 极端异常也只保留类型名，并把已知密码/用户名从文案里抹掉。
            return Failure(
                "LOGIN_EXCEPTION",
                AccountDialog.ScrubKnownSecrets(
                    $"登录操作异常：{ex.GetType().Name}", username, password));
        }
        finally
        {
            // 立即丢弃本地明文引用；本类不把它缓存到任何字段。
            password = string.Empty;
        }
    }

    private static SavedLoginOutcome Failure(string code, string message) =>
        new(false, "", LogSanitizer.Sanitize(code), LogSanitizer.Sanitize(message));

    private static SavedLoginOutcome FailureFrom(
        AccountStatusDto status,
        string username,
        string password) =>
        new(
            false,
            "",
            LogSanitizer.Sanitize(
                string.IsNullOrWhiteSpace(status.ErrorCode) ? "UNKNOWN_ERROR" : status.ErrorCode),
            AccountDialog.ScrubKnownSecrets(status.ErrorMessage, username, password));
}

/// <summary>
/// 账号录入 / 修改窗口（阶段 3）。
///
/// 流程（契约 §九.12）：
/// 1. 用户点「保存并登录」→ <see cref="CredentialStore.Save"/> 安全落盘；
/// 2. → <see cref="AccountBridge.SetCredentials"/> 把凭据写进 Python 进程内存；
/// 3. → <see cref="AccountBridge.Login"/> 登录，按返回的 error_code / username_masked 展示。
///
/// 另一个入口「使用已保存账号登录」专门解决应用重启后 Python 内存凭据为空的问题：
/// 从 <see cref="CredentialStore"/> 读出已保存凭据（不要求用户重新输入密码），
/// 依次写入 Python 内存并登录；全程密码不进入字段、日志或界面。
///
/// 安全底线：
/// - 密码只存在于 PasswordBox 与本次局部变量，不回显、不写配置、不进日志；
/// - 打开窗口时只读取已保存账号并展示打码用户名，不修改原凭据；
/// - 只有「保存并登录」会写入新凭据；「取消」直接关闭；
/// - 错误文案展示前再脱敏一次，绝不把密码带进界面。
/// </summary>
public partial class AccountDialog : Window
{
    private const string SavedHint =
        "已保存账号（密码不再显示）。可直接重新登录；输入新密码可改密码，清空用户名可更换账号。";

    private const string NoSavedHint =
        "未保存账号。填写用户名和密码后点「保存并登录」；取消不会改动任何已保存凭据。";

    /// <summary>打开窗口时的初始显示状态（与控件写入解耦，便于离线断言）。</summary>
    internal sealed record InitialState(
        bool HasSaved,
        string UsernameText,
        string Hint,
        bool ClearEnabled,
        bool SavedLoginVisible,
        string? Error);

    /// <summary>
    /// 根据已保存凭据/读取错误计算初始显示：
    /// 有保存 → 打码用户名 + “密码不再显示” + 显示免密登录按钮；
    /// 无保存 → 空用户名 + “未保存账号” + 隐藏免密登录按钮。
    /// </summary>
    internal static InitialState BuildInitialState(
        (string Username, string Password)? saved,
        string? lastError)
    {
        if (saved is not null)
        {
            return new InitialState(
                HasSaved: true,
                UsernameText: AccountBridge.MaskUsername(saved.Value.Username),
                Hint: SavedHint,
                ClearEnabled: true,
                SavedLoginVisible: true,
                Error: null);
        }

        return new InitialState(
            HasSaved: false,
            UsernameText: "",
            Hint: NoSavedHint,
            ClearEnabled: false,
            SavedLoginVisible: false,
            Error: string.IsNullOrEmpty(lastError) ? null : lastError);
    }

    private readonly ISavedCredentialStore _store;
    private readonly IAccountGateway _gateway;
    private bool _busy;
    private bool _hasSaved;
    private string _savedMasked = "";

    /// <summary>清除前的确认回调；默认弹 MessageBox，测试可替换。</summary>
    internal Func<bool> ConfirmClear { get; set; } = () => false;

    public AccountDialog()
        : this(new CredentialStoreAdapter(), AccountGateway.Instance)
    {
    }

    /// <summary>测试可注入独立存储与假账号网关；生产使用默认实例。</summary>
    internal AccountDialog(ISavedCredentialStore store, IAccountGateway gateway)
    {
        _store = store;
        _gateway = gateway;
        InitializeComponent();
        ConfirmClear = ConfirmClearWithDialog;
        LoadSavedState();
    }

    /// <summary>
    /// 仅做展示脱敏；不读取失败 DTO 之外的任何状态。
    /// 供测试断言：含 password=... 的错误文本不会把原值带到界面。
    /// </summary>
    internal static string SanitizeForDisplay(string? text) =>
        LogSanitizer.Sanitize(text);

    /// <summary>红字失败文案：固定为 <c>[error_code] error_message</c>，两段都先脱敏。</summary>
    internal static string FormatFailure(AccountStatusDto status)
    {
        string code = string.IsNullOrWhiteSpace(status.ErrorCode)
            ? "UNKNOWN_ERROR"
            : status.ErrorCode;
        return $"[{SanitizeForDisplay(code)}] {SanitizeForDisplay(status.ErrorMessage)}";
    }

    // ------------------------------------------------------------------
    // 打开窗口：只读展示，不改原凭据
    // ------------------------------------------------------------------

    private void LoadSavedState()
    {
        try
        {
            // 先判断是否存在；只有存在时才解密读取（用于展示打码用户名）。
            bool exists = _store.Exists();
            (string Username, string Password)? saved = exists ? _store.Load() : null;
            InitialState state = BuildInitialState(saved, _store.LastError);

            _hasSaved = state.HasSaved;
            _savedMasked = state.UsernameText;
            InputUsername.Text = state.UsernameText;
            TextSavedHint.Text = state.Hint;
            BtnClearSaved.IsEnabled = state.ClearEnabled;
            BtnLoginSaved.Visibility = state.SavedLoginVisible
                ? Visibility.Visible
                : Visibility.Collapsed;
            BtnLoginSaved.IsEnabled = state.SavedLoginVisible;

            if (state.Error is not null)
            {
                ShowError(state.Error);
            }
        }
        catch (Exception ex)
        {
            BtnLoginSaved.Visibility = Visibility.Collapsed;
            ShowError($"读取已保存账号失败：{ex.GetType().Name}");
        }
        finally
        {
            InputPassword.Clear();
        }
    }

    // ------------------------------------------------------------------
    // 保存并登录
    // ------------------------------------------------------------------

    private async void BtnSaveLogin_Click(object sender, RoutedEventArgs e)
    {
        if (_busy)
        {
            return;
        }

        if (!TryResolveInput(out string username, out string password, out bool mustSave, out string error))
        {
            ShowError(error);
            return;
        }

        SetBusy(true);
        try
        {
            if (mustSave)
            {
                if (!_store.Save(username, password))
                {
                    ShowError($"保存账号失败：{_store.LastError}", username, password);
                    return;
                }

                _hasSaved = true;
                _savedMasked = AccountBridge.MaskUsername(username);
                TextSavedHint.Text = SavedHint;
                InputUsername.Text = _savedMasked;
                BtnClearSaved.IsEnabled = true;
                BtnLoginSaved.Visibility = Visibility.Visible;
            }

            AccountStatusDto setResult = await _gateway.SetCredentials(username, password);
            if (!setResult.Ok)
            {
                ShowError($"凭据已保存，但写入登录凭据失败：{FormatFailure(setResult)}", username, password);
                return;
            }

            AccountStatusDto loginResult = await _gateway.Login();
            if (!loginResult.Ok)
            {
                ShowError(FormatFailure(loginResult), username, password);
                return;
            }

            string masked = string.IsNullOrWhiteSpace(loginResult.UsernameMasked)
                ? _savedMasked
                : AccountBridge.MaskUsername(loginResult.UsernameMasked);
            _savedMasked = masked;
            _hasSaved = true;
            InputUsername.Text = masked;
            TextSavedHint.Text = SavedHint;
            ShowSuccess($"登录成功：{masked}");
        }
        catch (Exception ex)
        {
            // 正常情况下 Bridge 已收口；这里只保留类型名并脱敏，防止极端异常把凭据带进界面。
            ShowError($"登录操作异常：{ex.GetType().Name}", username, password);
        }
        finally
        {
            InputPassword.Clear();
            SetBusy(false);
        }
    }

    // ------------------------------------------------------------------
    // 使用已保存账号登录（重启后免重输密码）
    // ------------------------------------------------------------------

    private async void BtnLoginSaved_Click(object sender, RoutedEventArgs e) =>
        await LoginWithSavedAsync();

    /// <summary>
    /// 从已保存凭据登录：Load → SetCredentials → Login。
    /// 不读取密码框，也不把密码缓存到字段；测试可直接 await。
    /// </summary>
    internal async Task LoginWithSavedAsync()
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
                ShowError($"[{outcome.ErrorCode}] {outcome.ErrorMessage}");
                return;
            }

            _hasSaved = true;
            _savedMasked = outcome.UsernameMasked;
            InputUsername.Text = outcome.UsernameMasked;
            TextSavedHint.Text = SavedHint;
            BtnClearSaved.IsEnabled = true;
            BtnLoginSaved.Visibility = Visibility.Visible;
            ShowSuccess($"登录成功：{outcome.UsernameMasked}");
        }
        catch (Exception ex)
        {
            ShowError($"登录操作异常：{ex.GetType().Name}");
        }
        finally
        {
            InputPassword.Clear();
            SetBusy(false);
        }
    }

    /// <summary>
    /// 解析输入：
    /// - 用户名仍是已保存的掩码且密码为空 → 用已保存凭据重新登录（不再写盘）；
    /// - 用户名仍是掩码但输入了新密码 → 沿用已保存的真实用户名，保存新密码；
    /// - 其它情况 → 用户输入的账号和密码都作为新凭据保存。
    /// </summary>
    private bool TryResolveInput(
        out string username,
        out string password,
        out bool mustSave,
        out string error)
    {
        username = InputUsername.Text.Trim();
        password = InputPassword.Password;
        mustSave = true;
        error = "";

        if (username.Length == 0)
        {
            error = "请输入用户名。";
            return false;
        }

        if (_hasSaved && string.Equals(username, _savedMasked, StringComparison.Ordinal))
        {
            (string Username, string Password)? saved = _store.Load();
            if (saved is null)
            {
                error = string.IsNullOrEmpty(_store.LastError)
                    ? "未读取到已保存账号，请重新输入用户名和密码。"
                    : _store.LastError;
                return false;
            }

            username = saved.Value.Username;
            if (password.Length == 0)
            {
                password = saved.Value.Password;
                mustSave = false;
            }

            return true;
        }

        if (password.Length == 0)
        {
            error = "请输入密码。";
            return false;
        }

        return true;
    }

    // ------------------------------------------------------------------
    // 清除 / 登出
    // ------------------------------------------------------------------

    private async void BtnClearSaved_Click(object sender, RoutedEventArgs e)
    {
        if (_busy || !ConfirmClear())
        {
            return;
        }

        await ClearSavedAsync();
    }

    /// <summary>执行清除（确认步骤由调用方完成）；测试可直接 await。</summary>
    internal async Task ClearSavedAsync()
    {
        if (_busy)
        {
            return;
        }

        SetBusy(true);
        try
        {
            bool storeDeleted = _store.Delete();
            AccountStatusDto cleared = await _gateway.Clear();

            if (storeDeleted && cleared.Ok)
            {
                ApplyClearedState();
                ShowSuccess("已清除已保存账号并退出登录。");
                return;
            }

            if (!storeDeleted && cleared.Ok)
            {
                ShowError($"已退出登录，但清除已保存账号失败：{_store.LastError}");
                return;
            }

            if (storeDeleted && !cleared.Ok)
            {
                ApplyClearedState();
                ShowError($"已删除已保存账号，但退出登录失败：{FormatFailure(cleared)}");
                return;
            }

            ShowError(
                $"清除已保存账号失败：{_store.LastError}；退出登录失败：{FormatFailure(cleared)}");
        }
        catch (Exception ex)
        {
            ShowError($"清除账号异常：{ex.GetType().Name}");
        }
        finally
        {
            InputPassword.Clear();
            SetBusy(false);
        }
    }

    /// <summary>清空界面上的已保存账号痕迹，并隐藏免密登录按钮。</summary>
    private void ApplyClearedState()
    {
        _hasSaved = false;
        _savedMasked = "";
        InputUsername.Clear();
        InputPassword.Clear();
        TextSavedHint.Text = NoSavedHint;
        BtnClearSaved.IsEnabled = false;
        BtnLoginSaved.Visibility = Visibility.Collapsed;
    }

    private async void BtnLogout_Click(object sender, RoutedEventArgs e) =>
        await LogoutAsync();

    /// <summary>执行登出；已保存凭据保留，因此免密登录按钮仍可用。</summary>
    internal async Task LogoutAsync()
    {
        if (_busy)
        {
            return;
        }

        SetBusy(true);
        try
        {
            AccountStatusDto result = await _gateway.Logout();
            if (!result.Ok)
            {
                ShowError(FormatFailure(result));
                return;
            }

            InputPassword.Clear();
            BtnLoginSaved.Visibility = _hasSaved ? Visibility.Visible : Visibility.Collapsed;
            ShowSuccess(_hasSaved ? "已退出登录（已保存账号仍保留）。" : "已退出登录。");
        }
        catch (Exception ex)
        {
            ShowError($"退出登录异常：{ex.GetType().Name}");
        }
        finally
        {
            InputPassword.Clear();
            SetBusy(false);
        }
    }

    private void BtnCancel_Click(object sender, RoutedEventArgs e)
    {
        // 不写入任何内容，直接丢弃本次输入。
        Close();
    }

    // ------------------------------------------------------------------
    // 展示辅助
    // ------------------------------------------------------------------

    private bool ConfirmClearWithDialog() =>
        MessageBox.Show(
            this,
            "确定清除已保存的账号吗？\nPython 登录态也会一并清除，原凭据不可恢复。",
            "清除已保存账号",
            MessageBoxButton.YesNo,
            MessageBoxImage.Warning) == MessageBoxResult.Yes;

    private void SetBusy(bool busy)
    {
        _busy = busy;
        BtnSaveLogin.IsEnabled = !busy;
        BtnCancel.IsEnabled = !busy;
        BtnClearSaved.IsEnabled = !busy && _hasSaved;
        BtnLoginSaved.Visibility = _hasSaved ? Visibility.Visible : Visibility.Collapsed;
        BtnLoginSaved.IsEnabled = !busy && _hasSaved;
        BtnLogout.IsEnabled = !busy;
    }

    private void ShowSuccess(string text)
    {
        TextAccountStatus.Foreground = (Brush)FindResource("AccountSuccessBrush");
        TextAccountStatus.Text = SanitizeForDisplay(text);
    }

    private void ShowError(string text, string? username = null, string? password = null)
    {
        TextAccountStatus.Foreground = (Brush)FindResource("ColorBrushError");
        TextAccountStatus.Text = ScrubKnownSecrets(text, username, password);
    }

    /// <summary>
    /// 先对本次实际使用的账号/密码做字面打码，再走通用 <see cref="LogSanitizer"/>。
    /// 即使第三方异常把裸密码带进文案，也不会显示到界面。
    /// </summary>
    internal static string ScrubKnownSecrets(string? text, string? username, string? password)
    {
        string safe = text ?? "";
        if (!string.IsNullOrEmpty(password) && password.Length >= 3)
        {
            safe = safe.Replace(password, "***", StringComparison.Ordinal);
        }

        if (!string.IsNullOrEmpty(username) && username.Length >= 3)
        {
            safe = safe.Replace(username, "***", StringComparison.Ordinal);
        }

        return LogSanitizer.Sanitize(safe);
    }
}
