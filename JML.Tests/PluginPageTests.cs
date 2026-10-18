using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using JML.Bridge;
using JML.Bridge.Dto;
using JML.Pages;
using Xunit;

namespace JML.Tests;

/// <summary>
/// 插件结构化页面协议（技术方案第三节方案 A，2026-10-18）离线测试：
/// 1) 三套模板 DTO 用真实 JSON 字符串反序列化（snake_case、行内任意字段保留）；
/// 2) <see cref="PluginPageHost"/> 在 STA 线程 + 假网关下按 list / browser / action
///    三种 template 构造与渲染；
/// 3) 空态文案、分页按钮状态、动作失败 <c>[error_code]</c> 展示；
/// 4) 全程不启动 Python、不联网、不读写真实 %APPDATA%。
/// </summary>
public class PluginPageTests
{
    private static readonly JsonSerializerOptions Options = new()
    {
        PropertyNameCaseInsensitive = true,
    };

    /// <summary>1×1 透明 PNG（最小可解码图片，用来验证 UI 线程建 BitmapImage）。</summary>
    private const string OnePixelPngBase64 =
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==";

    private static T Parse<T>(string json)
    {
        T? value = JsonSerializer.Deserialize<T>(json, Options);
        Assert.NotNull(value);
        return value!;
    }

    // ------------------------------------------------------------------
    // DTO 契约
    // ------------------------------------------------------------------

    [Fact]
    public void LoadResult_ReadsListTemplateWithArbitraryRowFields()
    {
        const string json = """
        {
          "ok": true,
          "template": "list",
          "title": "下载历史",
          "data": {
            "columns": [
              {"key": "time", "title": "时间"},
              {"key": "book_id", "title": "编号", "width": 80}
            ],
            "rows": [
              {"time": "2026-10-18", "book_id": "123", "extra": {"nested": 1}},
              {"time": "2026-10-17", "book_id": "456"}
            ],
            "page": 2,
            "page_size": 50,
            "total": 120,
            "total_pages": 3,
            "row_actions": [{"id": "open", "title": "打开", "primary": true}],
            "empty_text": "没有记录"
          }
        }
        """;

        PluginPageLoadResultDto dto = Parse<PluginPageLoadResultDto>(json);

        Assert.True(dto.Ok);
        Assert.Equal("list", dto.Template);
        Assert.Equal("下载历史", dto.Title);
        Assert.True(PluginPageData.IsObject(dto.Data));

        PluginListPageDto page = Assert.IsType<PluginListPageDto>(PluginPageData.ToList(dto.Data));
        Assert.Equal(2, page.Columns.Count);
        Assert.Equal("time", page.Columns[0].Key);
        Assert.Equal("时间", page.Columns[0].Title);
        Assert.Null(page.Columns[0].Width);
        Assert.Equal(80, page.Columns[1].Width);
        Assert.Equal(2, page.Page);
        Assert.Equal(50, page.PageSize);
        Assert.Equal(120, page.Total);
        Assert.Equal(3, page.TotalPages);
        Assert.Equal(2, page.Rows.Count);
        Assert.Equal("123", page.Rows[0]["book_id"].GetString());
        Assert.Equal(1, page.Rows[0]["extra"].GetProperty("nested").GetInt32());
        PluginPageRowActionDto action = Assert.Single(page.RowActions);
        Assert.Equal("open", action.Id);
        Assert.True(action.Primary);
        Assert.Equal("没有记录", page.EmptyText);
    }

    [Fact]
    public void LoadResult_ReadsBrowserTemplate()
    {
        const string json = """
        {
          "ok": true,
          "template": "browser",
          "title": "ZIP 浏览",
          "data": {
            "items": [
              {"name": "001.zip", "path": "C:/z/001.zip"},
              {"name": "002.zip", "path": "C:/z/002.zip"}
            ],
            "page": 1,
            "page_size": 20,
            "total": 2,
            "total_pages": 1,
            "item_index": 1,
            "image_index": 0,
            "image_count": 7,
            "empty_text": "目录为空"
          }
        }
        """;

        PluginPageLoadResultDto dto = Parse<PluginPageLoadResultDto>(json);
        PluginBrowserPageDto page =
            Assert.IsType<PluginBrowserPageDto>(PluginPageData.ToBrowser(dto.Data));

        Assert.Equal(2, page.Items.Count);
        Assert.Equal("001.zip", page.Items[0].Name);
        Assert.Equal("C:/z/001.zip", page.Items[0].Path);
        Assert.Equal(1, page.ItemIndex);
        Assert.Equal(0, page.ImageIndex);
        Assert.Equal(7, page.ImageCount);
        Assert.Equal("目录为空", page.EmptyText);
    }

