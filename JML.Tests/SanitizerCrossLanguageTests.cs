using System.IO;
using JML.Infrastructure;
using Python.Runtime;
using Xunit;

namespace JML.Tests;

/// <summary>
/// SEC-1 跨语言一致性：同一段文本，C# <see cref="LogSanitizer"/> 与 Python
/// <c>jml.core.errors.sanitize_text</c> 都不得残留认证令牌。
///
/// 与 PythonBackend 集合串行，避免和别的 Python 用例抢全局状态。
/// </summary>
[Collection(PythonBackendCollection.Name)]
public class SanitizerCrossLanguageTests
{
    [Theory]
    [InlineData("Bearer abc.def", "abc.def")]
    [InlineData("Basic dXNlcjpwYXNz", "dXNlcjpwYXNz")]
    [InlineData("authorization: Bearer abc.def", "abc.def")]
    [InlineData("Authorization: bEaReR AbC.dEf", "AbC.dEf")]
    [InlineData("AUTHORIZATION: BEARER AbC.dEf", "AbC.dEf")]
    [InlineData("cookie: a=b; c=d", "a=b")]
    [InlineData("set-cookie: a=b; c=d", "c=d")]
    [InlineData("proxy-authorization: Basic Zm9vOmJhcg==", "Zm9vOmJhcg==")]
    [InlineData("\"authorization\": \"Bearer abc.def\"", "abc.def")]
    public void BothSides_DoNotLeakToken(string input, string rawToken)
    {
        string csharp = LogSanitizer.Sanitize(input);
        string python = PythonSanitize(input);

        Assert.DoesNotContain(rawToken, csharp);
        Assert.DoesNotContain(rawToken, python);
        Assert.Contains("***", csharp);
        Assert.Contains("***", python);
    }

    /// <summary>调用 Python 侧 sanitize_text（与 C# 侧对照的唯一入口）。</summary>
    private static string PythonSanitize(string text)
    {
        return PythonRuntime.Invoke(() =>
        {
            dynamic sys = Py.Import("sys");
            dynamic path = sys.path;
            path.append(Path.Combine(AppContext.BaseDirectory, "Python"));

            dynamic errors = Py.Import("jml.core.errors");
            return (string)errors.sanitize_text(text);
        });
    }
}
