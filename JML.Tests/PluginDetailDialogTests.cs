using System.IO;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using JML.Bridge;
using JML.Bridge.Dto;
using JML.Pages;
using Xunit;

namespace JML.Tests;

/// <summary>
/// 插件详情窗（缺陷 2）离线 STA 测试：
/// 1) 双击插件管理列表的行 → 用假网关打开详情窗（不 ShowDialog）；
/// 2) 内容映射：名称 / id / 版本 / 状态 / 说明 / 功能页 / 依赖 / 服务 / 失败原因；
/// 3) 按钮可见性随状态，停用前的依赖确认（可注入），失败文案；
/// 4) 全程不弹 MessageBox、不调用 Python、不读写真实 %APPDATA%。
/// </summary>
public class PluginDetailDialogTests
{
    // ------------------------------------------------------------------
    // 内容映射
    // ------------------------------------------------------------------

    [Fact]
    public void DetailInfo_MapsManagementStateAndManifest()
    {
        var item = new PluginManagementItemDto
        {
            Id = "zip_web",
            Name = "ZIP 网页浏览",
            Version = "1.0.0",
            Description = "第一行说明\n第二行说明（完整多行）",
            Enabled = true,
            Status = PluginStatuses.Failed,
            Reason = "缺少服务 archive_reader/版本 1",
            Dependents = ["download_history"],
        };
        var manifest = new PluginManifestInfo
        {
            Id = "zip_web",
            Pages =
            [
                new PluginManifestPageInfo
                {
                    Id = "zip_web.main",
                    Title = "ZIP 网页浏览",
                    Template = "action",
                },
            ],
            Dependencies = ["zip_browser"],
            Provides = ["zip_web.reader/1"],
            Requires = ["archive_reader/1"],
        };

        PluginDetailInfo info = PluginDetailInfo.From(
            item,
            manifest,
            id => id == "zip_browser" ? "ZIP 浏览插件"
                : id == "download_history" ? "下载历史"
                : id);

        Assert.Equal("zip_web", info.Id);
        Assert.Equal("ZIP 网页浏览", info.Name);
        Assert.Equal("1.0.0", info.Version);
        Assert.Equal(PluginStatuses.Failed, info.Status);
        Assert.Contains("第二行说明", info.Description);
        Assert.Equal(new[] { "下载历史" }, info.Dependents);
        Assert.Equal(new[] { "ZIP 浏览插件" }, info.Dependencies);
        Assert.Equal(new[] { "zip_web.reader/1" }, info.Provides);
        Assert.Equal(new[] { "archive_reader/1" }, info.Requires);
        Assert.Contains(info.Pages, page => page.Contains("zip_web.main") && page.Contains("action"));
    }

    [Fact]
    public void Dialog_RendersAllDetailFields()
    {
        UiSmokeTests.RunOnUiThread(() =>
        {
            var dialog = new PluginDetailDialog(new FakeDetailGateway(), LoadedInfo());
            Assert.Equal("ZIP 网页浏览", Find<TextBlock>(dialog, "TextName").Text);
            Assert.Contains("id：zip_web", Find<TextBlock>(dialog, "TextIdVersion").Text);
            Assert.Contains("版本：1.0.0", Find<TextBlock>(dialog, "TextIdVersion").Text);
            Assert.Equal("已加载", Find<TextBlock>(dialog, "TextStatus").Text);
            Assert.Equal(
                "第一行说明\n第二行说明（完整多行）",
                Find<TextBlock>(dialog, "TextDescription").Text);
            Assert.Contains("zip_web.main", Find<TextBlock>(dialog, "TextPages").Text);
            Assert.Contains("ZIP 浏览插件", Find<TextBlock>(dialog, "TextDependencies").Text);
            Assert.Contains("archive_reader/1", Find<TextBlock>(dialog, "TextServices").Text);
            Assert.Contains("zip_web.reader/1", Find<TextBlock>(dialog, "TextServices").Text);

            // 说明区可滚动（完整多行）
            var descriptionScroll = Find<ScrollViewer>(dialog, "ScrollDescription");
            Assert.Equal(ScrollBarVisibility.Auto, descriptionScroll.VerticalScrollBarVisibility);

            // 已加载 → 只显示「停用」，失败原因区隐藏
            Assert.False(dialog.IsEnableVisible);
            Assert.True(dialog.IsDisableVisible);
            Assert.Equal(
                Visibility.Collapsed,
                Find<StackPanel>(dialog, "PanelFailureReason").Visibility);
        });
    }

    [Fact]
    public void Dialog_FailedPlugin_ShowsFailureReason()
    {
        UiSmokeTests.RunOnUiThread(() =>
        {
            PluginDetailInfo info = LoadedInfo() with
            {
                Status = PluginStatuses.Failed,
                Reason = "缺少服务 archive_reader/版本 1",
            };
            var dialog = new PluginDetailDialog(new FakeDetailGateway(), info);

            Assert.Equal(
                "加载失败（缺少服务 archive_reader/版本 1）",
                Find<TextBlock>(dialog, "TextStatus").Text);
            Assert.Equal(
                Visibility.Visible,
                Find<StackPanel>(dialog, "PanelFailureReason").Visibility);
            Assert.Contains(
                "archive_reader/版本 1",
                Find<TextBlock>(dialog, "TextFailureReason").Text);
        });
    }