    [Fact]
    public void LoadResult_ReadsActionTemplate()
    {
        const string json = """
        {
          "ok": true,
          "template": "action",
          "title": "ZIP 网页浏览",
          "data": {
            "items": [{"name": "001.zip", "path": "C:/z/001.zip"}],
            "state": "running",
            "status_text": "服务运行中：http://127.0.0.1:8080",
            "actions": [
              {"id": "start", "title": "开始浏览"},
              {"id": "stop", "title": "停止浏览"}
            ],
            "port": 8080
          }
        }
        """;

        PluginPageLoadResultDto dto = Parse<PluginPageLoadResultDto>(json);
        PluginActionPageDto page =
            Assert.IsType<PluginActionPageDto>(PluginPageData.ToAction(dto.Data));

        Assert.Equal("running", page.State);
        Assert.Contains("8080", page.StatusText);
        Assert.Equal(2, page.Actions.Count);
        Assert.Equal("start", page.Actions[0].Id);
        Assert.Equal(8080, page.Port);
        Assert.Single(page.Items);
    }

    [Fact]
    public void LoadResult_ReadsFailureFormAndRejectsNonObjectData()
    {
        PluginPageLoadResultDto fail = Parse<PluginPageLoadResultDto>(
            """{"ok": false, "error_code": "NOT_FOUND", "error_message": "插件未加载或不存在: demo"}""");

        Assert.False(fail.Ok);
        Assert.Equal("NOT_FOUND", fail.ErrorCode);
        Assert.Equal("插件未加载或不存在: demo", fail.ErrorMessage);
        Assert.Null(PluginPageData.ToList(fail.Data));
        Assert.Null(PluginPageData.ToBrowser(fail.Data));
        Assert.Null(PluginPageData.ToAction(fail.Data));
    }

    [Fact]
    public void ActionResult_ReadsSuccessAndFailure()
    {
        PluginPageActionResultDto ok = Parse<PluginPageActionResultDto>(
            """{"ok": true, "refresh": true, "data": {"items": []}, "message": "已启动"}""");

        Assert.True(ok.Ok);
        Assert.True(ok.Refresh);
        Assert.Equal("已启动", ok.Message);
        Assert.NotNull(ok.Data);
        Assert.True(PluginPageData.IsObject(ok.Data!.Value));

        PluginPageActionResultDto bare = Parse<PluginPageActionResultDto>(
            """{"ok": true, "refresh": false, "data": null, "message": ""}""");

        Assert.False(bare.Refresh);
        Assert.Null(bare.Data);

        PluginPageActionResultDto fail = Parse<PluginPageActionResultDto>(
            """{"ok": false, "error_code": "NOT_IMPLEMENTED", "error_message": "未实现动作钩子"}""");

        Assert.False(fail.Ok);
        Assert.Equal("NOT_IMPLEMENTED", fail.ErrorCode);
        Assert.Null(fail.Data);
    }

    [Fact]
    public void SerializeParameters_ProducesJsonObject()
    {
        Assert.Equal("{}", PluginPageBridge.SerializeParameters(null));
        Assert.Equal(
            "{}",
            PluginPageBridge.SerializeParameters(new Dictionary<string, object?>()));

        string json = PluginPageBridge.SerializeParameters(
            new Dictionary<string, object?>
            {
                ["page"] = 2,
                ["item_index"] = -1,
                ["name"] = "中文",
            });

        using JsonDocument document = JsonDocument.Parse(json);
        Assert.Equal(2, document.RootElement.GetProperty("page").GetInt32());
        Assert.Equal(-1, document.RootElement.GetProperty("item_index").GetInt32());
        Assert.Equal("中文", document.RootElement.GetProperty("name").GetString());
    }

    // ------------------------------------------------------------------
    // PluginPageHost：list
    // ------------------------------------------------------------------

