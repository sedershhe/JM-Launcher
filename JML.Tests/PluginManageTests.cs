using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using JML.Bridge;
using JML.Bridge.Dto;
using JML.Pages;
using Xunit;

namespace JML.Tests;

/// <summary>
/// 插件启用/停用（阶段 4-5）离线测试：
/// DTO 反序列化、状态列渲染（已加载/已停用/加载失败原因）、
/// 停用前依赖确认框（可注入确认器）、SetPluginEnabled 失败展示。
///
/// 全程用假网关，不启动 Python、不联网、不读写真实 %APPDATA%。
/// </summary>
public class PluginManageTests
{
    private static readonly JsonSerializerOptions Options = new()
    {
        PropertyNameCaseInsensitive = true,
    };

    private static T Parse<T>(string json)
    {
        T? value = JsonSerializer.Deserialize<T>(json, Options);
        Assert.NotNull(value);
        return value!;
    }

    // ------------------------------------------------------------------
    // DTO 契约（与 Python 返回值逐字对齐）
    // ------------------------------------------------------------------

    [Fact]
    public void BootstrapDto_ReadsEnabledAndBlockedReason()
    {
        const string json = """
        {
          "ok": true,
          "loaded": [
            {"id":"a","name":"插件A","version":"1.0","description":"正常","pages":[],
             "development_sample":false,"enabled":true,"blocked_reason":""}
          ],
          "failed": [
            {"id":"b","reason":"插件已停用","enabled":false,"blocked_reason":"插件已停用"},
            {"id":"c","reason":"缺少依赖插件 a（该插件已停用）","enabled":true,
             "blocked_reason":"缺少依赖插件 a（该插件已停用）"}
          ],
          "services": {"jm.search": 1}
        }
        """;

        BootstrapResultDto dto = Parse<BootstrapResultDto>(json);

        Assert.True(dto.Ok);
        PluginInfoDto loaded = Assert.Single(dto.Loaded);
        Assert.True(loaded.Enabled);
        Assert.Equal("", loaded.BlockedReason);

        Assert.Equal(2, dto.Failed.Count);
        Assert.False(dto.Failed[0].Enabled);
        Assert.Equal("插件已停用", dto.Failed[0].BlockedReason);
        Assert.True(dto.Failed[1].Enabled);
        Assert.Contains("缺少依赖插件 a", dto.Failed[1].Reason);
    }

    [Fact]
    public void ManagementStateDto_ReadsStatusReasonAndDependents()
    {
        const string json = """
        {
          "ok": true,
          "plugins": [
            {"id":"a","name":"插件A","version":"1.0","description":"被依赖",
             "enabled":true,"status":"loaded","reason":"",
             "dependents":["b","c"]},
            {"id":"b","name":"插件B","version":"1.0","description":"",
             "enabled":false,"status":"disabled","reason":"插件已停用",
             "dependents":[]},
            {"id":"c","name":"插件C","version":"1.0","description":"",
             "enabled":true,"status":"failed",
             "reason":"缺少服务 archive_reader/版本 1","dependents":[]}
          ]
        }
        """;

        PluginManagementStateDto dto = Parse<PluginManagementStateDto>(json);

        Assert.True(dto.Ok);
        Assert.Equal(3, dto.Plugins.Count);
        Assert.Equal(PluginStatuses.Loaded, dto.Plugins[0].Status);
        Assert.Equal(new[] { "b", "c" }, dto.Plugins[0].Dependents);
        Assert.Equal(PluginStatuses.Disabled, dto.Plugins[1].Status);
        Assert.False(dto.Plugins[1].Enabled);
        Assert.Equal(PluginStatuses.Failed, dto.Plugins[2].Status);
        Assert.Contains("缺少服务", dto.Plugins[2].Reason);
    }

    [Fact]
    public void SetPluginEnabledResult_ReadsSuccessAndNotFound()
    {
        SetPluginEnabledResultDto ok = Parse<SetPluginEnabledResultDto>(
            """{"ok": true, "plugin_id": "a", "enabled": false, "status": "disabled"}""");
        Assert.True(ok.Ok);
        Assert.Equal("a", ok.PluginId);
        Assert.False(ok.Enabled);
        Assert.Equal("disabled", ok.Status);

        SetPluginEnabledResultDto fail = Parse<SetPluginEnabledResultDto>(
            """{"ok": false, "error_code": "NOT_FOUND", "error_message": "插件不存在: x"}""");
        Assert.False(fail.Ok);
        Assert.Equal("NOT_FOUND", fail.ErrorCode);
    }