    // ------------------------------------------------------------------
    // 按钮可见性 + 启停
    // ------------------------------------------------------------------

    [Fact]
    public void Dialog_ButtonVisibility_FollowsStatus()
    {
        UiSmokeTests.RunOnUiThread(() =>
        {
            var disabled = new PluginDetailDialog(
                new FakeDetailGateway(),
                LoadedInfo() with
                {
                    Status = PluginStatuses.Disabled,
                    Enabled = false,
                    Reason = "插件已停用",
                });
            Assert.True(disabled.IsEnableVisible);
            Assert.False(disabled.IsDisableVisible);

            var failedEnabled = new PluginDetailDialog(
                new FakeDetailGateway(),
                LoadedInfo() with { Status = PluginStatuses.Failed, Reason = "缺少服务" });
            Assert.False(failedEnabled.IsEnableVisible);
            Assert.True(failedEnabled.IsDisableVisible);
        });
    }

    [Fact]
    public void Dialog_Disable_WithDependents_Declined_DoesNotCallGateway()
    {
        UiSmokeTests.RunOnUiThread(() =>
        {
            var gateway = new FakeDetailGateway();
            var dialog = new PluginDetailDialog(gateway, LoadedInfo());

            string? confirmText = null;
            string? confirmCaption = null;
            dialog.ConfirmDisable = (text, caption) =>
            {
                confirmText = text;
                confirmCaption = caption;
                return false;
            };

            bool ok = dialog.DisableWithConfirmationAsync().GetAwaiter().GetResult();

            Assert.False(ok);
            Assert.Equal("停用插件", confirmCaption);
            Assert.NotNull(confirmText);
            Assert.Contains("ZIP 网页浏览", confirmText);
            Assert.Contains("下载历史", confirmText); // dependents 的名字必须列出来
            Assert.Contains("缺少依赖", confirmText);
            Assert.Empty(gateway.SetCalls);
        });
    }

    [Fact]
    public void Dialog_Disable_WithDependents_Confirmed_UpdatesStateAndNotifies()
    {
        UiSmokeTests.RunOnUiThread(() =>
        {
            var gateway = new FakeDetailGateway
            {
                SetResult = new SetPluginEnabledResultDto
                {
                    Ok = true,
                    PluginId = "zip_web",
                    Enabled = false,
                    Status = "disabled",
                },
            };
            var dialog = new PluginDetailDialog(gateway, LoadedInfo());
            dialog.ConfirmDisable = (_, _) => true;
            bool notified = false;
            dialog.ChangedCallback = () =>
            {
                notified = true;
                return Task.CompletedTask;
            };

            bool ok = dialog.DisableWithConfirmationAsync().GetAwaiter().GetResult();

            Assert.True(ok);
            Assert.Equal(("zip_web", false), Assert.Single(gateway.SetCalls));
            Assert.Equal(PluginStatuses.Disabled, dialog.Info.Status);
            Assert.False(dialog.Info.Enabled);
            Assert.True(dialog.IsEnableVisible);
            Assert.False(dialog.IsDisableVisible);
            Assert.True(notified);
            Assert.Equal("已停用", Find<TextBlock>(dialog, "TextStatus").Text);
        });
    }

    [Fact]
    public void Dialog_Enable_UpdatesStateAndButtons()
    {
        UiSmokeTests.RunOnUiThread(() =>
        {
            var gateway = new FakeDetailGateway
            {
                SetResult = new SetPluginEnabledResultDto
                {
                    Ok = true,
                    PluginId = "zip_web",
                    Enabled = true,
                    Status = "enabled",
                },
            };
            var dialog = new PluginDetailDialog(
                gateway,
                LoadedInfo() with
                {
                    Status = PluginStatuses.Disabled,
                    Enabled = false,
                    Reason = "插件已停用",
                });

            bool ok = dialog.SetEnabledAsync(true).GetAwaiter().GetResult();

            Assert.True(ok);
            Assert.Equal(("zip_web", true), Assert.Single(gateway.SetCalls));
            Assert.True(dialog.Info.Enabled);
            Assert.Contains("重启后加载", Find<TextBlock>(dialog, "TextStatus").Text);
            Assert.False(dialog.IsEnableVisible);
            Assert.True(dialog.IsDisableVisible);
        });
    }