    [Fact]
    public void Host_ListTemplate_BuildsColumnsRowsActionsAndPager()
    {
        UiSmokeTests.RunOnUiThread(() =>
        {
            var gateway = new FakePluginPageGateway();
            var host = NewHost(gateway);

            host.ApplyLoad(ListResult(
                page: 2,
                totalPages: 5,
                rows: 2,
                rowActions: 1,
                emptyText: "没有记录"));

            Assert.Equal(Visibility.Visible, Find<Grid>(host, "PanelList").Visibility);
            Assert.Equal(Visibility.Collapsed, Find<Grid>(host, "PanelBrowser").Visibility);
            Assert.Equal(Visibility.Collapsed, Find<Grid>(host, "PanelAction").Visibility);

            var grid = Find<DataGrid>(host, "GridRows");
            Assert.Equal(3, grid.Columns.Count); // 2 列 + 1 个行操作列
            Assert.Equal(2, grid.Items.Count);
            Assert.Equal("第 2 页 / 共 5 页", Find<TextBlock>(host, "TextListPage").Text);
            Assert.True(Find<Button>(host, "BtnListPrev").IsEnabled);
            Assert.True(Find<Button>(host, "BtnListNext").IsEnabled);
            Assert.Equal(
                Visibility.Collapsed,
                Find<TextBlock>(host, "TextListEmpty").Visibility);
            Assert.Equal(
                Visibility.Visible,
                Find<StackPanel>(host, "PanelListPager").Visibility);
            Assert.Equal(
                Visibility.Collapsed,
                Find<TextBlock>(host, "TextError").Visibility);
        });
    }

    [Fact]
    public void Host_ListTemplate_BindsCellTextFromRowJson()
    {
        UiSmokeTests.RunOnUiThread(() =>
        {
            var host = NewHost(new FakePluginPageGateway());
            host.ApplyLoad(ListResult(
                page: 1, totalPages: 1, rows: 2, rowActions: 0, emptyText: ""));

            var grid = Find<DataGrid>(host, "GridRows");
            var panel = (FrameworkElement)grid.Parent;
            panel.Measure(new Size(900, 600));
            panel.Arrange(new Rect(0, 0, 900, 600));
            panel.UpdateLayout();
            grid.UpdateLayout();

            object firstRow = grid.Items[0];
            var firstCell = grid.Columns[0].GetCellContent(firstRow) as TextBlock;
            var secondCell = grid.Columns[1].GetCellContent(firstRow) as TextBlock;
            Assert.NotNull(firstCell);
            Assert.NotNull(secondCell);
            Assert.Equal("2026-10-01", firstCell!.Text);
            Assert.Equal("100", secondCell!.Text);
        });
    }

    [Fact]
    public void Host_ListTemplate_EmptyStateAndPagerBounds()
    {
        UiSmokeTests.RunOnUiThread(() =>
        {
            var host = NewHost(new FakePluginPageGateway());

            host.ApplyLoad(ListResult(
                page: 1,
                totalPages: 1,
                rows: 0,
                rowActions: 1,
                emptyText: "还没有记录"));

            var empty = Find<TextBlock>(host, "TextListEmpty");
            Assert.Equal(Visibility.Visible, empty.Visibility);
            Assert.Equal("还没有记录", empty.Text);
            Assert.Equal(Visibility.Collapsed, Find<DataGrid>(host, "GridRows").Visibility);
            Assert.Equal(
                Visibility.Collapsed,
                Find<StackPanel>(host, "PanelListPager").Visibility);
            Assert.False(Find<Button>(host, "BtnListPrev").IsEnabled);
            Assert.False(Find<Button>(host, "BtnListNext").IsEnabled);

            // 末页：下一页禁用、上一页可用（按钮状态只跟着 Python 给的页数走）
            host.ApplyLoad(ListResult(
                page: 3,
                totalPages: 3,
                rows: 1,
                rowActions: 0,
                emptyText: ""));
            Assert.True(Find<Button>(host, "BtnListPrev").IsEnabled);
            Assert.False(Find<Button>(host, "BtnListNext").IsEnabled);
        });
    }

    [Fact]
    public void Host_LoadFailure_ShowsErrorCodeAndMessage()
    {
        UiSmokeTests.RunOnUiThread(() =>
        {
            var host = NewHost(new FakePluginPageGateway());

            host.ApplyLoad(new PluginPageLoadResultDto
            {
                Ok = false,
                ErrorCode = "NOT_FOUND",
                ErrorMessage = "插件未加载或不存在: demo",
            });

            var error = Find<TextBlock>(host, "TextError");
            Assert.Equal(Visibility.Visible, error.Visibility);
            Assert.Contains("[NOT_FOUND]", error.Text);
            Assert.Contains("插件未加载或不存在", error.Text);
        });
    }

