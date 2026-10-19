using System.Reflection;
using System.Windows;
using System.Windows.Controls;
using JML.Bridge;
using JML.Bridge.Dto;
using JML.Pages;
using Xunit;

namespace JML.Tests;

/// <summary>
/// 「使用已保存账号登录」（重启后免重输密码）的离线测试。
///
/// 全部使用注入的假凭据存储 + 假账号网关：
/// - 不读取真实 Windows 凭据管理器 / DPAPI 文件，不联网；
/// - 只断言调用顺序、打码显示、按钮可见性与失败文案不含密码原文。
/// </summary>
public class SavedAccountLoginTests
{
    [Fact]
    public async Task SavedLogin_UsesFixedCallOrder_AndReturnsMaskedUserName()
    {
        var calls = new List<string>();
        var store = new FakeCredentialStore(calls, ("alice123", "Not-A-Real-Password"));
        var gateway = new FakeAccountGateway(calls);

        SavedLoginOutcome outcome =
            await new SavedAccountLoginService(store, gateway).LoginAsync();

        Assert.True(outcome.Ok);
        Assert.Equal("al***23", outcome.UsernameMasked);
        Assert.Equal(new[] { "Load", "SetCredentials", "Login" }, calls);
        Assert.DoesNotContain("Not-A-Real-Password", outcome.UsernameMasked);
        Assert.Equal("", outcome.ErrorMessage);
    }

    [Fact]
    public void SavedLoginButton_VisibleOnlyWhenCredentialExists()
    {
        UiSmokeTests.RunOnUiThread(() =>
        {
            var withCalls = new List<string>();
            var withSaved = new AccountDialog(
                new FakeCredentialStore(withCalls, ("alice123", "Not-A-Real-Password")),
                new FakeAccountGateway(withCalls));
            try
            {
                var button = UiSmokeTests.Find<Button>(withSaved, "BtnLoginSaved");
                Assert.Equal(Visibility.Visible, button.Visibility);
                Assert.True(button.IsEnabled);

                var username = UiSmokeTests.Find<TextBox>(withSaved, "InputUsername");
                Assert.Equal("al***23", username.Text);
                Assert.DoesNotContain("alice123", username.Text);
            }
            finally
            {
                withSaved.Close();
            }

            var withoutCalls = new List<string>();
            var withoutSaved = new AccountDialog(
                new FakeCredentialStore(withoutCalls, saved: null),
                new FakeAccountGateway(withoutCalls));
            try
            {
                var button = UiSmokeTests.Find<Button>(withoutSaved, "BtnLoginSaved");
                Assert.Equal(Visibility.Collapsed, button.Visibility);
                Assert.False(button.IsEnabled);
            }
            finally
            {
                withoutSaved.Close();
            }
        });
    }

    [Fact]
    public void SavedLoginButton_RemainsUsableAfterLogout_AndDisappearsAfterClear()
    {
        UiSmokeTests.RunOnUiThread(() =>
        {
            var calls = new List<string>();
            var store = new FakeCredentialStore(calls, ("alice123", "Not-A-Real-Password"));
            var dialog = new AccountDialog(store, new FakeAccountGateway(calls));
            try
            {
                var button = UiSmokeTests.Find<Button>(dialog, "BtnLoginSaved");

                dialog.LogoutAsync().GetAwaiter().GetResult();

                Assert.NotNull(store.Saved);
                Assert.Equal(Visibility.Visible, button.Visibility);
                Assert.True(button.IsEnabled);

                dialog.ClearSavedAsync().GetAwaiter().GetResult();

                Assert.Null(store.Saved);
                Assert.Equal(Visibility.Collapsed, button.Visibility);
                Assert.False(button.IsEnabled);
            }
            finally
            {
                dialog.Close();
            }
        });
    }

    [Fact]
    public void SavedLoginFailure_ShowsErrorWithoutPasswordText()
    {
        UiSmokeTests.RunOnUiThread(() =>
        {
            const string password = "RawSecret_987";
            var calls = new List<string>();
            var store = new FakeCredentialStore(calls, ("alice123", password));
            var gateway = new FakeAccountGateway(calls)
            {
                LoginResult = new AccountStatusDto
                {
                    Ok = false,
                    LoggedIn = false,
                    ErrorCode = "INVALID_CREDENTIALS",
                    ErrorMessage = "登录失败 password=" + password,
                },
            };

            var dialog = new AccountDialog(store, gateway);
            try
            {
                dialog.LoginWithSavedAsync().GetAwaiter().GetResult();

                var status = UiSmokeTests.Find<TextBlock>(dialog, "TextAccountStatus");
                Assert.Contains("[INVALID_CREDENTIALS]", status.Text);
                Assert.Contains("***", status.Text);
                Assert.DoesNotContain(password, status.Text);
                Assert.DoesNotContain("alice123", status.Text);
            }
            finally
            {
                dialog.Close();
            }
        });
    }