    [Fact]
    public void Dialog_SetEnabledFailure_ShowsSanitizedErrorInRed()
    {
        UiSmokeTests.RunOnUiThread(() =>
        {
            var gateway = new FakeDetailGateway
            {
                SetResult = new SetPluginEnabledResultDto
                {
                    Ok = false,
                    ErrorCode = "IO_ERROR",
                    ErrorMessage = "插件停用状态写入失败: 磁盘只读",
                },
            };
            var dialog = new PluginDetailDialog(gateway, LoadedInfo());

            bool ok = dialog.SetEnabledAsync(false).GetAwaiter().GetResult();

            Assert.False(ok);
            var status = Find<TextBlock>(dialog, "TextDialogStatus");
            Assert.Contains("[IO_ERROR]", status.Text);
            Assert.Contains("磁盘只读", status.Text);
            var expected = (SolidColorBrush)dialog.FindResource("ColorBrushError");
            var actual = Assert.IsType<SolidColorBrush>(status.Foreground);
            Assert.Equal(expected.Color, actual.Color);

            // 失败不改本窗状态
            Assert.Equal(PluginStatuses.Loaded, dialog.Info.Status);
        });
    }

    // ------------------------------------------------------------------
    // manifest 只读读取器（功能页 / 依赖 / 服务来源）
    // ------------------------------------------------------------------

    [Fact]
    public void ManifestReader_ReadsRepoZipWebManifest()
    {
        string root = FindRepoRoot();
        IReadOnlyDictionary<string, PluginManifestInfo> manifests =
            PluginManifestReader.ReadAll(Path.Combine(root, "plugins"));

        Assert.True(manifests.TryGetValue("zip_web", out PluginManifestInfo? manifest));
        Assert.Contains(
            manifest!.Pages,
            page => page.Id == "zip_web.main"
                && page.Title == "ZIP 网页浏览"
                && page.Template == "action");
        Assert.Equal(new[] { "archive_reader/1" }, manifest.Requires);
        Assert.Empty(manifest.Provides);
        Assert.Empty(manifest.Dependencies);
    }

    // ------------------------------------------------------------------
    // 列表双击 → 详情窗
    // ------------------------------------------------------------------

    [Fact]
    public void PluginListView_DoubleClick_Row_OpensDetailDialog()
    {
        UiSmokeTests.RunOnUiThread(() =>
        {
            var gateway = new FakeDetailGateway { StateResult = SampleState() };
            var page = new PluginListView(gateway);
            page.RenderManagementState(SampleState());

            PluginDetailDialog? opened = null;
            page.DetailDialogFactory = row => new PluginDetailDialog(
                gateway,
                new PluginDetailInfo
                {
                    Id = row.Id,
                    Name = row.Name,
                    Version = row.Version,
                    Status = row.StatusCode,
                    Description = row.Description,
                    Enabled = row.Enabled,
                });
            page.ShowDetailDialog = dialog => opened = dialog;

            var grid = UiSmokeTests.Find<DataGrid>(page, "GridPlugins");
            grid.SelectedItem = page.Rows[0];

            var args = new MouseButtonEventArgs(Mouse.PrimaryDevice, 0, MouseButton.Left)
            {
                RoutedEvent = Control.MouseDoubleClickEvent,
            };
            grid.RaiseEvent(args);

            Assert.NotNull(opened);
            Assert.Equal("a", opened!.Info.Id);
            Assert.NotNull(opened.ChangedCallback); // 启停成功后通知主列表刷新
        });
    }

    // ------------------------------------------------------------------
    // 辅助
    // ------------------------------------------------------------------

    private static PluginDetailInfo LoadedInfo() => new()
    {
        Id = "zip_web",
        Name = "ZIP 网页浏览",
        Version = "1.0.0",
        Status = PluginStatuses.Loaded,
        Description = "第一行说明\n第二行说明（完整多行）",
        Enabled = true,
        Dependents = ["下载历史"],
        Pages = ["ZIP 网页浏览（zip_web.main / action）"],
        Dependencies = ["ZIP 浏览插件"],
        Provides = ["zip_web.reader/1"],
        Requires = ["archive_reader/1"],
    };

    private static PluginManagementStateDto SampleState() => new()
    {
        Ok = true,
        Plugins =
        [
            new PluginManagementItemDto
            {
                Id = "a",
                Name = "插件A",
                Version = "1.0",
                Description = "说明A",
                Enabled = true,
                Status = PluginStatuses.Loaded,
            },
        ],
    };

    private static T Find<T>(FrameworkElement root, string name) where T : class =>
        UiSmokeTests.Find<T>(root, name);

    /// <summary>从测试输出目录向上找 JML.csproj（用于只读真实 plugins/ 的 manifest）。</summary>
    private static string FindRepoRoot()
    {
        DirectoryInfo? directory = new(AppContext.BaseDirectory);
        while (directory is not null)
        {
            if (File.Exists(Path.Combine(directory.FullName, "JML.csproj")))
            {
                return directory.FullName;
            }

            directory = directory.Parent;
        }

        throw new InvalidOperationException("找不到 JML.csproj 所在仓库根目录");
    }

    /// <summary>假插件管理网关：记录 set 调用、返回预置 DTO，完全不碰 Python。</summary>
    private sealed class FakeDetailGateway : IPluginManageGateway
    {
        public BootstrapResultDto BootstrapResult { get; set; } = new() { Ok = true };

        public BootstrapResultDto ListResult { get; set; } = new() { Ok = true };

        public PluginManagementStateDto StateResult { get; set; } = new() { Ok = true };

        public SetPluginEnabledResultDto SetResult { get; set; } = new() { Ok = true };

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