    [Fact]
    public void Host_UnknownTemplate_ShowsProtocolError()
    {
        UiSmokeTests.RunOnUiThread(() =>
        {
            var host = NewHost(new FakePluginPageGateway());

            host.ApplyLoad(new PluginPageLoadResultDto
            {
                Ok = true,
                Template = "xaml",
                Title = "坏插件",
                Data = JsonDocument.Parse("{}").RootElement,
            });

            var error = Find<TextBlock>(host, "TextError");
            Assert.Equal(Visibility.Visible, error.Visibility);
            Assert.Contains("[INVALID_DATA]", error.Text);
        });
    }

    // ------------------------------------------------------------------
    // PluginPageHost：browser
    // ------------------------------------------------------------------

    [Fact]
    public void Host_BrowserTemplate_RendersFileListImagePagerAndBitmap()
    {
        UiSmokeTests.RunOnUiThread(() =>
        {
            var gateway = new FakePluginPageGateway();
            var host = NewHost(gateway);

            host.ApplyLoad(BrowserResult(
                itemIndex: 1,
                imageIndex: 0,
                imageCount: 5,
                itemCount: 3));

            var list = Find<ListBox>(host, "BrowserFileList");
            Assert.Equal(3, list.Items.Count);
            Assert.Equal(1, list.SelectedIndex);
            Assert.Equal(Visibility.Visible, Find<Grid>(host, "PanelBrowser").Visibility);
            Assert.Equal(Visibility.Visible, Find<StackPanel>(host, "PanelImageBar").Visibility);
            Assert.Equal("第 1 页 / 共 5 页", Find<TextBlock>(host, "TextImagePage").Text);
            Assert.False(Find<Button>(host, "BtnImagePrev").IsEnabled);
            Assert.True(Find<Button>(host, "BtnImageNext").IsEnabled);
            Assert.Equal(Visibility.Collapsed, Find<Image>(host, "ImagePage").Visibility);

            // 图片走 UI 线程建 BitmapImage（此处直接喂 bytes，验证解码与展示）
            host.ApplyImage(Convert.FromBase64String(OnePixelPngBase64));

            var image = Find<Image>(host, "ImagePage");
            Assert.NotNull(image.Source);
            Assert.IsType<System.Windows.Media.Imaging.BitmapImage>(image.Source);
            Assert.Equal(Visibility.Visible, image.Visibility);
            Assert.Equal(
                Visibility.Collapsed,
                Find<TextBlock>(host, "TextImageHint").Visibility);

            // 取图失败：显示图片占位文案，不抛异常
            host.ApplyImage(null);
            Assert.Null(image.Source);
            Assert.Equal("图片加载失败", Find<TextBlock>(host, "TextImageHint").Text);
        });
    }

    [Fact]
    public void Host_BrowserTemplate_EmptyShowsEmptyText()
    {
        UiSmokeTests.RunOnUiThread(() =>
        {
            var host = NewHost(new FakePluginPageGateway());

            host.ApplyLoad(BrowserResult(
                itemIndex: -1,
                imageIndex: 0,
                imageCount: 0,
                itemCount: 0,
                emptyText: "压缩包目录为空"));

            var empty = Find<TextBlock>(host, "TextBrowserEmpty");
            Assert.Equal(Visibility.Visible, empty.Visibility);
            Assert.Equal("压缩包目录为空", empty.Text);
            Assert.Empty(Find<ListBox>(host, "BrowserFileList").Items);
            Assert.Equal(
                Visibility.Collapsed,
                Find<StackPanel>(host, "PanelImageBar").Visibility);
            Assert.False(Find<Button>(host, "BtnFilePrev").IsEnabled);
            Assert.False(Find<Button>(host, "BtnFileNext").IsEnabled);
        });
    }

