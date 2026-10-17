using System.IO;
using System.Text;
using System.Text.RegularExpressions;

namespace JML.Infrastructure;

/// <summary>
/// 日志与错误信息的凭据脱敏。
///
/// 安全红线：网络日志和报错信息里的账号、Cookie、Token 去掉之后，才能贴进对话、日志或提交。
/// 覆盖范围与 Python 侧 <c>jml/core/errors.py</c> 的 <c>sanitize_text</c> 对齐（只多不少）：
///   1) <c>Bearer xxx</c> / <c>Basic xxx</c> 的令牌（对应 Python 的 _AUTH_SCHEME_PATTERN）；
///   2) Cookie / Set-Cookie / Authorization / Proxy-Authorization 一类请求头整行取值
///      （对应 Python 的 _HEADER_VALUE_PATTERN）；
///   3) 其它敏感键的 key=value / "key": "value"（对应 Python 的 _KV_PATTERN）。
///
/// SEC-1 回归：旧实现只有第 3 条，`"authorization": "Bearer abc.def"` 会被打成
/// `"authorization": "*** abc.def"`，"Bearer" 被吃掉而令牌 abc.def 残留。
/// 因此第 1 步（认证方案 + 令牌）必须先执行，避免令牌在后续步骤被拆碎后失去识别特征。
/// </summary>
public static class LogSanitizer
{
    /// <summary>打码占位符（与 Python 侧一致）。</summary>
    private const string Mask = "***";

    /// <summary>
    /// 需要打码的敏感键名（大小写不敏感）。
    /// 与 Python <c>errors.py</c> 的 _SENSITIVE_KEY_PATTERN 同一份清单、同一条正则形态。
    /// </summary>
    private const string SensitiveKeyPattern =
        "password|passwd|pwd|token|access[_-]?token|refresh[_-]?token|" +
        "username|user[_-]?name|account|secret|api[_-]?key";

    /// <summary>
    /// HTTP 认证方案 + 令牌：Bearer / Basic 后面的令牌整体打码。
    /// 令牌字符集与 Python 的 _AUTH_SCHEME_PATTERN 一致（base64url / base64 / 点号分隔的 JWT）。
    /// </summary>
    private static readonly Regex AuthSchemePattern = new(
        @"(?i)\b(bearer|basic)(\s+)([A-Za-z0-9._~+/=\-]{4,})",
        RegexOptions.Compiled | RegexOptions.CultureInvariant);

    /// <summary>
    /// Cookie / Authorization 一类请求头：值本身可能含空格、分号、多段，整行取值打码。
    /// 同时兼容 JSON 形态（键值两侧带引号，值也可能是带引号串）。
    /// </summary>
    private static readonly Regex HeaderValuePattern = new(
        @"(?i)\b(cookie|set-cookie|authorization|proxy-authorization)\b([""']?\s*[:=]\s*)(""[^""\r\n]*""|'[^'\r\n]*'|[^\r\n]+)",
        RegexOptions.Compiled | RegexOptions.CultureInvariant);

    /// <summary>
    /// 其它敏感键的键值对：只打码取值，保留键名便于排错。
    /// 支持 key=value / key: value / "key": "value" 三种形态。
    /// </summary>
    private static readonly Regex KeyValuePattern = new(
        $@"(?i)([""']?\b(?:{SensitiveKeyPattern})\b[""']?\s*[:=]\s*[""']?)([^""'\s,;&}}]+)",
        RegexOptions.Compiled | RegexOptions.CultureInvariant);

    /// <summary>
    /// 对文本做脱敏处理（纯字符串处理，不读写磁盘、不访问网络）。
    /// </summary>
    public static string Sanitize(string? text)
    {
        if (string.IsNullOrEmpty(text))
        {
            return string.Empty;
        }

        string result = text;

        // 1) 认证方案 + 令牌：必须最先做。否则 "authorization: Bearer abc.def" 会被键值规则
        //    先把 "Bearer" 打成 ***，只剩 " abc.def"，后续再没有可识别的令牌形态（SEC-1）。
        result = AuthSchemePattern.Replace(
            result, m => m.Groups[1].Value + m.Groups[2].Value + Mask);

        // 2) Cookie / Authorization 一类请求头的整行取值
        result = HeaderValuePattern.Replace(
            result, m => m.Groups[1].Value + m.Groups[2].Value + Mask);

        // 3) 其它敏感键的键值对
        result = KeyValuePattern.Replace(result, m => m.Groups[1].Value + Mask);

        return result;
    }

    /// <summary>
    /// 脱敏并记录日志（写入 %LOCALAPPDATA%\JML\logs）。
    /// 日志内容一律先脱敏再落盘。
    /// </summary>
    public static void Log(string? message)
    {
        string safe = Sanitize(message);
        string line = $"[{DateTime.Now:yyyy-MM-dd HH:mm:ss}] {safe}";

        try
        {
            AppPaths.EnsureCreated();
            string file = Path.Combine(AppPaths.LogDirectory, $"jml-{DateTime.Now:yyyyMMdd}.log");
            File.AppendAllText(file, line + Environment.NewLine, Encoding.UTF8);
        }
        catch
        {
            // 日志写入失败不得影响主流程
        }
    }
}