    // ------------------------------------------------------------------
    // 状态列渲染
    // ------------------------------------------------------------------

    [Fact]
    public void PluginListView_RendersLoadedDisabledAndFailedStatus()
    {
        UiSmokeTests.RunOnUiThread(() =>
        {
            var page = new PluginListView(new FakePluginManageGateway());

            page.RenderManagementState(SampleState());

            IReadOnlyList<PluginListView.PluginRow> rows = page.Rows;
            Assert.Equal(3, rows.Count);

            PluginListView.PluginRow loaded = Assert.Single(rows, row => row.Id == "a");
            Assert.Equal("已加载", loaded.Status);
            Assert.True(loaded.Enabled);

            PluginListView.PluginRow disabled = Assert.Single(rows, row => row.Id == "b");
            Assert.Equal("已停用", disabled.Status);
            Assert.False(disabled.Enabled);

            PluginListView.PluginRow failed = Assert.Single(rows, row => row.Id == "c");
            Assert.Equal("加载失败（缺少服务 archive_reader/版本 1）", failed.Status);

            var status = UiSmokeTests.Find<TextBlock>(page, "TextStatus");
            Assert.Contains("已加载 1", status.Text);
            Assert.Contains("已停用 1", status.Text);
            Assert.Contains("加载失败 1", status.Text);
        });
    }

    [Fact]
    public void PluginListView_FailedWithoutReason_ShowsPlainFailure()
    {
        UiSmokeTests.RunOnUiThread(() =>
        {
            var page = new PluginListView(new FakePluginManageGateway());
            page.RenderManagementState(
                new PluginManagementStateDto
                {
                    Ok = true,
                    Plugins =
                    [
                        new PluginManagementItemDto
                        {
                            Id = "x",
                            Name = "X",
                            Status = PluginStatuses.Failed,
                            Reason = "",
                        },
                    ],
                });

            Assert.Equal("加载失败", Assert.Single(page.Rows).Status);
        });
    }

    // ------------------------------------------------------------------
    // 停用前依赖确认
    // ------------------------------------------------------------------

    [Fact]
    public void Disable_WithDependents_ShowsConfirmationAndCancelsWhenDeclined()
    {
        UiSmokeTests.RunOnUiThread(() =>
        {
            var gateway = new FakePluginManageGateway();
            var page = new PluginListView(gateway);
            page.RenderManagementState(SampleState());

            string? dialogText = null;
            string? dialogCaption = null;
            page.ConfirmDisable = (text, caption) =>
            {
                dialogText = text;
                dialogCaption = caption;
                return false; // 用户取消
            };

            PluginListView.PluginRow row = Assert.Single(page.Rows, item => item.Id == "a");
            page.DisableRowAsync(row).GetAwaiter().GetResult();

            Assert.Equal("停用插件", dialogCaption);
            Assert.NotNull(dialogText);
            Assert.Contains("插件A", dialogText);
            Assert.Contains("插件B", dialogText); // dependents 里的插件名必须列出来
            Assert.Contains("插件C", dialogText);
            Assert.Contains("缺少依赖", dialogText);
            Assert.Empty(gateway.SetCalls); // 取消后不得调用 set_plugin_enabled
        });
    }

    [Fact]
    public void Disable_WithDependents_ProceedsWhenConfirmed()
    {
        UiSmokeTests.RunOnUiThread(() =>
        {
            var gateway = new FakePluginManageGateway();
            var page = new PluginListView(gateway);
            page.RenderManagementState(SampleState());
            page.ConfirmDisable = (_, _) => true;

            PluginListView.PluginRow row = Assert.Single(page.Rows, item => item.Id == "a");
            page.DisableRowAsync(row).GetAwaiter().GetResult();

            (string PluginId, bool Enabled) call = Assert.Single(gateway.SetCalls);
            Assert.Equal("a", call.PluginId);
            Assert.False(call.Enabled);
        });
    }