    [Fact]
    public void Host_BrowserBack_PassesItemIndexMinusOneAndReturnsToList()
    {
        UiSmokeTests.RunOnUiThreadAsync(async () =>
        {
            var gateway = new FakePluginPageGateway();
            gateway.PendingLoads.Enqueue(BrowserResult(
                itemIndex: -1, imageIndex: 0, imageCount: 0, itemCount: 3));
            var host = NewHost(gateway);

            host.ApplyLoad(BrowserResult(
                itemIndex: 1, imageIndex: 0, imageCount: 5, itemCount: 3));
            Assert.Equal(Visibility.Visible, Find<StackPanel>(host, "PanelImageBar").Visibility);

            // 返回按钮与 Esc 共用同一条路径：回列表 = 带 item_index=-1 重新 load
            await host.BackToFileListAsync();

            Assert.Single(gateway.LoadCalls);
            Assert.Contains("\"item_index\":-1", gateway.LoadCalls[0]);
            Assert.Equal(
                Visibility.Collapsed,
                Find<StackPanel>(host, "PanelImageBar").Visibility);
        });
    }

    [Fact]
    public void Host_AwaitFlow_UsesGatewayAndRendersTemplate()
    {
        UiSmokeTests.RunOnUiThreadAsync(async () =>
        {
            var gateway = new FakePluginPageGateway();
            gateway.PendingLoads.Enqueue(ListResult(
                page: 1, totalPages: 2, rows: 1, rowActions: 0, emptyText: ""));
            var host = NewHost(gateway);

            await host.LoadAsync(1);

            Assert.Single(gateway.LoadCalls);
            Assert.Contains("\"page\":1", gateway.LoadCalls[0]);
            Assert.Single(Find<DataGrid>(host, "GridRows").Items.Cast<object>());
            Assert.Equal(
                Visibility.Visible,
                Find<Grid>(host, "PanelList").Visibility);
        });
    }

    // ------------------------------------------------------------------
    // PluginPageHost：action
    // ------------------------------------------------------------------

    [Fact]
    public void Host_ActionTemplate_RendersFilesButtonsStatusAndRefresh()
    {
        UiSmokeTests.RunOnUiThread(() =>
        {
            var host = NewHost(new FakePluginPageGateway());

            host.ApplyLoad(ActionResult(
                state: "running",
                statusText: "服务运行中：http://127.0.0.1:8080",
                actionCount: 2,
                itemCount: 2,
                port: 8080));

            Assert.Equal(Visibility.Visible, Find<Grid>(host, "PanelAction").Visibility);
            Assert.Equal(2, Find<ListBox>(host, "ActionFileList").Items.Count);
            Assert.Equal(
                2,
                Find<StackPanel>(host, "PanelActionButtons").Children.Count);
            Assert.Equal(
                "服务运行中：http://127.0.0.1:8080",
                Find<TextBlock>(host, "TextActionStatus").Text);
            Assert.Same(
                Application.Current.FindResource("SecondaryButton"),
                Find<Button>(host, "BtnActionRefresh").Style);
        });
    }

    [Fact]
    public void Host_TryHandleEscape_OnlyAppliesToImageView()
    {
        UiSmokeTests.RunOnUiThread(() =>
        {
            var host = NewHost(new FakePluginPageGateway());

            host.ApplyLoad(ListResult(
                page: 1, totalPages: 1, rows: 1, rowActions: 0, emptyText: ""));
            Assert.False(host.TryHandleEscape());

            host.ApplyLoad(BrowserResult(
                itemIndex: -1, imageIndex: 0, imageCount: 0, itemCount: 3));
            Assert.False(host.TryHandleEscape());

            host.ApplyLoad(BrowserResult(
                itemIndex: 0, imageIndex: 0, imageCount: 5, itemCount: 3));
            Assert.True(host.IsViewingImage);
            Assert.True(host.TryHandleEscape());
        });
    }

    [Fact]
    public void Host_ActionFailure_ShowsErrorCodeAndKeepsPanel()
    {
        UiSmokeTests.RunOnUiThreadAsync(async () =>
        {
            var host = NewHost(new FakePluginPageGateway());
            host.ApplyLoad(ActionResult(
                state: "idle", statusText: "未启动", actionCount: 1, itemCount: 1, port: 0));

            await host.ApplyActionResultAsync(new PluginPageActionResultDto
            {
                Ok = false,
                ErrorCode = "NOT_IMPLEMENTED",
                ErrorMessage = "插件 demo 的页面 demo.main 未实现动作钩子: start",
            });

            var error = Find<TextBlock>(host, "TextError");
            Assert.Equal(Visibility.Visible, error.Visibility);
            Assert.Contains("[NOT_IMPLEMENTED]", error.Text);
            Assert.Contains("未实现动作钩子", error.Text);
            // 失败不清空当前页面，用户可修正后重试
            Assert.Equal(Visibility.Visible, Find<Grid>(host, "PanelAction").Visibility);
        });
    }

