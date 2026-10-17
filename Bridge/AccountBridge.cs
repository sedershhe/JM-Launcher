using JML.Bridge.Dto;

namespace JML.Bridge;

/// <summary>
/// 账号桥接（阶段 3）：把 Python 的 <c>account_*</c> 入口包装成异步 DTO。
///
/// 纪律（契约 §九.12、§九.13、§十）：
/// - 全部调用走 <see cref="PythonGateway.CallJson{T}"/>，C# 不复制登录业务；
/// - 网络登录可能耗时，统一在 <see cref="Task.Run(Func{Task})"/> 上执行；
/// - 失败收口成 DTO，不把异常抛给 UI；
/// - 返回值只含打码用户名，不含明文密码。
/// </summary>
public static class AccountBridge
{
    /// <summary>
    /// 执行器（测试可替换以验证失败形态不抛异常）。
    /// 生产默认 <see cref="Task.Run(Func{Task})"/>，保证同步网络调用不阻塞 UI 线程。
    /// </summary>
    internal static Func<Func<AccountStatusDto>, Task<AccountStatusDto>> Executor { get; set; } =
        work => Task.Run(work);

    /// <summary>把账号密码写入 Python 进程内存（不联网、不落盘）。</summary>
    public static Task<AccountStatusDto> SetCredentials(string username, string password)
    {
        string user = username ?? string.Empty;
        string pwd = password ?? string.Empty;
        return InvokeAsync(
            "写入账号凭据",
            () => PythonGateway.CallJson<AccountStatusDto>(
                backend => (string)backend.account_set_credentials(user, pwd)));
    }

    /// <summary>用 Python 内存里的凭据登录（同步网络，已异步包装）。</summary>
    public static Task<AccountStatusDto> Login()
    {
        return InvokeAsync(
            "账号登录",
            () => PythonGateway.CallJson<AccountStatusDto>(
                backend => (string)backend.account_login()));
    }

    /// <summary>一次性登录：凭据不留在 Python 长期内存（同步网络，已异步包装）。</summary>
    public static Task<AccountStatusDto> LoginWith(string username, string password)
    {
        string user = username ?? string.Empty;
        string pwd = password ?? string.Empty;
        return InvokeAsync(
            "账号一次性登录",
            () => PythonGateway.CallJson<AccountStatusDto>(
                backend => (string)backend.account_login_with(user, pwd)));
    }

    /// <summary>退出登录；默认保留 Python 内存凭据（由返回的 credentials_kept 告知）。</summary>
    public static Task<AccountStatusDto> Logout()
    {
        return InvokeAsync(
            "退出账号登录",
            () => PythonGateway.CallJson<AccountStatusDto>(
                backend => (string)backend.account_logout()));
    }

    /// <summary>查询登录态（纯查询、不联网；仍按统一异步入口包装）。</summary>
    public static Task<AccountStatusDto> Status()
    {
        return InvokeAsync(
            "查询账号状态",
            () => PythonGateway.CallJson<AccountStatusDto>(
                backend => (string)backend.account_status()));
    }

    /// <summary>清空 Python 内存凭据与登录态（修改账号前调用）。</summary>
    public static Task<AccountStatusDto> Clear()
    {
        return InvokeAsync(
            "清除账号凭据",
            () => PythonGateway.CallJson<AccountStatusDto>(
                backend => (string)backend.account_clear()));
    }

    /// <summary>
    /// 与 Python <c>jml.core.account.mask_username</c> 一致的展示打码：
    /// 1~2 字符 → <c>***</c>；3~5 字符 → 首尾各 1；6 字符及以上 → 首尾各 2。
    ///
    /// 仅用于“还没调用 Python 状态接口”时展示已保存账号；登录后以
    /// <see cref="AccountStatusDto.UsernameMasked"/> 为准，绝不把明文回写配置。
    /// </summary>
    public static string MaskUsername(string? username)
    {
        string text = username ?? string.Empty;
        if (text.Length == 0)
        {
            return "";
        }

        if (text.Length <= 2)
        {
            return "***";
        }

        if (text.Length <= 5)
        {
            return $"{text[0]}***{text[^1]}";
        }

        return $"{text[..2]}***{text[^2..]}";
    }

    private static async Task<AccountStatusDto> InvokeAsync(
        string operation,
        Func<AccountStatusDto> call)
    {
        try
        {
            return await Executor(call).ConfigureAwait(false);
        }
        catch (Exception ex)
        {
            // 只记录脱敏后的类型与文案；桥接层兜底错误码为 UNKNOWN_ERROR。
            BridgeFailures.Log(operation, ex);
            return BridgeFailures.Create<AccountStatusDto>(ex);
        }
    }
}

/// <summary>
/// 账号操作抽象：生产实现 <see cref="AccountGateway"/> 只转发到 <see cref="AccountBridge"/>，
/// 测试可注入假实现，离线断言「使用已保存账号登录」的调用顺序与失败形态。
///
/// 纪律：接口只暴露既有桥接入口，不新增 Python 契约；密码只作为参数传递，不进入任何结果。
/// </summary>
internal interface IAccountGateway
{
    Task<AccountStatusDto> SetCredentials(string username, string password);

    Task<AccountStatusDto> Login();

    Task<AccountStatusDto> Logout();

    Task<AccountStatusDto> Clear();

    Task<AccountStatusDto> Status();
}

/// <summary>生产用账号网关：逐项转发到静态 <see cref="AccountBridge"/>，不复制任何业务判断。</summary>
internal sealed class AccountGateway : IAccountGateway
{
    /// <summary>无状态单例，供窗口/页面默认构造使用。</summary>
    public static AccountGateway Instance { get; } = new();

    private AccountGateway()
    {
    }

    public Task<AccountStatusDto> SetCredentials(string username, string password) =>
        AccountBridge.SetCredentials(username, password);

    public Task<AccountStatusDto> Login() => AccountBridge.Login();

    public Task<AccountStatusDto> Logout() => AccountBridge.Logout();

    public Task<AccountStatusDto> Clear() => AccountBridge.Clear();

    public Task<AccountStatusDto> Status() => AccountBridge.Status();
}