    [Fact]
    public void Settings_RefreshStatus_ShowsSavedHintWithoutLoggingIn()
    {
        UiSmokeTests.RunOnUiThread(() =>
        {
            var calls = new List<string>();
            var store = new FakeCredentialStore(calls, ("alice123", "Not-A-Real-Password"));
            var gateway = new FakeAccountGateway(calls);
            var page = new SettingsPage(store, gateway);

            page.RefreshAccountStatusAsync().GetAwaiter().GetResult();

            var status = UiSmokeTests.Find<TextBlock>(page, "TextAccountStatus");
            Assert.Equal("已保存账号：al***23（未登录）", status.Text);
            Assert.DoesNotContain("alice123", status.Text);
            Assert.DoesNotContain("Not-A-Real-Password", status.Text);

            var button = UiSmokeTests.Find<Button>(page, "BtnLoginSaved");
            Assert.Equal(Visibility.Visible, button.Visibility);
            Assert.True(button.IsEnabled);

            // 仅刷新账号状态时不得静默登录；启动自动登录由 EnsureAutoLoginAsync 负责。
            Assert.DoesNotContain("Login", calls);

            calls.Clear();
            page.LoginSavedAsync().GetAwaiter().GetResult();

            Assert.Equal(
                new[] { "Load", "SetCredentials", "Login" },
                calls.Take(3).ToArray());
            Assert.Equal("已登录：al***23", status.Text);
            Assert.Equal(Visibility.Collapsed, button.Visibility);
        });
    }

    [Fact]
    public void Settings_EnsureAutoLogin_WithSavedCredential_LogsInOnce()
    {
        UiSmokeTests.RunOnUiThread(() =>
        {
            var calls = new List<string>();
            var store = new FakeCredentialStore(calls, ("alice123", "Not-A-Real-Password"));
            var page = new SettingsPage(store, new FakeAccountGateway(calls));

            SavedLoginOutcome? outcome = page.EnsureAutoLoginAsync().GetAwaiter().GetResult();

            Assert.NotNull(outcome);
            Assert.True(outcome!.Ok);
            Assert.Equal(new[] { "Exists", "Load", "SetCredentials", "Login" }, calls);

            // 设置页晚于主窗口加载时复用同一个任务，不重复登录。
            SavedLoginOutcome? second = page.EnsureAutoLoginAsync().GetAwaiter().GetResult();
            Assert.Same(outcome, second);
            Assert.Single(calls, call => call == "Login");
        });
    }

    [Fact]
    public void Settings_EnsureAutoLogin_WithoutSavedCredential_DoesNotCallGateway()
    {
        UiSmokeTests.RunOnUiThread(() =>
        {
            var calls = new List<string>();
            var page = new SettingsPage(
                new FakeCredentialStore(calls, saved: null),
                new FakeAccountGateway(calls));

            SavedLoginOutcome? outcome = page.EnsureAutoLoginAsync().GetAwaiter().GetResult();

            Assert.Null(outcome);
            Assert.Equal(new[] { "Exists" }, calls);
        });
    }

    [Fact]
    public void Settings_WithoutSavedCredential_ShowsNotLoggedInAndHidesButton()
    {
        UiSmokeTests.RunOnUiThread(() =>
        {
            var calls = new List<string>();
            var page = new SettingsPage(
                new FakeCredentialStore(calls, saved: null),
                new FakeAccountGateway(calls));

            page.RefreshAccountStatusAsync().GetAwaiter().GetResult();

            var status = UiSmokeTests.Find<TextBlock>(page, "TextAccountStatus");
            Assert.Equal("未登录", status.Text);
            Assert.Equal(
                Visibility.Collapsed,
                UiSmokeTests.Find<Button>(page, "BtnLoginSaved").Visibility);
        });
    }