    [Fact]
    public void Host_ActionRefresh_ReloadsPage()
    {
        UiSmokeTests.RunOnUiThreadAsync(async () =>
        {
            var gateway = new FakePluginPageGateway();
            gateway.PendingLoads.Enqueue(ActionResult(
                state: "running", statusText: "已启动", actionCount: 1, itemCount: 1, port: 9000));
            var host = NewHost(gateway);
            host.ApplyLoad(ActionResult(
                state: "idle", statusText: "未启动", actionCount: 1, itemCount: 1, port: 0));

            await host.ApplyActionResultAsync(new PluginPageActionResultDto
            {
                Ok = true,
                Refresh = true,
                Message = "已启动",
            });

            Assert.Single(gateway.LoadCalls);
            Assert.Equal("已启动", Find<TextBlock>(host, "TextActionStatus").Text);
        });
    }

    [Fact]
    public void Host_ActionData_ReplacesPayloadWithoutReload()
    {
        UiSmokeTests.RunOnUiThreadAsync(async () =>
        {
            var gateway = new FakePluginPageGateway();
            var host = NewHost(gateway);
            host.ApplyLoad(ListResult(
                page: 1, totalPages: 1, rows: 1, rowActions: 0, emptyText: ""));

            var updated = ListResult(
                page: 1, totalPages: 1, rows: 3, rowActions: 0, emptyText: "");
            await host.ApplyActionResultAsync(new PluginPageActionResultDto
            {
                Ok = true,
                Refresh = false,
                Data = updated.Data,
                Message = "已刷新行数据",
            });

            Assert.Empty(gateway.LoadCalls);
            Assert.Equal(3, Find<DataGrid>(host, "GridRows").Items.Count);
        });
    }

    // ------------------------------------------------------------------
    // 组件构造与假网关
    // ------------------------------------------------------------------

    [Fact]
    public void FeatureView_ConstructsOnStaThread_WithPluginAndPagePanels()
    {
        UiSmokeTests.RunOnUiThread(() =>
        {
            var view = new PluginFeaturesView();

            Assert.Equal(
                Visibility.Visible,
                UiSmokeTests.Find<DockPanel>(view, "PanelPlugins").Visibility);
            Assert.Equal(
                Visibility.Collapsed,
                UiSmokeTests.Find<DockPanel>(view, "PanelPages").Visibility);
            Assert.IsType<ListBox>(UiSmokeTests.Find<ListBox>(view, "FeatureList"));
            Assert.IsType<ListBox>(UiSmokeTests.Find<ListBox>(view, "PageList"));
        });
    }

    [Fact]
    public void PluginPageHost_ConstructsOnStaThread_WithoutCallingBackend()
    {
        UiSmokeTests.RunOnUiThread(() =>
        {
            var gateway = new FakePluginPageGateway();
            var host = NewHost(gateway);

            Assert.Equal("demo", host.PluginId);
            Assert.Equal("demo.main", host.PageId);
            Assert.Empty(gateway.LoadCalls); // 未 Loaded 不自动调用
            Assert.Equal(
                Visibility.Collapsed,
                UiSmokeTests.Find<Grid>(host, "PanelList").Visibility);
        });
    }

    // ------------------------------------------------------------------
    // 工具
    // ------------------------------------------------------------------

    private static PluginPageHost NewHost(FakePluginPageGateway gateway) =>
        new("demo", "demo.main", "演示页", gateway);

    private static T Find<T>(FrameworkElement root, string name) where T : class =>
        UiSmokeTests.Find<T>(root, name);

    private static PluginPageLoadResultDto ListResult(
        int page,
        int totalPages,
        int rows,
        int rowActions,
        string emptyText)
    {
        var rowList = Enumerable.Range(0, rows)
            .Select(index => $"{{\"time\":\"2026-10-{index + 1:00}\",\"book_id\":\"{index + 100}\"}}");
        var actionList = Enumerable.Range(0, rowActions)
            .Select(index =>
                $"{{\"id\":\"act{index}\",\"title\":\"操作{index}\",\"primary\":{(index == 0 ? "true" : "false")}}}");

        string json = $$"""
        {
          "ok": true,
          "template": "list",
          "title": "演示页",
          "data": {
            "columns": [
              {"key": "time", "title": "时间"},
              {"key": "book_id", "title": "编号", "width": 80}
            ],
            "rows": [{{string.Join(",", rowList)}}],
            "page": {{page}},
            "page_size": 50,
            "total": {{rows * totalPages}},
            "total_pages": {{totalPages}},
            "row_actions": [{{string.Join(",", actionList)}}],
            "empty_text": "{{emptyText}}"
          }
        }
        """;

        return Parse<PluginPageLoadResultDto>(json);
    }

