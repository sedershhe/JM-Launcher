using System.IO;
using System.Text;
using System.Text.RegularExpressions;

namespace JML.Infrastructure;

/// <summary>
/// 日志与错误信息的凭据脱敏。
///
/// 安全红线：网络日志和报错信息里的账号、Cookie、Token 去掉之后，才能贴进对话、日志或提交。
/// 覆盖范围沿用旧项目 sanitize_text 的关键词，只多不少。
/// </summary>
public static class LogSanitizer
{
    /// <summary>需要脱敏的敏感字段名</summary>
    private static readonly string[] SensitiveKeys =
    {
        "password", "passwd", "token", "cookie", "authorization", "username"
    };

    /// <summary>
    /// 对文本做脱敏处理。
    /// 处理两类形态：
    /// 1) key=value / "key": "value" 形式的键值配对；
    /// 2) 整段文本里出现敏感关键词时，直接整段替换，避免残留。
    /// </summary>
    public static string Sanitize(string? text)
    {
        if (string.IsNullOrEmpty(text))
        {
            return string.Empty;
        }

        string result = text;

        // 1) 键值配对脱敏：password=xxx → password=***；"password": "xxx" → "password": "***"
        foreach (string key in SensitiveKeys)
        {
            // 匹配 key=值 或 key: 值（含 JSON 引号形态）
            string pattern = $@"(?i)([""]?{key}[""]?\s*[=:]\s*[""]?)([^""\s,;&}}]+)";
            result = Regex.Replace(result, pattern, "$1***");
        }

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