    [Fact]
    public void SettingsStatusText_SavedAccountUsesMaskedUserNameOnly()
    {
        string text = SettingsPage.BuildAccountStatusText(false, null, "alice123");

        Assert.Equal("已保存账号：al***23（未登录）", text);
        Assert.DoesNotContain("alice123", text);
        Assert.Equal("未登录", SettingsPage.BuildAccountStatusText(false, null));
        Assert.Equal("已登录：al***23", SettingsPage.BuildAccountStatusText(true, "al***23", "alice123"));
    }

    [Fact]
    public void NoAccountType_CachesPasswordInAnyField()
    {
        foreach (Type type in new[]
                 {
                     typeof(AccountDialog),
                     typeof(SavedAccountLoginService),
                     typeof(CredentialStoreAdapter),
                     typeof(SettingsPage),
                 })
        {
            // 只检查“把明文密码缓存成字段”的情况；InputPassword 是 PasswordBox 控件本身，
            // 不是字符串缓存。
            FieldInfo[] secretFields = type
                .GetFields(BindingFlags.Instance | BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic)
                .Where(field =>
                    field.FieldType == typeof(string) &&
                    field.Name.Contains("password", StringComparison.OrdinalIgnoreCase))
                .ToArray();

            Assert.True(
                secretFields.Length == 0,
                $"{type.Name} 出现了密码字段：{string.Join(", ", secretFields.Select(f => f.Name))}；" +
                "已保存密码只允许存在于局部变量，登录后立即丢弃。");
        }
    }

    [Fact]
    public void InitialState_FlagsSavedLoginVisibility()
    {
        AccountDialog.InitialState withSaved = AccountDialog.BuildInitialState(
            ("alice123", "Not-A-Real-Password"),
            lastError: null);
        AccountDialog.InitialState withoutSaved = AccountDialog.BuildInitialState(
            saved: null,
            lastError: null);

        Assert.True(withSaved.SavedLoginVisible);
        Assert.False(withoutSaved.SavedLoginVisible);
    }

    /// <summary>假凭据存储：记录调用顺序，不读写磁盘/凭据管理器。</summary>
    private sealed class FakeCredentialStore : ISavedCredentialStore
    {
        private readonly List<string> _calls;

        public FakeCredentialStore(
            List<string> calls,
            (string Username, string Password)? saved)
        {
            _calls = calls;
            Saved = saved;
        }

        public (string Username, string Password)? Saved { get; set; }

        public string LastError { get; private set; } = "";

        public bool Exists()
        {
            _calls.Add("Exists");
            return Saved is not null;
        }

        public (string Username, string Password)? Load()
        {
            _calls.Add("Load");
            return Saved;
        }

        public bool Save(string username, string password)
        {
            _calls.Add("Save");
            Saved = (username, password);
            return true;
        }

        public bool Delete()
        {
            _calls.Add("Delete");
            Saved = null;
            return true;
        }
    }

    /// <summary>假账号网关：返回预置 DTO，记录调用顺序，不触发 Python/网络。</summary>
    private sealed class FakeAccountGateway : IAccountGateway
    {
        private readonly List<string> _calls;

        public FakeAccountGateway(List<string> calls)
        {
            _calls = calls;
        }

        public AccountStatusDto SetResult { get; set; } = new() { Ok = true };

        public AccountStatusDto LoginResult { get; set; } = new()
        {
            Ok = true,
            LoggedIn = true,
            UsernameMasked = "al***23",
        };

        public AccountStatusDto LogoutResult { get; set; } = new() { Ok = true };

        public AccountStatusDto ClearResult { get; set; } = new() { Ok = true };

        public AccountStatusDto StatusResult { get; set; } = new() { Ok = true, LoggedIn = false };

        public Task<AccountStatusDto> SetCredentials(string username, string password)
        {
            _calls.Add("SetCredentials");
            return Task.FromResult(SetResult);
        }

        public Task<AccountStatusDto> Login()
        {
            _calls.Add("Login");
            return Task.FromResult(LoginResult);
        }

        public Task<AccountStatusDto> Logout()
        {
            _calls.Add("Logout");
            return Task.FromResult(LogoutResult);
        }

        public Task<AccountStatusDto> Clear()
        {
            _calls.Add("Clear");
            return Task.FromResult(ClearResult);
        }

        public Task<AccountStatusDto> Status()
        {
            _calls.Add("Status");
            return Task.FromResult(StatusResult);
        }
    }
}