    private static PluginPageLoadResultDto BrowserResult(
        int itemIndex,
        int imageIndex,
        int imageCount,
        int itemCount,
        string emptyText = "")
    {
        var items = Enumerable.Range(0, itemCount)
            .Select(index => $"{{\"name\":\"{index + 1:000}.zip\",\"path\":\"C:/z/{index + 1:000}.zip\"}}");

        string json = $$"""
        {
          "ok": true,
          "template": "browser",
          "title": "ZIP 浏览",
          "data": {
            "items": [{{string.Join(",", items)}}],
            "page": 1,
            "page_size": 20,
            "total": {{itemCount}},
            "total_pages": 1,
            "item_index": {{itemIndex}},
            "image_index": {{imageIndex}},
            "image_count": {{imageCount}},
            "empty_text": "{{emptyText}}"
          }
        }
        """;

        return Parse<PluginPageLoadResultDto>(json);
    }

    private static PluginPageLoadResultDto ActionResult(
        string state,
        string statusText,
        int actionCount,
        int itemCount,
        int port)
    {
        var items = Enumerable.Range(0, itemCount)
            .Select(index => $"{{\"name\":\"{index + 1:000}.zip\",\"path\":\"C:/z/{index + 1:000}.zip\"}}");
        var actions = Enumerable.Range(0, actionCount)
            .Select(index => $"{{\"id\":\"act{index}\",\"title\":\"动作{index}\"}}");

        string json = $$"""
        {
          "ok": true,
          "template": "action",
          "title": "ZIP 网页浏览",
          "data": {
            "items": [{{string.Join(",", items)}}],
            "state": "{{state}}",
            "status_text": "{{statusText}}",
            "actions": [{{string.Join(",", actions)}}],
            "port": {{port}}
          }
        }
        """;

        return Parse<PluginPageLoadResultDto>(json);
    }

    /// <summary>假插件页面网关：只记录调用并返回预置 DTO（离线、无 Python）。</summary>
    private sealed class FakePluginPageGateway : IPluginPageGateway
    {
        /// <summary>按顺序消费的载入结果；为空时返回失败形态。</summary>
        public Queue<PluginPageLoadResultDto> PendingLoads { get; } = new();

        /// <summary>每次 LoadAsync 的“参数 JSON”记录（断言参数透传用）。</summary>
        public List<string> LoadCalls { get; } = [];

        public byte[]? ImageResult { get; set; }

        public List<int> ImageCalls { get; } = [];

        public PluginPageActionResultDto? ActionResult { get; set; }

        public List<string> ActionCalls { get; } = [];

        public Task<PluginPageLoadResultDto> LoadAsync(
            string pluginId,
            string pageId,
            IReadOnlyDictionary<string, object?>? parameters)
        {
            LoadCalls.Add(PluginPageBridge.SerializeParameters(parameters));
            if (PendingLoads.Count > 0)
            {
                return Task.FromResult(PendingLoads.Dequeue());
            }

            return Task.FromResult(new PluginPageLoadResultDto
            {
                Ok = false,
                ErrorCode = "NOT_FOUND",
                ErrorMessage = $"假网关没有更多页面数据: {pluginId}/{pageId}",
            });
        }

        public Task<PluginPageActionResultDto> ActionAsync(
            string pluginId,
            string pageId,
            string actionId,
            IReadOnlyDictionary<string, object?>? payload)
        {
            ActionCalls.Add(actionId + ":" + PluginPageBridge.SerializeParameters(payload));
            return Task.FromResult(ActionResult ?? new PluginPageActionResultDto { Ok = true });
        }

        public Task<byte[]?> ImageAsync(string pluginId, string pageId, int index)
        {
            ImageCalls.Add(index);
            return Task.FromResult(ImageResult);
        }
    }
}
