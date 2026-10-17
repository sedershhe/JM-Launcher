using JML.Infrastructure;
using Xunit;

namespace JML.Tests;

/// <summary>
/// 凭据脱敏测试。安全红线：日志与报错信息里的凭据必须被去掉。
///
/// SEC-1 回归：旧实现只按 "key=value" 打码，`Bearer abc.def` 这种"方案 + 令牌"形态会漏成 "*** abc.def"，
/// 令牌残留在日志里。下面逐条断言两侧（本类与 SanitizerCrossLanguageTests）都不残留原始令牌。
/// </summary>
public class LogSanitizerTests
{
    [Theory]
    [InlineData("password=abc123", "***")]
    [InlineData("token: xyz789", "***")]
    [InlineData("\"cookie\": \"session=1\"", "***")]
    public void Sanitize_MasksSensitiveValue(string input, string expectedMask)
    {
        string result = LogSanitizer.Sanitize(input);

        Assert.Contains(expectedMask, result);
    }

    [Fact]
    public void Sanitize_DoesNotLeakOriginalValue()
    {
        string result = LogSanitizer.Sanitize("username=tom password=secret123");

        Assert.DoesNotContain("secret123", result);
        Assert.DoesNotContain("tom", result);
    }

    /// <summary>
    /// SEC-1：认证方案（Bearer / Basic）后的令牌必须打码，且不留原始令牌碎片。
    /// 修复前 `"authorization": "Bearer abc.def"` → `"*** abc.def"`（abc.def 残留）。
    /// </summary>
    [Theory]
    [InlineData("Bearer abc.def", "abc.def")]
    [InlineData("Basic dXNlcjpwYXNz", "dXNlcjpwYXNz")]
    [InlineData("authorization: Bearer abc.def", "abc.def")]
    [InlineData("Authorization: Bearer AbC.dEf", "AbC.dEf")]
    [InlineData("AUTHORIZATION: bEaReR AbC.dEf", "AbC.dEf")]
    [InlineData("X-Api-Token: Basic ZTp0", "ZTp0")]
    [InlineData("\"authorization\": \"Bearer abc.def\"", "abc.def")]
    public void Sanitize_MasksAuthSchemeToken(string input, string rawToken)
    {
        string result = LogSanitizer.Sanitize(input);

        Assert.DoesNotContain(rawToken, result);
        Assert.Contains("***", result);
    }

    /// <summary>
    /// Cookie / Authorization 请求头整行取值打码（值里可能含空格、分号、多段，不能只截第一个词）。
    /// </summary>
    [Theory]
    [InlineData("cookie: a=b; c=d", new[] { "a=b", "c=d" })]
    [InlineData("Cookie: SESSION=abc; UID=42", new[] { "SESSION=abc", "UID=42" })]
    [InlineData("set-cookie: a=b; Path=/", new[] { "a=b", "Path=/" })]
    [InlineData("proxy-authorization: Basic Zm9vOmJhcg==", new[] { "Zm9vOmJhcg==" })]
    public void Sanitize_MasksWholeHeaderLine(string input, string[] secrets)
    {
        string result = LogSanitizer.Sanitize(input);

        foreach (string secret in secrets)
        {
            Assert.DoesNotContain(secret, result);
        }

        Assert.Contains("***", result);
    }

    /// <summary>
    /// 大小写混写、JSON 引号形态都不能绕过；脱敏不能吞掉无关正文。
    /// </summary>
    [Fact]
    public void Sanitize_KeepsNonSensitiveTextAndMasksMixedCase()
    {
        string result = LogSanitizer.Sanitize("请求失败 401: {\"AuthoriZation\": \"bearer a.b.c\", \"reason\": \"expired\"}");

        Assert.DoesNotContain("a.b.c", result);
        Assert.Contains("***", result);

        string plain = LogSanitizer.Sanitize("下载完成：共 3 张，路径 D:\\out\\123");
        Assert.Equal("下载完成：共 3 张，路径 D:\\out\\123", plain);
    }

    [Fact]
    public void Sanitize_HandlesNullOrEmpty()
    {
        Assert.Equal(string.Empty, LogSanitizer.Sanitize(null));
        Assert.Equal(string.Empty, LogSanitizer.Sanitize(""));
    }
}
