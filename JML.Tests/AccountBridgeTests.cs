using System.Text.Json;
using JML.Bridge;
using JML.Bridge.Dto;
using JML.Pages;
using Xunit;

namespace JML.Tests;

/// <summary>
/// 账号桥接 / 界面文案离线测试。
/// 不连接真实账号：只验证 DTO 反序列化、打码、失败收口与脱敏。
/// </summary>
public class AccountBridgeTests
{
    private static readonly JsonSerializerOptions Options = new()
    {
        PropertyNameCaseInsensitive = true,
    };

    private static AccountStatusDto Parse(string json)
    {
        AccountStatusDto? value = JsonSerializer.Deserialize<AccountStatusDto>(json, Options);
        Assert.NotNull(value);
        return value!;
    }

    [Fact]
    public void AccountStatus_ReadsSuccessAndFailureJson()
    {
        AccountStatusDto ok = Parse(
            """
            {
              "ok": true,
              "logged_in": true,
              "username_masked": "ab***cd",
              "error_code": "",
              "error_message": ""
            }
            """);

        Assert.True(ok.Ok);
        Assert.True(ok.LoggedIn);
        Assert.Equal("ab***cd", ok.UsernameMasked);
        Assert.Equal("", ok.ErrorCode);
        Assert.Equal("", ok.ErrorMessage);

        AccountStatusDto fail = Parse(
            """
            {
              "ok": false,
              "logged_in": false,
              "username_masked": "ab***cd",
              "error_code": "INVALID_CREDENTIALS",
              "error_message": "账号或密码不正确"
            }
            """);

        Assert.False(fail.Ok);
        Assert.False(fail.LoggedIn);
        Assert.Equal("ab***cd", fail.UsernameMasked);
        Assert.Equal("INVALID_CREDENTIALS", fail.ErrorCode);
        Assert.Equal("账号或密码不正确", fail.ErrorMessage);
    }

    [Theory]
    [InlineData("", "")]
    [InlineData("a", "***")]
    [InlineData("ab", "***")]
    [InlineData("abc", "a***c")]
    [InlineData("abcde", "a***e")]
    [InlineData("abcdef", "ab***ef")]
    [InlineData("abcdefg", "ab***fg")]
    public void MaskUsername_MatchesPythonContract(string input, string expected)
    {
        Assert.Equal(expected, AccountBridge.MaskUsername(input));
    }

    [Fact]
    public async Task FailureShape_DoesNotThrow_AndSanitizesErrorText()
    {
        Func<Func<AccountStatusDto>, Task<AccountStatusDto>> original = AccountBridge.Executor;
        try
        {
            AccountBridge.Executor = _ => Task.FromException<AccountStatusDto>(
                new InvalidOperationException(
                    "connect failed password=TopSecret_123 token=abc.def"));

            AccountStatusDto result = await AccountBridge.Status();

            Assert.False(result.Ok);
            Assert.False(result.LoggedIn);
            Assert.Equal("UNKNOWN_ERROR", result.ErrorCode);
            Assert.DoesNotContain("TopSecret_123", result.ErrorMessage);
            Assert.DoesNotContain("abc.def", result.ErrorMessage);
        }
        finally
        {
            AccountBridge.Executor = original;
        }
    }

    [Fact]
    public void SettingsStatusText_ShowsMaskedAccountOnly()
    {
        Assert.Equal("未登录", SettingsPage.BuildAccountStatusText(false, "ab***cd"));
        Assert.Equal("已登录：ab***cd", SettingsPage.BuildAccountStatusText(true, "ab***cd"));

        // 上游误传明文也只展示打码形态。
        string text = SettingsPage.BuildAccountStatusText(true, "alice123");
        Assert.Equal("已登录：al***23", text);
        Assert.DoesNotContain("alice123", text);
    }

    [Fact]
    public void AccountDialog_FailureText_MasksPasswordValue()
    {
        var status = new AccountStatusDto
        {
            Ok = false,
            LoggedIn = false,
            ErrorCode = "INVALID_CREDENTIALS",
            ErrorMessage = "登录失败 password=RawSecret_987",
        };

        string text = AccountDialog.FormatFailure(status);

        Assert.Contains("[INVALID_CREDENTIALS]", text);
        Assert.DoesNotContain("RawSecret_987", text);
        Assert.Contains("***", text);
    }

    [Fact]
    public void AccountDialogInitialState_WithSavedCredential_ShowsMaskedUserNameOnly()
    {
        AccountDialog.InitialState state = AccountDialog.BuildInitialState(
            ("alice123", "not-a-real-password"),
            lastError: null);

        Assert.True(state.HasSaved);
        Assert.Equal("al***23", state.UsernameText);
        Assert.Contains("已保存账号（密码不再显示）", state.Hint);
        Assert.True(state.ClearEnabled);
        Assert.Null(state.Error);
        Assert.DoesNotContain("alice123", state.UsernameText);
        Assert.DoesNotContain("not-a-real-password", state.Hint);
    }

    [Fact]
    public void AccountDialogInitialState_WithoutSavedCredential_ShowsEmptyForm()
    {
        AccountDialog.InitialState state = AccountDialog.BuildInitialState(
            saved: null,
            lastError: null);

        Assert.False(state.HasSaved);
        Assert.Equal("", state.UsernameText);
        Assert.Contains("未保存账号", state.Hint);
        Assert.False(state.ClearEnabled);
        Assert.Null(state.Error);
    }
}
