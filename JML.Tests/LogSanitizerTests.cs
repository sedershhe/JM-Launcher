using JML.Infrastructure;
using Xunit;

namespace JML.Tests;

/// <summary>
/// 凭据脱敏测试。安全红线：日志与报错信息里的凭据必须被去掉。
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

    [Fact]
    public void Sanitize_HandlesNullOrEmpty()
    {
        Assert.Equal(string.Empty, LogSanitizer.Sanitize(null));
        Assert.Equal(string.Empty, LogSanitizer.Sanitize(""));
    }
}