    [Fact]
    public void Disable_WithoutDependents_DoesNotShowConfirmation()
    {
        UiSmokeTests.RunOnUiThread(() =>
        {
            var gateway = new FakePluginManageGateway();
            var page = new PluginListView(gateway);
            page.RenderManagementState(
                new PluginManagementStateDto
                {
                    Ok = true,
                    Plugins =
                    [
                        new PluginManagementItemDto
                        {
                            Id = "solo",
                            Name = "独立插件",
                            Status = PluginStatuses.Loaded,
                            Enabled = true,
                        },
                    ],
                });

            bool dialogCalled = false;
            page.ConfirmDisable = (_, _) =>
            {
                dialogCalled = true;
                return false;
            };

            page.DisableRowAsync(Assert.Single(page.Rows)).GetAwaiter().GetResult();

            Assert.False(dialogCalled);
            Assert.Single(gateway.SetCalls);
        });
    }

    // ------------------------------------------------------------------
    // 失败展示
    // ------------------------------------------------------------------

    [Fact]
    public void SetPluginEnabled_Failure_ShowsErrorInStatusBar()
    {
        UiSmokeTests.RunOnUiThread(() =>
        {
            var gateway = new FakePluginManageGateway
            {
                SetResult = new SetPluginEnabledResultDto
                {
                    Ok = false,
                    ErrorCode = "IO_ERROR",
                    ErrorMessage = "插件停用状态写入失败: 磁盘只读",
                },
            };
            var page = new PluginListView(gateway);
            page.RenderManagementState(SampleState());

            bool ok = page.SetPluginEnabledAsync("a", false).GetAwaiter().GetResult();

            Assert.False(ok);
            var status = UiSmokeTests.Find<TextBlock>(page, "TextStatus");
            Assert.Contains("[IO_ERROR]", status.Text);
            Assert.Contains("插件停用失败", status.Text);
            Assert.Contains("磁盘只读", status.Text);
        });
    }

    // ------------------------------------------------------------------
    // 辅助
    // ------------------------------------------------------------------

    private static PluginManagementStateDto SampleState()
    {
        return new PluginManagementStateDto
        {
            Ok = true,
            Plugins =
            [
                new PluginManagementItemDto
                {
                    Id = "a",
                    Name = "插件A",
                    Version = "1.0",
                    Description = "被依赖的插件",
                    Enabled = true,
                    Status = PluginStatuses.Loaded,
                    Dependents = ["b", "c"],
                },
                new PluginManagementItemDto
                {
                    Id = "b",
                    Name = "插件B",
                    Version = "1.0",
                    Enabled = false,
                    Status = PluginStatuses.Disabled,
                    Reason = "插件已停用",
                },
                new PluginManagementItemDto
                {
                    Id = "c",
                    Name = "插件C",
                    Version = "1.0",
                    Enabled = true,
                    Status = PluginStatuses.Failed,
                    Reason = "缺少服务 archive_reader/版本 1",
                },
            ],
        };
    }

    /// <summary>假插件管理网关：预置 DTO、记录 set 调用，完全不碰 Python。</summary>
    private sealed class FakePluginManageGateway : IPluginManageGateway
    {
        public BootstrapResultDto BootstrapResult { get; set; } = new() { Ok = true };

        public BootstrapResultDto ListResult { get; set; } = new() { Ok = true };

        public PluginManagementStateDto StateResult { get; set; } = new() { Ok = true };

        public SetPluginEnabledResultDto SetResult { get; set; } = new()
        {
            Ok = true,
            Status = "disabled",
        };

        public List<(string PluginId, bool Enabled)> SetCalls { get; } = [];

        public Task<BootstrapResultDto> BootstrapAsync(string pluginsDir) =>
            Task.FromResult(BootstrapResult);

        public Task<BootstrapResultDto> ListAsync(string pluginsDir) =>
            Task.FromResult(ListResult);

        public Task<PluginManagementStateDto> GetManagementStateAsync(string pluginsDir) =>
            Task.FromResult(StateResult);

        public Task<SetPluginEnabledResultDto> SetEnabledAsync(string pluginId, bool enabled)
        {
            SetCalls.Add((pluginId, enabled));
            return Task.FromResult(SetResult);
        }
    }
}
