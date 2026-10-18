using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using JML.Bridge;
using JML.Bridge.Dto;
using JML.Pages;
using Xunit;

namespace JML.Tests;

/// <summary>
/// 插件设置项（任务 3-B）的离线测试：DTO 反序列化、动态表单渲染、
/// 非法值只显示字段级错误且不改界面值、password 不回填 / 不留明文、
/// <c>list_plugin_settings</c> 失败显示空态。
///
/// 全程用假插件设置网关，不启动 Python、不联网、不读写真实 %APPDATA%。
/// </summary>
public class PluginSettingsTests
{
    // ------------------------------------------------------------------
    // DTO 契约（与 Python 返回值逐字对齐）
    // ------------------------------------------------------------------

    [Fact]
    public void PluginSettingsDto_ReadsAllFieldTypes()
    {
        const string json = """
        {
          "ok": true,
          "plugins": [
            {
              "plugin_id": "settings_demo",
              "plugin_name": "设置演示插件",
              "fields": [
                {"key":"threads","label":"并发数","type":"number","default":4,"value":16,"options":[],"min":1,"max":32},
                {"key":"keyword","label":"keyword","type":"text","default":"abc","value":"abc","options":[]},
                {"key":"enabled","label":"enabled","type":"bool","default":true,"value":true,"options":[]},
                {"key":"mode","label":"mode","type":"select","default":"fast","value":"safe","options":["fast","safe","auto"]},
                {"key":"work_dir","label":"work_dir","type":"path","default":"","value":"D:/下载","options":[]},
                {"key":"token","label":"token","type":"password","default":"","value":"","options":[],"has_value":true}
              ]
            }
          ]
        }
        """;

        PluginSettingsDto dto = Parse<PluginSettingsDto>(json);

        Assert.True(dto.Ok);
        PluginSettingsGroupDto group = Assert.Single(dto.Plugins);
        Assert.Equal("settings_demo", group.PluginId);
        Assert.Equal("设置演示插件", group.PluginName);
        Assert.Equal(6, group.Fields.Count);

        PluginSettingFieldDto threads = group.Fields[0];
        Assert.Equal(PluginSettingFieldTypes.Number, threads.Type);
        Assert.Equal(1, threads.Min);
        Assert.Equal(32, threads.Max);
        Assert.Equal("16", PluginSettingValues.ToText(threads.Value));
        Assert.Equal("4", PluginSettingValues.ToText(threads.DefaultValue));

        Assert.Equal(PluginSettingFieldTypes.Select, group.Fields[3].Type);
        Assert.Equal(3, group.Fields[3].Options.Count);
        Assert.Equal("safe", PluginSettingValues.ToText(group.Fields[3].Value));
        Assert.Equal("D:/下载", PluginSettingValues.ToText(group.Fields[4].Value));

        PluginSettingFieldDto token = group.Fields[5];
        Assert.Equal(PluginSettingFieldTypes.Password, token.Type);
        Assert.Equal("", PluginSettingValues.ToText(token.Value));
        Assert.True(token.HasValue);
        Assert.DoesNotContain("secret", json, StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public void PluginSettingsDto_ReadsFailureForm()
    {
        PluginSettingsDto dto = Parse<PluginSettingsDto>(
            """{"ok": false, "error_code": "UNKNOWN_ERROR", "error_message": "枚举炸了"}""");

        Assert.False(dto.Ok);
        Assert.Equal("UNKNOWN_ERROR", dto.ErrorCode);
        Assert.Equal("枚举炸了", dto.ErrorMessage);
        Assert.Empty(dto.Plugins);
    }

    [Fact]
    public void SetPluginSettingResult_ReadsEchoAndPasswordMarker()
    {
        SetPluginSettingResultDto ok = Parse<SetPluginSettingResultDto>(
            """{"ok": true, "key": "threads", "value": 16}""");
        Assert.True(ok.Ok);
        Assert.Equal("threads", ok.Key);
        Assert.Equal("16", PluginSettingValues.ToText(ok.Value));
        Assert.False(ok.HasValue);

        SetPluginSettingResultDto password = Parse<SetPluginSettingResultDto>(
            """{"ok": true, "key": "token", "value": "", "has_value": true}""");
        Assert.True(password.Ok);
        Assert.Equal("", PluginSettingValues.ToText(password.Value));
        Assert.True(password.HasValue);

        SetPluginSettingResultDto fail = Parse<SetPluginSettingResultDto>(
            """{"ok": false, "error_code": "INVALID_ARGUMENT", "error_message": "值 999 大于最大值 32"}""");
        Assert.False(fail.Ok);
        Assert.Equal("INVALID_ARGUMENT", fail.ErrorCode);
    }

    // ------------------------------------------------------------------
    // 值 → JSON 文本（不重复 Python 的校验业务）
    // ------------------------------------------------------------------

    [Theory]
    [InlineData("16", "16")]
    [InlineData(" 007 ", "7")]
    [InlineData("1.5", "1.5")]
    [InlineData("-0.25", "-0.25")]
    [InlineData("abc", "\"abc\"")]
    public void NumberToJson_KeepsIntegerOrFallsBackToText(string input, string expected)
    {
        Assert.Equal(expected, PluginSettingValues.NumberToJson(input));
    }

    [Fact]
    public void TextAndBoolHelpers_ProduceJsonLiterals()
    {
        Assert.Equal("\"abc\"", PluginSettingValues.TextToJson("abc"));
        Assert.Equal("\"\"", PluginSettingValues.TextToJson(null));
        Assert.Equal("true", PluginSettingValues.BoolToJson(true));
        Assert.Equal("false", PluginSettingValues.BoolToJson(false));

        Assert.True(PluginSettingValues.ToBool(Json("true")));
        Assert.True(PluginSettingValues.ToBool(Json("\"true\"")));
        Assert.False(PluginSettingValues.ToBool(Json("0")));
        Assert.False(PluginSettingValues.ToBool(null));
        Assert.Equal("", PluginSettingValues.ToText(null));
    }

    // ------------------------------------------------------------------
    // 动态表单渲染
    // ------------------------------------------------------------------

    [Fact]
    public void SettingsPage_RendersDynamicFormWithTypePerField()
    {
        UiSmokeTests.RunOnUiThread(() =>
        {
            var gateway = new FakePluginSettingsGateway();
            var page = NewPage(gateway);

            page.RenderPluginSettings(SampleDto());

            Assert.Equal(6, page.PluginSettingEditors.Count);
            Assert.Equal(
                Visibility.Collapsed,
                UiSmokeTests.Find<TextBlock>(page, "TextPluginEmpty").Visibility);
            Assert.Equal(
                Visibility.Visible,
                UiSmokeTests.Find<Button>(page, "BtnSavePluginSettings").Visibility);

            PluginSettingEditor threads = Find(page, "threads");
            Assert.Equal("并发数", Assert.IsType<TextBlock>(threads.Root.Children[0]).Text);
            Assert.Equal("16", Assert.IsType<TextBox>(threads.Editor).Text);
            Assert.Null(FindBrowseButton(threads));

            Assert.Equal("abc", Assert.IsType<TextBox>(Find(page, "keyword").Editor).Text);

            var checkBox = Assert.IsType<CheckBox>(Find(page, "enabled").Editor);
            Assert.True(checkBox.IsChecked);

            var comboBox = Assert.IsType<ComboBox>(Find(page, "mode").Editor);
            var selected = Assert.IsType<ComboBoxItem>(comboBox.SelectedItem);
            Assert.Equal("safe", selected.Content);
            Assert.Equal("\"safe\"", selected.Tag);

            PluginSettingEditor workDir = Find(page, "work_dir");
            Assert.Equal("D:/下载", Assert.IsType<TextBox>(workDir.Editor).Text);
            Button browse = Assert.IsType<Button>(FindBrowseButton(workDir));
            Assert.Equal("浏览…", browse.Content);

            PluginSettingEditor token = Find(page, "token");
            var passwordBox = Assert.IsType<PasswordBox>(token.Editor);
            Assert.Equal("", passwordBox.Password); // password 从不回填
            Assert.True(passwordBox.IsEnabled);
            // 已设置提示（不含明文）
            Assert.Contains(
                token.Root.Children.OfType<TextBlock>(),
                text => text.Text.Contains("已设置", StringComparison.Ordinal));
        });
    }

    [Fact]
    public void SettingsPage_WithoutPluginSettings_ShowsEmptyState()
    {
        UiSmokeTests.RunOnUiThread(() =>
        {
            var page = NewPage(new FakePluginSettingsGateway());

            page.RenderPluginSettings(new PluginSettingsDto { Ok = true });

            var empty = UiSmokeTests.Find<TextBlock>(page, "TextPluginEmpty");
            Assert.Equal(Visibility.Visible, empty.Visibility);
            Assert.Equal("当前没有插件提供设置项", empty.Text);
            Assert.Empty(page.PluginSettingEditors);
            Assert.Equal(
                Visibility.Collapsed,
                UiSmokeTests.Find<Button>(page, "BtnSavePluginSettings").Visibility);
        });
    }

    [Fact]
    public void SettingsPage_ListFailure_ShowsEmptyStateAndFieldLevelError()
    {
        UiSmokeTests.RunOnUiThread(() =>
        {
            var gateway = new FakePluginSettingsGateway
            {
                ListResult = new PluginSettingsDto
                {
                    Ok = false,
                    ErrorCode = "UNKNOWN_ERROR",
                    ErrorMessage = "枚举炸了",
                },
            };
            var page = NewPage(gateway);

            page.RefreshPluginSettingsAsync().GetAwaiter().GetResult();

            var empty = UiSmokeTests.Find<TextBlock>(page, "TextPluginEmpty");
            Assert.Equal(Visibility.Visible, empty.Visibility);
            Assert.Equal("当前没有插件提供设置项", empty.Text);

            var error = UiSmokeTests.Find<TextBlock>(page, "TextPluginError");
            Assert.Equal(Visibility.Visible, error.Visibility);
            Assert.Contains("[UNKNOWN_ERROR]", error.Text);
            Assert.Contains("枚举炸了", error.Text);
            Assert.Empty(page.PluginSettingEditors);
        });
    }

    // ------------------------------------------------------------------
    // 提交：非法值不改变界面值；password 不回填 / 不留明文
    // ------------------------------------------------------------------

    [Fact]
    public void SettingsPage_InvalidValue_ShowsErrorAndKeepsUiValue()
    {
        UiSmokeTests.RunOnUiThread(() =>
        {
            var gateway = new FakePluginSettingsGateway
            {
                SetResult = new SetPluginSettingResultDto
                {
                    Ok = false,
                    ErrorCode = "INVALID_ARGUMENT",
                    ErrorMessage = "插件 settings_demo 的配置项 threads 的值 999 大于最大值 32",
                },
            };
            var page = NewPage(gateway);
            page.RenderPluginSettings(SampleDto());

            PluginSettingEditor threads = Find(page, "threads");
            var numberBox = Assert.IsType<TextBox>(threads.Editor);
            numberBox.Text = "999";

            page.CommitAllPluginSettingsAsync().GetAwaiter().GetResult();

            Assert.Equal(Visibility.Visible, threads.ErrorText.Visibility);
            Assert.Contains("[INVALID_ARGUMENT]", threads.ErrorText.Text);
            Assert.Contains("大于最大值", threads.ErrorText.Text);
            Assert.Equal("999", numberBox.Text); // 失败不改回界面值
            // 失败不阻止用户修正后重试
            Assert.True(numberBox.IsEnabled);

            PluginSettingCall call = Assert.Single(gateway.Calls);
            Assert.Equal("settings_demo", call.PluginId);
            Assert.Equal("threads", call.Key);
            Assert.Equal("999", call.ValueJson);
        });
    }

    [Fact]
    public void SettingsPage_UnchangedFields_AreNotWritten()
    {
        UiSmokeTests.RunOnUiThread(() =>
        {
            var gateway = new FakePluginSettingsGateway();
            var page = NewPage(gateway);
            page.RenderPluginSettings(SampleDto());

            page.CommitAllPluginSettingsAsync().GetAwaiter().GetResult();

            // 没有任何字段被改动 → 一次 set_plugin_setting 都不发（避免无谓写盘）
            Assert.Empty(gateway.Calls);
        });
    }

    [Fact]
    public void SettingsPage_Password_EmptyMeansUnchanged_AndClearedAfterSuccess()
    {
        UiSmokeTests.RunOnUiThread(() =>
        {
            var gateway = new FakePluginSettingsGateway();
            var page = NewPage(gateway);
            page.RenderPluginSettings(SampleDto());

            PluginSettingEditor token = Find(page, "token");
            var passwordBox = Assert.IsType<PasswordBox>(token.Editor);

            // 留空：不提交、不报错
            page.CommitAllPluginSettingsAsync().GetAwaiter().GetResult();
            Assert.Empty(gateway.Calls);

            const string secret = "PLUGIN-SECRET-7";
            passwordBox.Password = secret;
            page.CommitAllPluginSettingsAsync().GetAwaiter().GetResult();

            PluginSettingCall call = Assert.Single(gateway.Calls);
            Assert.Equal("token", call.Key);
            Assert.Equal($"\"{secret}\"", call.ValueJson);

            // 成功后立刻清空密码框：C# 不保留明文，之后留空 = 不修改
            Assert.Equal("", passwordBox.Password);
            Assert.Equal(Visibility.Collapsed, token.ErrorText.Visibility);

            page.CommitAllPluginSettingsAsync().GetAwaiter().GetResult();
            Assert.Single(gateway.Calls);
        });
    }

    [Fact]
    public void SettingsPage_BoolAndSelect_ChangeTriggersWrite()
    {
        UiSmokeTests.RunOnUiThread(() =>
        {
            var gateway = new FakePluginSettingsGateway();
            var page = NewPage(gateway);
            page.RenderPluginSettings(SampleDto());

            var checkBox = Assert.IsType<CheckBox>(Find(page, "enabled").Editor);
            checkBox.IsChecked = false;
            page.CommitAllPluginSettingsAsync().GetAwaiter().GetResult();

            PluginSettingCall boolCall = Assert.Single(gateway.Calls);
            Assert.Equal("enabled", boolCall.Key);
            Assert.Equal("false", boolCall.ValueJson);

            var comboBox = Assert.IsType<ComboBox>(Find(page, "mode").Editor);
            comboBox.SelectedIndex = 0; // fast
            page.CommitAllPluginSettingsAsync().GetAwaiter().GetResult();

            Assert.Equal(2, gateway.Calls.Count);
            Assert.Equal("mode", gateway.Calls[1].Key);
            Assert.Equal("\"fast\"", gateway.Calls[1].ValueJson);
        });
    }

    // ------------------------------------------------------------------
    // 桥接层兜底：Python 不可用时只返回失败 DTO，不向 UI 抛异常
    // ------------------------------------------------------------------

    [Fact]
    public async Task ListAsync_BridgeFailure_ReturnsFailureDto()
    {
        Func<Func<PluginSettingsDto>, Task<PluginSettingsDto>> original =
            PluginSettingsBridge.ListExecutor;
        try
        {
            PluginSettingsBridge.ListExecutor = _ =>
                throw new InvalidOperationException("Python 未就绪");

            PluginSettingsDto dto = await PluginSettingsBridge.ListAsync();

            Assert.False(dto.Ok);
            Assert.Equal("UNKNOWN_ERROR", dto.ErrorCode);
            Assert.Empty(dto.Plugins);
        }
        finally
        {
            PluginSettingsBridge.ListExecutor = original;
        }
    }

    // ------------------------------------------------------------------
    // 辅助类型
    // ------------------------------------------------------------------

    private static T Parse<T>(string json)
    {
        T? value = JsonSerializer.Deserialize<T>(
            json,
            new JsonSerializerOptions { PropertyNameCaseInsensitive = true });
        Assert.NotNull(value);
        return value!;
    }

    private static JsonElement Json(string raw)
    {
        using var document = JsonDocument.Parse(raw);
        return document.RootElement.Clone();
    }

    private static PluginSettingsDto SampleDto()
    {
        return new PluginSettingsDto
        {
            Ok = true,
            Plugins =
            [
                new PluginSettingsGroupDto
                {
                    PluginId = "settings_demo",
                    PluginName = "设置演示插件",
                    Fields =
                    [
                        new PluginSettingFieldDto
                        {
                            Key = "threads",
                            Label = "并发数",
                            Type = PluginSettingFieldTypes.Number,
                            DefaultValue = Json("4"),
                            Value = Json("16"),
                            Min = 1,
                            Max = 32,
                        },
                        new PluginSettingFieldDto
                        {
                            Key = "keyword",
                            Label = "keyword",
                            Type = PluginSettingFieldTypes.Text,
                            DefaultValue = Json("\"abc\""),
                            Value = Json("\"abc\""),
                        },
                        new PluginSettingFieldDto
                        {
                            Key = "enabled",
                            Label = "enabled",
                            Type = PluginSettingFieldTypes.Bool,
                            DefaultValue = Json("true"),
                            Value = Json("true"),
                        },
                        new PluginSettingFieldDto
                        {
                            Key = "mode",
                            Label = "mode",
                            Type = PluginSettingFieldTypes.Select,
                            DefaultValue = Json("\"fast\""),
                            Value = Json("\"safe\""),
                            Options =
                            [
                                Json("\"fast\""),
                                Json("\"safe\""),
                                Json("\"auto\""),
                            ],
                        },
                        new PluginSettingFieldDto
                        {
                            Key = "work_dir",
                            Label = "work_dir",
                            Type = PluginSettingFieldTypes.Path,
                            DefaultValue = Json("\"\""),
                            Value = Json("\"D:/下载\""),
                        },
                        new PluginSettingFieldDto
                        {
                            Key = "token",
                            Label = "token",
                            Type = PluginSettingFieldTypes.Password,
                            DefaultValue = Json("\"\""),
                            Value = Json("\"\""),
                            HasValue = true,
                        },
                    ],
                },
            ],
        };
    }

    private static SettingsPage NewPage(FakePluginSettingsGateway gateway)
    {
        return new SettingsPage(
            new StubCredentialStore(),
            new StubAccountGateway(),
            gateway);
    }

    private static PluginSettingEditor Find(SettingsPage page, string key)
    {
        PluginSettingEditor? editor = page.FindPluginSettingEditor("settings_demo", key);
        Assert.NotNull(editor);
        return editor!;
    }

    private static Button? FindBrowseButton(PluginSettingEditor editor)
    {
        foreach (object child in editor.Root.Children)
        {
            if (child is not Grid grid)
            {
                continue;
            }

            foreach (object inner in grid.Children)
            {
                if (inner is Button button && button.Content as string == "浏览…")
                {
                    return button;
                }
            }
        }

        return null;
    }

    private sealed record PluginSettingCall(string PluginId, string Key, string ValueJson);

    /// <summary>假插件设置网关：预置 DTO、记录写调用，完全不碰 Python。</summary>
    private sealed class FakePluginSettingsGateway : IPluginSettingsGateway
    {
        public PluginSettingsDto ListResult { get; set; } = new() { Ok = true };

        public SetPluginSettingResultDto SetResult { get; set; } = new() { Ok = true };

        public List<PluginSettingCall> Calls { get; } = [];

        public Task<PluginSettingsDto> ListAsync() => Task.FromResult(ListResult);

        public Task<SetPluginSettingResultDto> SetAsync(
            string pluginId,
            string key,
            string valueJson)
        {
            Calls.Add(new PluginSettingCall(pluginId, key, valueJson));
            return Task.FromResult(SetResult);
        }
    }

    /// <summary>构造设置页只需一个不落盘的假凭据存储。</summary>
    private sealed class StubCredentialStore : ISavedCredentialStore
    {
        public string LastError => "";

        public bool Exists() => false;

        public (string Username, string Password)? Load() => null;

        public bool Save(string username, string password) => true;

        public bool Delete() => true;
    }

    /// <summary>构造设置页只需一个不联网、永不登录的假账号网关。</summary>
    private sealed class StubAccountGateway : IAccountGateway
    {
        public Task<AccountStatusDto> SetCredentials(string username, string password) =>
            Task.FromResult(new AccountStatusDto { Ok = true });

        public Task<AccountStatusDto> Login() =>
            Task.FromResult(new AccountStatusDto { Ok = true });

        public Task<AccountStatusDto> Logout() =>
            Task.FromResult(new AccountStatusDto { Ok = true });

        public Task<AccountStatusDto> Clear() =>
            Task.FromResult(new AccountStatusDto { Ok = true });

        public Task<AccountStatusDto> Status() =>
            Task.FromResult(new AccountStatusDto { Ok = true, LoggedIn = false });
    }
}
