using System.ComponentModel;
using System.Text.Json;
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
        Assert.Null(bare.Intent);

        PluginPageActionResultDto fail = Parse<PluginPageActionResultDto>(
            """{"ok": false, "error_code": "NOT_IMPLEMENTED", "error_message": "未实现动作钩子"}""");

        Assert.False(fail.Ok);
        Assert.Equal("NOT_IMPLEMENTED", fail.ErrorCode);
        Assert.Null(fail.Data);
    }

    [Fact]
    public void ActionResult_ReadsDetailAndDownloadIntents()
    {
        PluginPageActionResultDto detail = Parse<PluginPageActionResultDto>(
            """
            {"ok": true, "refresh": false, "data": null, "message": "",
             "intent": {"kind": "detail", "book_id": "123"}}
            """);
        Assert.NotNull(detail.Intent);
        Assert.Equal("detail", detail.Intent!.Kind);
        Assert.Equal("123", detail.Intent.BookId);
        Assert.Empty(detail.Intent.BookIds);

        PluginPageActionResultDto download = Parse<PluginPageActionResultDto>(
            """
            {"ok": true, "intent": {"kind": "download", "book_ids": ["456", "789"]}}
            """);
        Assert.NotNull(download.Intent);
        Assert.Equal("download", download.Intent!.Kind);
        Assert.Equal(new[] { "456", "789" }, download.Intent.BookIds);
    }

    [Fact]
    public void LoadResult_ReadsListFilters()
    {
        const string json = """
        {
          "ok": true,
          "template": "list",
          "title": "收藏夹",
          "data": {
            "columns": [{"key": "title", "title": "标题"}],
            "rows": [],
            "filters": [
              {"key": "folder_id", "label": "收藏夹",
               "options": [
                 {"value": "0", "label": "全部"},
                 {"value": "7", "label": "子夹"}
               ],
               "value": "7"},
              {"key": "sort", "label": "排序",
               "options": [
                 {"value": "mr", "label": "最近收藏"},
                 {"value": "mv", "label": "最多点击"}
               ],
               "value": "mv"}
            ],
            "empty_text": "收藏夹是空的"
          }
        }
        """;

        PluginPageLoadResultDto dto = Parse<PluginPageLoadResultDto>(json);
        PluginListPageDto page =
            Assert.IsType<PluginListPageDto>(PluginPageData.ToList(dto.Data));

        Assert.Equal(2, page.Filters.Count);
        Assert.Equal("folder_id", page.Filters[0].Key);
        Assert.Equal("收藏夹", page.Filters[0].Label);
        Assert.Equal("7", page.Filters[0].Value);
        Assert.Equal(2, page.Filters[0].Options.Count);
        Assert.Equal("子夹", page.Filters[0].Options[1].Label);
        Assert.Equal("sort", page.Filters[1].Key);
        Assert.Equal("mv", page.Filters[1].Value);
    }

    [Fact]
    public void LoadResult_ReadsRowDoubleClickActionAndThumbBookId()
    {
        const string json = """
        {
          "ok": true,
          "template": "list",
          "title": "收藏夹",
          "data": {
            "columns": [{"key": "title", "title": "标题"}],
            "rows": [
              {"book_id": "123", "thumb_book_id": "123", "title": "标题A"},
              {"book_id": "456", "thumb_book_id": 789, "title": "标题B"},
              {"book_id": "789", "title": "无封面"}
            ],
            "row_double_click_action": "detail",
            "row_actions": [{"id": "detail", "title": "详情", "primary": true}],
            "empty_text": "收藏夹是空的"
          }
        }
        """;

        PluginPageLoadResultDto dto = Parse<PluginPageLoadResultDto>(json);
        PluginListPageDto page =
            Assert.IsType<PluginListPageDto>(PluginPageData.ToList(dto.Data));

        // 页级双击动作与行操作并列透传
        Assert.Equal("detail", page.RowDoubleClickAction);
        Assert.Equal("detail", page.RowActions[0].Id);

        // 行内 thumb_book_id 只在是字符串时生效，且原始行字段一个不少
        Assert.Equal("123", PluginPageRowFields.GetThumbBookId(page.Rows[0]));
        Assert.Equal("", PluginPageRowFields.GetThumbBookId(page.Rows[1]));
        Assert.Equal("", PluginPageRowFields.GetThumbBookId(page.Rows[2]));
        Assert.Equal("123", page.Rows[0]["thumb_book_id"].GetString());
        Assert.Equal("标题A", page.Rows[0]["title"].GetString());

        // 缺省字段（旧插件）不报错：动作空串、行内无 thumb_book_id
        PluginListPageDto legacy = Parse<PluginListPageDto>(
            """{"columns": [{"key": "time", "title": "时间"}], "rows": [{"time": "x"}]}""");
        Assert.Equal("", legacy.RowDoubleClickAction);
        Assert.Equal("", PluginPageRowFields.GetThumbBookId(legacy.Rows[0]));
        Assert.Equal("", PluginPageRowFields.GetThumbBookId(null));
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
            // 单列资源行模板（PCL2 样式），行操作按钮在模板内部，不再单独占列
            Assert.Single(grid.Columns);
            Assert.Equal(2, grid.Items.Count);
            Assert.Single(Assert.IsType<PluginRowView>(grid.Items[0]).Actions);
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
            LayoutList(host);

            object firstRow = grid.Items[0];
            var cell = grid.Columns[0].GetCellContent(firstRow) as FrameworkElement;
            Assert.NotNull(cell);
            cell!.UpdateLayout();

            // 资源行把第一列当标题、book_id 当副标题（不再有独立的文本单元格）
            Assert.NotNull(FindVisualChild<TextBlock>(
                cell,
                block => block.Text == "2026-10-01" && block.Visibility == Visibility.Visible));
            Assert.NotNull(FindVisualChild<TextBlock>(
                cell,
                block => block.Text == "100" && block.Visibility == Visibility.Visible));
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
    public void Host_ListTemplate_RendersFiltersAndReloadsWithSelectedValues()
    {
        UiSmokeTests.RunOnUiThreadAsync(async () =>
        {
            var gateway = new FakePluginPageGateway();
            gateway.PendingLoads.Enqueue(ListResultWithFilters(folderValue: "7", sortValue: "mv"));
            var host = NewHost(gateway);

            host.ApplyLoad(ListResultWithFilters(folderValue: "7", sortValue: "mv"));

            var filtersPanel = Find<StackPanel>(host, "PanelListFilters");
            Assert.Equal(Visibility.Visible, filtersPanel.Visibility);
            Assert.Equal(2, host.ListFilterCombos.Count);

            ComboBox folder = host.ListFilterCombos[0];
            Assert.Equal("收藏夹", (filtersPanel.Children[0] as TextBlock)?.Text);
            Assert.Equal(2, folder.Items.Count);
            Assert.Equal(1, folder.SelectedIndex); // 当前值 "7"（第二项）

            ComboBox sort = host.ListFilterCombos[1];
            Assert.Equal(1, sort.SelectedIndex); // 当前值 "mv"（第二项）

            // 用户切换收藏夹 → 以 {page:1, folder_id, sort} 重新加载
            folder.SelectedIndex = 0;
            await WaitUntilAsync(() => gateway.LoadCalls.Count >= 1);

            using JsonDocument document = JsonDocument.Parse(gateway.LoadCalls[0]);
            JsonElement root = document.RootElement;
            Assert.Equal(1, root.GetProperty("page").GetInt32());
            Assert.Equal("0", root.GetProperty("folder_id").GetString());
            Assert.Equal("mv", root.GetProperty("sort").GetString());
            Assert.Equal(Visibility.Collapsed, Find<TextBlock>(host, "TextError").Visibility);
        });
    }

    [Fact]
    public void Host_ListTemplate_HidesFilterBarWithoutFilters()
    {
        UiSmokeTests.RunOnUiThread(() =>
        {
            var host = NewHost(new FakePluginPageGateway());

            host.ApplyLoad(ListResult(
                page: 1, totalPages: 1, rows: 1, rowActions: 0, emptyText: ""));

            Assert.Empty(host.ListFilterCombos);
            Assert.Equal(
                Visibility.Collapsed,
                Find<StackPanel>(host, "PanelListFilters").Visibility);
        });
    }

    // ------------------------------------------------------------------
    // PluginPageHost：list 封面列与行双击（阶段 4-4 扩展）
    // ------------------------------------------------------------------

    [Fact]
    public void Host_ListTemplate_ShowsCoverColumnOnlyWhenRowsHaveThumbBookId()
    {
        UiSmokeTests.RunOnUiThread(() =>
        {
            var host = NewHost(new FakePluginPageGateway());

            // 旧插件：行内没有 thumb_book_id → 不显示封面（但仍是单列资源行模板）
            host.ApplyLoad(ListResult(
                page: 1, totalPages: 1, rows: 2, rowActions: 1, emptyText: ""));
            var legacyGrid = Find<DataGrid>(host, "GridRows");
            Assert.False(host.HasCoverColumn);
            Assert.Single(legacyGrid.Columns);

            // 收藏夹：行内带 thumb_book_id → 同一列内显示 44×58 封面
            host.ApplyLoad(CoverListResult(new[] { "123", "456" }, "detail"));
            var grid = Find<DataGrid>(host, "GridRows");
            Assert.True(host.HasCoverColumn);
            Assert.Single(grid.Columns);
            Assert.IsType<DataGridTemplateColumn>(grid.Columns[0]);
            Assert.Equal(DataGridLengthUnitType.Star, grid.Columns[0].Width.UnitType);
            Assert.Equal(string.Empty, grid.Columns[0].Header as string);
        });
    }

    [Fact]
    public void Host_ListTemplate_MapsRowsToPclResourceFields()
    {
        UiSmokeTests.RunOnUiThread(() =>
        {
            var host = NewHost(new FakePluginPageGateway());
            host.ApplyLoad(CoverListResult(new[] { "123" }, "detail"));

            var grid = Find<DataGrid>(host, "GridRows");
            var row = Assert.IsType<PluginRowView>(grid.Items[0]);

            Assert.Equal("标题1", row.TitleText);
            Assert.Equal("作者1", row.SubtitleText);
            Assert.Equal(new[] { "页数 10", "收藏数 20" }, row.Tags);
            Assert.Contains(row.MetaItems, item => item.Text == "本子号 123");
            Assert.Contains(row.MetaItems, item => item.Text == "收藏夹");
            Assert.Equal(2, row.Actions.Count);
            Assert.Equal(Visibility.Collapsed, row.LogoVisibility);
            Assert.Equal(Visibility.Visible, row.PlaceholderVisibility);
        });
    }

    [Fact]
    public void Host_ListTemplate_LoadsCoversOnlyForRealizedRows()
    {
        UiSmokeTests.RunOnUiThread(() =>
        {
            var fetchCalls = new List<string>();
            var host = NewHost(new FakePluginPageGateway());
            host.CoverFetcher = bookId =>
            {
                fetchCalls.Add(bookId);
                return Task.FromResult<byte[]?>(
                    Convert.FromBase64String(OnePixelPngBase64));
            };

            string[] ids = Enumerable.Range(0, 40)
                .Select(index => (index + 100).ToString())
                .ToArray();
            host.ApplyLoad(CoverListResult(ids, "detail"));

            var grid = Find<DataGrid>(host, "GridRows");
            LayoutList(host);

            var realizedIds = new List<string>();
            for (int index = 0; index < grid.Items.Count; index++)
            {
                if (grid.ItemContainerGenerator.ContainerFromIndex(index) is DataGridRow container
                    && container.Item is PluginRowView row)
                {
                    realizedIds.Add(row.ThumbBookId);
                }
            }

            // 虚拟化生效：只创建可视区内的行容器
            Assert.NotEmpty(realizedIds);
            Assert.True(
                realizedIds.Count < grid.Items.Count,
                "40 行应靠虚拟化只创建可见行容器");

            // 只对“当前可见/已加载”的行取图：次数 = 已创建行容器数
            Assert.Equal(realizedIds.Count, fetchCalls.Count);
            Assert.Equal(
                realizedIds.OrderBy(id => id, StringComparer.Ordinal),
                fetchCalls.OrderBy(id => id, StringComparer.Ordinal));

            // 已取到封面的行显示图片；取图不改变列表本身
            Assert.All(
                grid.Items.Cast<PluginRowView>()
                    .Where(row => realizedIds.Contains(row.ThumbBookId)),
                row => Assert.NotNull(row.Cover));
            Assert.Equal(
                Visibility.Collapsed,
                Find<TextBlock>(host, "TextError").Visibility);
        });
    }

    [Fact]
    public void Host_ListTemplate_CoverFetchDeduplicatesByBookId()
    {
        UiSmokeTests.RunOnUiThreadAsync(async () =>
        {
            var fetchCalls = new List<string>();
            var host = NewHost(new FakePluginPageGateway());
            host.CoverFetcher = bookId =>
            {
                fetchCalls.Add(bookId);
                return Task.FromResult<byte[]?>(
                    Convert.FromBase64String(OnePixelPngBase64));
            };

            host.ApplyLoad(CoverListResult(new[] { "123", "123" }, "detail"));
            var grid = Find<DataGrid>(host, "GridRows");
            var first = Assert.IsType<PluginRowView>(grid.Items[0]);
            var second = Assert.IsType<PluginRowView>(grid.Items[1]);

            await host.EnsureRowCoverAsync(first);
            await host.EnsureRowCoverAsync(second);
            // 行回收后再进入：仍然只复用缓存，不重新取图
            await host.EnsureRowCoverAsync(first);

            Assert.Equal(new[] { "123" }, fetchCalls);
            Assert.NotNull(first.Cover);
            Assert.Same(first.Cover, second.Cover);
            Assert.Equal(
                Visibility.Collapsed,
                Find<TextBlock>(host, "TextError").Visibility);
        });
    }

    [Fact]
    public void Host_ListTemplate_CoverFailureShowsXWithoutBreakingList()
    {
        UiSmokeTests.RunOnUiThreadAsync(async () =>
        {
            var fetchCalls = new List<string>();
            var host = NewHost(new FakePluginPageGateway());
            host.CoverFetcher = bookId =>
            {
                fetchCalls.Add(bookId);
                // "123" 取图失败；"456" 返回无法解码的字节（解码失败同样显示 X）
                return Task.FromResult<byte[]?>(
                    bookId == "123" ? null : [1, 2, 3]);
            };

            host.ApplyLoad(CoverListResult(new[] { "123", "456" }, "detail"));
            var grid = Find<DataGrid>(host, "GridRows");
            var noBytes = Assert.IsType<PluginRowView>(grid.Items[0]);
            var badBytes = Assert.IsType<PluginRowView>(grid.Items[1]);

            await host.EnsureRowCoverAsync(noBytes);
            await host.EnsureRowCoverAsync(badBytes);

            foreach (PluginRowView row in new[] { noBytes, badBytes })
            {
                Assert.Null(row.Cover);
                Assert.True(row.CoverSettled);
                Assert.Equal(Visibility.Visible, row.FailureVisibility);
                Assert.Equal(Visibility.Collapsed, row.PlaceholderVisibility);
                Assert.Equal(Visibility.Collapsed, row.CoverVisibility);
            }

            // 失败缓存：同一 book_id 不再重复取图
            await host.EnsureRowCoverAsync(noBytes);
            Assert.Equal(new[] { "123", "456" }, fetchCalls);

            // 失败不影响列表渲染，也不弹错误框
            Assert.Equal(2, grid.Items.Count);
            Assert.Equal(Visibility.Visible, Find<Grid>(host, "PanelList").Visibility);
            Assert.Equal(
                Visibility.Collapsed,
                Find<TextBlock>(host, "TextError").Visibility);

            // 封面格真的显示暗红底 X（模板绑定生效，不只是行模型状态）
            LayoutList(host);
            var coverCell = grid.Columns[0].GetCellContent(noBytes) as FrameworkElement;
            Assert.NotNull(coverCell);
            coverCell!.UpdateLayout();
            Assert.NotNull(FindVisualChild<TextBlock>(
                coverCell,
                block => block.Text == "X" && block.Visibility == Visibility.Visible));
        });
    }

    [Fact]
    public void Host_ListTemplate_DoubleClickRunsActionAndDispatchesIntent()
    {
        UiSmokeTests.RunOnUiThreadAsync(async () =>
        {
            var gateway = new FakePluginPageGateway
            {
                ActionResult = new PluginPageActionResultDto
                {
                    Ok = true,
                    Intent = new PluginPageIntentDto { Kind = "detail", BookId = "123" },
                },
            };
            var host = NewHost(gateway);
            var handler = new FakeUiIntentHandler();
            host.IntentHandler = handler;

            host.ApplyLoad(CoverListResult(new[] { "123", "456" }, "detail"));
            var grid = Find<DataGrid>(host, "GridRows");
            grid.SelectedItem = grid.Items[0];

            Assert.Equal("detail", host.RowDoubleClickAction);
            grid.RaiseEvent(new MouseButtonEventArgs(Mouse.PrimaryDevice, 0, MouseButton.Left)
            {
                RoutedEvent = Control.MouseDoubleClickEvent,
            });
            await WaitUntilAsync(() => gateway.ActionCalls.Count >= 1);

            // 双击 = 以 {row, row_index, page} 调 page 级 row_double_click_action
            Assert.StartsWith("detail:", gateway.ActionCalls[0]);
            Assert.Contains("\"row_index\":0", gateway.ActionCalls[0]);
            Assert.Contains("\"page\":1", gateway.ActionCalls[0]);
            Assert.Contains("\"book_id\":\"123\"", gateway.ActionCalls[0]);

            // 插件返回的 intent 原样交给注入的转发器（C# 不打开窗口）
            PluginPageIntentDto intent = Assert.Single(handler.Intents);
            Assert.Equal("detail", intent.Kind);
            Assert.Equal("123", intent.BookId);
        });
    }

    [Fact]
    public void Host_ListTemplate_DoubleClickWithoutActionDoesNothing()
    {
        UiSmokeTests.RunOnUiThreadAsync(async () =>
        {
            var gateway = new FakePluginPageGateway();
            var host = NewHost(gateway);
            host.ApplyLoad(CoverListResult(new[] { "123" }, rowDoubleClickAction: ""));
            var grid = Find<DataGrid>(host, "GridRows");
            grid.SelectedItem = grid.Items[0];

            Assert.Equal(string.Empty, host.RowDoubleClickAction);
            grid.RaiseEvent(new MouseButtonEventArgs(Mouse.PrimaryDevice, 0, MouseButton.Left)
            {
                RoutedEvent = Control.MouseDoubleClickEvent,
            });
            await Task.Delay(50);
            Assert.Empty(gateway.ActionCalls);

            // 内部入口同样防御：动作 id 为空时双击不派发任何调用
            await host.RunListRowDoubleClickAsync(
                Assert.IsType<PluginRowView>(grid.Items[0]));
            Assert.Empty(gateway.ActionCalls);
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
    public void Host_BrowserFolderChoice_ShowsButtonOnlyWhenAllowed()
    {
        UiSmokeTests.RunOnUiThread(() =>
        {
            var host = NewHost(new FakePluginPageGateway());

            // ZIP 浏览插件首个状态：没有目录 → 按钮可见、空态提示「选择文件夹…」
            host.ApplyLoad(BrowserResult(
                itemIndex: -1,
                imageIndex: 0,
                imageCount: 0,
                itemCount: 0,
                emptyText: "选择文件夹…",
                allowFolderChoice: true));
            Assert.True(host.IsFolderChoiceVisible);
            Assert.Equal("选择文件夹…", Find<Button>(host, "BtnChooseFolder").Content as string);
            Assert.Equal("选择文件夹…", Find<TextBlock>(host, "TextBrowserEmpty").Text);

            // Python 不允许（或普通 browser 页）→ 按钮隐藏，可见性不自己做业务判断
            host.ApplyLoad(BrowserResult(
                itemIndex: -1,
                imageIndex: 0,
                imageCount: 0,
                itemCount: 2,
                allowFolderChoice: false,
                folder: "C:/z"));
            Assert.False(host.IsFolderChoiceVisible);
            Assert.Equal(
                Visibility.Collapsed,
                Find<Button>(host, "BtnChooseFolder").Visibility);
        });
    }

    [Fact]
    public void Host_BrowserFolderChoice_ClickReloadsWithFolderParam()
    {
        UiSmokeTests.RunOnUiThreadAsync(async () =>
        {
            var gateway = new FakePluginPageGateway();
            gateway.PendingLoads.Enqueue(BrowserResult(
                itemIndex: -1,
                imageIndex: 0,
                imageCount: 0,
                itemCount: 2,
                allowFolderChoice: true,
                folder: @"D:\zip-fixture"));
            var host = NewHost(gateway);
            host.FolderPicker = () => @"D:\zip-fixture";

            host.ApplyLoad(BrowserResult(
                itemIndex: -1,
                imageIndex: 0,
                imageCount: 0,
                itemCount: 0,
                emptyText: "选择文件夹…",
                allowFolderChoice: true));
            var button = Find<Button>(host, "BtnChooseFolder");
            Assert.Equal(Visibility.Visible, button.Visibility);

            // 等价于用户点击「选择文件夹…」：文件夹选择器已注入假实现
            button.RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
            await WaitUntilAsync(() => gateway.LoadCalls.Count >= 1);

            using (JsonDocument document = JsonDocument.Parse(gateway.LoadCalls[0]))
            {
                JsonElement root = document.RootElement;
                Assert.Equal(1, root.GetProperty("page").GetInt32());
                Assert.Equal(-1, root.GetProperty("item_index").GetInt32());
                Assert.Equal(@"D:\zip-fixture", root.GetProperty("folder").GetString());
            }
            Assert.Equal(@"D:\zip-fixture", host.CurrentFolder);

            // 之后翻文件页仍带同一个 folder（目录状态由 C# 保持，内容由 Python 决定）
            await host.LoadAsync(2);
            using (JsonDocument document = JsonDocument.Parse(gateway.LoadCalls[1]))
            {
                Assert.Equal(
                    @"D:\zip-fixture",
                    document.RootElement.GetProperty("folder").GetString());
            }
        });
    }

    [Fact]
    public void Host_BrowserFolderChoice_CancelKeepsPageWithoutReload()
    {
        UiSmokeTests.RunOnUiThreadAsync(async () =>
        {
            var gateway = new FakePluginPageGateway();
            var host = NewHost(gateway);
            host.FolderPicker = () => null; // 用户点了取消

            host.ApplyLoad(BrowserResult(
                itemIndex: -1,
                imageIndex: 0,
                imageCount: 0,
                itemCount: 0,
                emptyText: "选择文件夹…",
                allowFolderChoice: true));

            Find<Button>(host, "BtnChooseFolder")
                .RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
            await Task.Delay(50);

            Assert.Empty(gateway.LoadCalls);
            Assert.Equal(Visibility.Collapsed, Find<TextBlock>(host, "TextError").Visibility);
        });
    }

    /// <summary>轮询等待界面线程上的异步操作完成（不依赖真实计时赛跑）。</summary>
    private static async Task WaitUntilAsync(Func<bool> condition, int timeoutMs = 5000)
    {
        long deadline = Environment.TickCount64 + timeoutMs;
        while (!condition())
        {
            if (Environment.TickCount64 > deadline)
            {
                throw new TimeoutException("等待异步界面操作超时");
            }

            await Task.Delay(10);
        }
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

    [Fact]
    public void Host_ActionIntent_ForwardsDetailAndDownloadToInjectedHandler()
    {
        UiSmokeTests.RunOnUiThreadAsync(async () =>
        {
            var host = NewHost(new FakePluginPageGateway());
            var handler = new FakeUiIntentHandler();
            host.IntentHandler = handler;
            host.ApplyLoad(ListResult(
                page: 1, totalPages: 1, rows: 1, rowActions: 1, emptyText: ""));

            // detail：只把 book_id 转给主窗口（C# 不打开窗口、不做业务）
            await host.ApplyActionResultAsync(new PluginPageActionResultDto
            {
                Ok = true,
                Intent = new PluginPageIntentDto { Kind = "detail", BookId = "123" },
            });

            // download：只把 book_ids 转给主窗口（切下载页由主窗口负责）
            await host.ApplyActionResultAsync(new PluginPageActionResultDto
            {
                Ok = true,
                Intent = new PluginPageIntentDto
                {
                    Kind = "download",
                    BookIds = ["456", "789"],
                },
            });

            Assert.Equal(2, handler.Intents.Count);
            Assert.Equal("detail", handler.Intents[0].Kind);
            Assert.Equal("123", handler.Intents[0].BookId);
            Assert.Empty(handler.Intents[0].BookIds);
            Assert.Equal("download", handler.Intents[1].Kind);
            Assert.Equal(new[] { "456", "789" }, handler.Intents[1].BookIds);
            Assert.Equal(Visibility.Collapsed, Find<TextBlock>(host, "TextError").Visibility);

            // 未知 kind 不崩溃、不报错：C# 原样转发，是否处理由主窗口决定
            await host.ApplyActionResultAsync(new PluginPageActionResultDto
            {
                Ok = true,
                Intent = new PluginPageIntentDto { Kind = "unknown" },
            });
            Assert.Equal(3, handler.Intents.Count);
            Assert.Equal("unknown", handler.Intents[2].Kind);
            Assert.Equal(Visibility.Collapsed, Find<TextBlock>(host, "TextError").Visibility);
        });
    }

    [Fact]
    public void Host_ActionIntent_WithoutHandlerShowsProtocolError()
    {
        UiSmokeTests.RunOnUiThreadAsync(async () =>
        {
            var host = NewHost(new FakePluginPageGateway());
            host.ApplyLoad(ListResult(
                page: 1, totalPages: 1, rows: 1, rowActions: 1, emptyText: ""));

            await host.ApplyActionResultAsync(new PluginPageActionResultDto
            {
                Ok = true,
                Intent = new PluginPageIntentDto { Kind = "detail", BookId = "1" },
            });

            var error = Find<TextBlock>(host, "TextError");
            Assert.Equal(Visibility.Visible, error.Visibility);
            Assert.Contains("[NOT_IMPLEMENTED]", error.Text);
        });
    }

    // ------------------------------------------------------------------
    // PluginPageHost：action 模板的阶段 4-3 扩展（url / allow_folder_choice）
    // ------------------------------------------------------------------

    [Fact]
    public void Host_ActionTemplate_ShowsOpenBrowserOnlyWhenUrlPresent()
    {
        UiSmokeTests.RunOnUiThread(() =>
        {
            var host = NewHost(new FakePluginPageGateway());
            const string url = "http://127.0.0.1:51234/";

            // action payload 带 url → 「在浏览器中打开」按钮可见且可用
            host.ApplyLoad(ActionResult(
                state: "running",
                statusText: "正在浏览：http://127.0.0.1:51234/",
                actionCount: 1,
                itemCount: 2,
                port: 51234,
                url: url));
            Assert.True(host.IsOpenInBrowserVisible);
            Assert.Equal(url, host.CurrentActionUrl);
            var button = Find<Button>(host, "BtnActionOpenBrowser");
            Assert.Equal(Visibility.Visible, button.Visibility);
            Assert.True(button.IsEnabled);
            Assert.Equal("在浏览器中打开", button.Content as string);

            // 没有 url（未启动 / 已停止）→ 按钮隐藏，C# 不自己拼地址
            host.ApplyLoad(ActionResult(
                state: "idle",
                statusText: "已停止",
                actionCount: 1,
                itemCount: 2,
                port: 0,
                url: ""));
            Assert.False(host.IsOpenInBrowserVisible);
            Assert.Equal(string.Empty, host.CurrentActionUrl);
            Assert.Equal(
                Visibility.Collapsed,
                Find<Button>(host, "BtnActionOpenBrowser").Visibility);

            // 非 http(s) 地址不显示（不把任意协议交给 ShellExecute）
            host.ApplyLoad(ActionResult(
                state: "running",
                statusText: "运行中",
                actionCount: 1,
                itemCount: 1,
                port: 9000,
                url: "file:///C:/Windows/System32/calc.exe"));
            Assert.False(host.IsOpenInBrowserVisible);
        });
    }

    [Fact]
    public void Host_ActionTemplate_OpenBrowserForwardsUrlToInjectedOpener()
    {
        UiSmokeTests.RunOnUiThread(() =>
        {
            var host = NewHost(new FakePluginPageGateway());
            const string url = "http://127.0.0.1:51234/";
            host.ApplyLoad(ActionResult(
                state: "running",
                statusText: "正在浏览",
                actionCount: 1,
                itemCount: 1,
                port: 51234,
                url: url));

            string? opened = null;
            host.BrowserOpener = target =>
            {
                opened = target;
                return true;
            };

            Find<Button>(host, "BtnActionOpenBrowser")
                .RaiseEvent(new RoutedEventArgs(Button.ClickEvent));

            Assert.Equal(url, opened);
            Assert.Equal(
                Visibility.Collapsed,
                Find<TextBlock>(host, "TextError").Visibility);

            // 无 url 时点击不触发打开器（按钮不承担业务判断）
            host.ApplyLoad(ActionResult(
                state: "idle",
                statusText: "未启动",
                actionCount: 1,
                itemCount: 1,
                port: 0,
                url: ""));
            opened = null;
            Find<Button>(host, "BtnActionOpenBrowser")
                .RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
            Assert.Null(opened);
        });
    }

    // ------------------------------------------------------------------
    // PluginPageHost：action 成功返回 data.url → 自动唤起浏览器（缺陷 1）
    // ------------------------------------------------------------------

    [Fact]
    public void Host_ActionWithUrl_AutoOpensBrowserOnce()
    {
        UiSmokeTests.RunOnUiThreadAsync(async () =>
        {
            var host = NewHost(new FakePluginPageGateway());
            host.ApplyLoad(ActionResult(
                state: "idle", statusText: "未启动", actionCount: 1, itemCount: 1, port: 0));

            var opened = new List<string>();
            host.BrowserOpener = url =>
            {
                opened.Add(url);
                return true;
            };

            const string url = "http://127.0.0.1:51234/";
            await host.ApplyActionResultAsync(ActionResultWithUrl(url));

            Assert.Equal(new[] { url }, opened);
            Assert.True(host.IsOpenInBrowserVisible);
            Assert.Equal(url, host.CurrentActionUrl);
            Assert.Equal(
                Visibility.Collapsed,
                Find<TextBlock>(host, "TextError").Visibility);
        });
    }

    [Fact]
    public void Host_ActionWithUrl_RefreshPathStillOpensAfterReload()
    {
        UiSmokeTests.RunOnUiThreadAsync(async () =>
        {
            // zip_web 的真实返回形态：refresh=true + data.url。旧的 refresh 分支
            // 直接 return，既没读 data.url，也不会打开浏览器（缺陷 1 真因）。
            var gateway = new FakePluginPageGateway();
            gateway.PendingLoads.Enqueue(ActionResult(
                state: "running",
                statusText: "正在浏览：http://127.0.0.1:51234/",
                actionCount: 1,
                itemCount: 1,
                port: 51234,
                url: "http://127.0.0.1:51234/"));
            var host = NewHost(gateway);
            host.ApplyLoad(ActionResult(
                state: "idle", statusText: "未启动", actionCount: 1, itemCount: 1, port: 0));

            var opened = new List<string>();
            host.BrowserOpener = url =>
            {
                opened.Add(url);
                return true;
            };

            await host.ApplyActionResultAsync(
                ActionResultWithUrl("http://127.0.0.1:51234/", refresh: true));

            Assert.Single(gateway.LoadCalls);
            Assert.Equal(new[] { "http://127.0.0.1:51234/" }, opened);
            Assert.True(host.IsOpenInBrowserVisible);
        });
    }

    [Fact]
    public void Host_SameUrl_RerenderAndRepeatActionDoNotReopenBrowser()
    {
        UiSmokeTests.RunOnUiThreadAsync(async () =>
        {
            var host = NewHost(new FakePluginPageGateway());
            host.ApplyLoad(ActionResult(
                state: "idle", statusText: "未启动", actionCount: 1, itemCount: 1, port: 0));

            int calls = 0;
            host.BrowserOpener = _ =>
            {
                calls++;
                return true;
            };

            const string url = "http://127.0.0.1:51234/";
            await host.ApplyActionResultAsync(ActionResultWithUrl(url));

            // 页面刷新 / 同一 url 重新渲染：只更新按钮，不再弹浏览器
            host.ApplyLoad(ActionResult(
                state: "running",
                statusText: "正在浏览",
                actionCount: 1,
                itemCount: 1,
                port: 51234,
                url: url));
            // 同一 url 的后续动作结果（非用户操作）：保持幂等
            await host.ApplyActionResultAsync(ActionResultWithUrl(url));

            Assert.Equal(1, calls);
        });
    }

    [Fact]
    public void Host_BrowserOpenFailure_ShowsUrlAndExceptionType()
    {
        UiSmokeTests.RunOnUiThreadAsync(async () =>
        {
            var host = NewHost(new FakePluginPageGateway());
            host.ApplyLoad(ActionResult(
                state: "idle", statusText: "未启动", actionCount: 1, itemCount: 1, port: 0));
            host.BrowserOpener = _ => throw new Win32Exception(5, "拒绝访问。");

            const string url = "http://127.0.0.1:51234/";
            await host.ApplyActionResultAsync(ActionResultWithUrl(url));

            var error = Find<TextBlock>(host, "TextError");
            Assert.Equal(Visibility.Visible, error.Visibility);
            Assert.Contains("[OPEN_FAILED]", error.Text);
            Assert.Contains(url, error.Text);
            Assert.Contains(nameof(Win32Exception), error.Text);
            // 失败不崩：页面仍然可用，并保留「在浏览器中打开」备用按钮
            Assert.True(host.IsOpenInBrowserVisible);
        });
    }

    [Fact]
    public void Host_BrowserOpenFailure_AfterRefreshReloadIsNotCleared()
    {
        UiSmokeTests.RunOnUiThreadAsync(async () =>
        {
            var gateway = new FakePluginPageGateway();
            gateway.PendingLoads.Enqueue(ActionResult(
                state: "running",
                statusText: "正在浏览",
                actionCount: 1,
                itemCount: 1,
                port: 51234,
                url: "http://127.0.0.1:51234/"));
            var host = NewHost(gateway);
            host.ApplyLoad(ActionResult(
                state: "idle", statusText: "未启动", actionCount: 1, itemCount: 1, port: 0));
            host.BrowserOpener = _ => throw new Win32Exception(5, "拒绝访问。");

            // zip_web 的真实形态 refresh=true：自动唤起必须在重载之后执行，
            // 否则重载里的 ClearError 会把打开失败文案吞掉。
            await host.ApplyActionResultAsync(
                ActionResultWithUrl("http://127.0.0.1:51234/", refresh: true));

            var error = Find<TextBlock>(host, "TextError");
            Assert.Equal(Visibility.Visible, error.Visibility);
            Assert.Contains("[OPEN_FAILED]", error.Text);
            Assert.Contains("Win32Exception", error.Text);
        });
    }

    [Fact]
    public void Host_BrowserOpenFailure_AllowsRetryWithSameUrl()
    {
        UiSmokeTests.RunOnUiThreadAsync(async () =>
        {
            var host = NewHost(new FakePluginPageGateway());
            host.ApplyLoad(ActionResult(
                state: "idle", statusText: "未启动", actionCount: 1, itemCount: 1, port: 0));

            int calls = 0;
            host.BrowserOpener = _ =>
            {
                calls++;
                if (calls == 1)
                {
                    throw new InvalidOperationException("没有默认浏览器");
                }

                return true;
            };

            const string url = "http://127.0.0.1:51234/";
            await host.ApplyActionResultAsync(ActionResultWithUrl(url));
            Assert.Equal(Visibility.Visible, Find<TextBlock>(host, "TextError").Visibility);
            Assert.Contains(nameof(InvalidOperationException), Find<TextBlock>(host, "TextError").Text);

            // 同一 url 重试：失败不进入幂等黑名单，第二次允许再次唤起
            await host.ApplyActionResultAsync(ActionResultWithUrl(url));
            Assert.Equal(2, calls);
            Assert.Equal(
                Visibility.Collapsed,
                Find<TextBlock>(host, "TextError").Visibility);
        });
    }

    [Fact]
    public void Host_ManualOpenButton_StillOpensAfterAutoOpen()
    {
        UiSmokeTests.RunOnUiThreadAsync(async () =>
        {
            var host = NewHost(new FakePluginPageGateway());
            host.ApplyLoad(ActionResult(
                state: "idle", statusText: "未启动", actionCount: 1, itemCount: 1, port: 0));

            var opened = new List<string>();
            host.BrowserOpener = url =>
            {
                opened.Add(url);
                return true;
            };

            const string url = "http://127.0.0.1:51234/";
            await host.ApplyActionResultAsync(ActionResultWithUrl(url));
            Find<Button>(host, "BtnActionOpenBrowser")
                .RaiseEvent(new RoutedEventArgs(Button.ClickEvent));

            // 自动打开一次 + 用户显式点击一次；备用按钮不被幂等逻辑吞掉
            Assert.Equal(new[] { url, url }, opened);
        });
    }

    // ------------------------------------------------------------------
    // 浏览器唤起：三级兜底（Process.Start → cmd start → rundll32）
    // ------------------------------------------------------------------

    [Fact]
    public void ProductionOpenAttempts_AreOrderedAsDocumented()
    {
        Assert.Equal(
            new[]
            {
                "Process.Start(UseShellExecute)",
                "cmd.exe /c start",
                "rundll32 url.dll,FileProtocolHandler",
            },
            PluginPageHost.ProductionOpenAttempts()
                .Select(attempt => attempt.Method)
                .ToArray());
    }

    [Fact]
    public void Host_ProductionConstructor_UsesThreeLevelFallbackChain()
    {
        UiSmokeTests.RunOnUiThread(() =>
        {
            // 生产入口不再预置单级 BrowserOpener：否则三级兜底永远走不到。
            var host = new PluginPageHost("demo", "demo.main", "演示页");
            Assert.Null(host.BrowserOpener);
            Assert.Equal(
                new[]
                {
                    "Process.Start(UseShellExecute)",
                    "cmd.exe /c start",
                    "rundll32 url.dll,FileProtocolHandler",
                },
                host.CurrentOpenAttempts.Select(attempt => attempt.Method).ToArray());

            // 旧测试注入的单级打开器仍然只占一级（失败即停，不触发真实 cmd/rundll32）。
            host.BrowserOpener = _ => false;
            Assert.Single(host.CurrentOpenAttempts);
        });
    }

    [Fact]
    public void Host_BrowserOpen_TriesThreeLevelFallbacksInOrderAndStopsOnSuccess()
    {
        UiSmokeTests.RunOnUiThreadAsync(async () =>
        {
            var host = NewHost(new FakePluginPageGateway());
            host.ApplyLoad(ActionResult(
                state: "idle", statusText: "未启动", actionCount: 1, itemCount: 1, port: 0));

            var calls = new List<string>();
            host.BrowserOpeners = new[]
            {
                new BrowserOpenAttempt("Process.Start(UseShellExecute)", _ =>
                {
                    calls.Add("Process.Start(UseShellExecute)");
                    return false;
                }),
                new BrowserOpenAttempt("cmd.exe /c start", _ =>
                {
                    calls.Add("cmd.exe /c start");
                    throw new Win32Exception(5, "拒绝访问。");
                }),
                new BrowserOpenAttempt("rundll32 url.dll,FileProtocolHandler", _ =>
                {
                    calls.Add("rundll32 url.dll,FileProtocolHandler");
                    return true;
                }),
            };

            const string url = "http://127.0.0.1:51234/";
            await host.ApplyActionResultAsync(ActionResultWithUrl(url));

            // 三级按顺序；第三级成功即停止（不会再有任何后续调用）。
            Assert.Equal(
                new[]
                {
                    "Process.Start(UseShellExecute)",
                    "cmd.exe /c start",
                    "rundll32 url.dll,FileProtocolHandler",
                },
                calls);
            Assert.Equal(3, host.LastBrowserOpenRecords.Count);
            Assert.False(host.LastBrowserOpenRecords[0].Succeeded);
            Assert.False(host.LastBrowserOpenRecords[1].Succeeded);
            Assert.True(host.LastBrowserOpenRecords[2].Succeeded);

            // 成功时状态区明确写“已用方法 X 打开”，并说明前面失败过几次。
            Assert.Equal(
                Visibility.Collapsed,
                Find<TextBlock>(host, "TextError").Visibility);
            string status = Find<TextBlock>(host, "TextStatus").Text;
            Assert.Contains(
                "已用方法 rundll32 url.dll,FileProtocolHandler",
                status,
                StringComparison.Ordinal);
            Assert.Contains("前 2 次尝试失败", status, StringComparison.Ordinal);
        });
    }

    [Fact]
    public void Host_BrowserOpen_AllThreeFail_ShowsOpenFailedAndKeepsCopyButton()
    {
        UiSmokeTests.RunOnUiThreadAsync(async () =>
        {
            var host = NewHost(new FakePluginPageGateway());
            host.ApplyLoad(ActionResult(
                state: "idle", statusText: "未启动", actionCount: 1, itemCount: 1, port: 0));
            host.BrowserOpeners = new[]
            {
                new BrowserOpenAttempt("Process.Start(UseShellExecute)", _ =>
                    throw new Win32Exception(5, "拒绝访问。")),
                new BrowserOpenAttempt("cmd.exe /c start", _ =>
                    throw new InvalidOperationException("cmd 启动失败")),
                new BrowserOpenAttempt("rundll32 url.dll,FileProtocolHandler", _ => false),
            };

            const string url = "http://127.0.0.1:51234/";
            await host.ApplyActionResultAsync(ActionResultWithUrl(url));

            var error = Find<TextBlock>(host, "TextError");
            Assert.Equal(Visibility.Visible, error.Visibility);
            Assert.Contains(
                "[OPEN_FAILED] 三种方式均无法打开浏览器：" + url,
                error.Text,
                StringComparison.Ordinal);
            // 逐次尝试明细：方法名、异常类型、HResult 都必须在状态区。
            Assert.Contains("Process.Start(UseShellExecute)", error.Text, StringComparison.Ordinal);
            Assert.Contains("Win32Exception", error.Text, StringComparison.Ordinal);
            Assert.Contains("HResult=0x", error.Text, StringComparison.Ordinal);
            Assert.Contains("cmd.exe /c start", error.Text, StringComparison.Ordinal);
            Assert.Contains("InvalidOperationException", error.Text, StringComparison.Ordinal);
            Assert.Contains(
                "rundll32 url.dll,FileProtocolHandler", error.Text, StringComparison.Ordinal);
            Assert.Contains("未转发", error.Text, StringComparison.Ordinal);

            Assert.Equal(3, host.LastBrowserOpenRecords.Count);
            Assert.All(host.LastBrowserOpenRecords, record => Assert.False(record.Succeeded));
            // 全部失败也不拿走备用入口：按钮与「复制链接」始终保留。
            Assert.True(host.IsOpenInBrowserVisible);
            Assert.True(host.IsCopyUrlVisible);
        });
    }

    [Fact]
    public void Host_ActionTemplate_CopyUrlButtonWritesClipboardAndHidesWithoutUrl()
    {
        UiSmokeTests.RunOnUiThread(() =>
        {
            var host = NewHost(new FakePluginPageGateway());
            const string url = "http://127.0.0.1:51234/";
            host.ApplyLoad(ActionResult(
                state: "running",
                statusText: "正在浏览",
                actionCount: 1,
                itemCount: 1,
                port: 51234,
                url: url));
            Assert.True(host.IsCopyUrlVisible);

            string? copied = null;
            host.ClipboardWriter = value => copied = value;
            Find<Button>(host, "BtnActionCopyUrl")
                .RaiseEvent(new RoutedEventArgs(Button.ClickEvent));

            Assert.Equal(url, copied);
            Assert.Contains(
                "链接已复制",
                Find<TextBlock>(host, "TextStatus").Text,
                StringComparison.Ordinal);

            // 没有 url 时按钮隐藏，点击也不写剪贴板。
            host.ApplyLoad(ActionResult(
                state: "idle", statusText: "未启动", actionCount: 1, itemCount: 1, port: 0));
            Assert.False(host.IsCopyUrlVisible);
            copied = null;
            Find<Button>(host, "BtnActionCopyUrl")
                .RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
            Assert.Null(copied);
        });
    }

    // ------------------------------------------------------------------
    // action 连点/双击：同一时刻只执行一个动作（zip_web 的 start 会换端口，
    // 第二次 start 会把刚显示、刚打开的地址停掉）
    // ------------------------------------------------------------------

    [Fact]
    public void Host_ActionDoubleClick_RunsOnlyOneActionAndDisablesButtons()
    {
        UiSmokeTests.RunOnUiThreadAsync(async () =>
        {
            var gateway = new FakePluginPageGateway
            {
                ActionGate = new TaskCompletionSource(),
                ActionResult = ActionResultWithUrl("http://127.0.0.1:51234/"),
            };
            var host = NewHost(gateway);
            host.BrowserOpener = _ => true;
            host.ApplyLoad(ActionResult(
                state: "running",
                statusText: "正在浏览：http://127.0.0.1:51234/",
                actionCount: 1,
                itemCount: 1,
                port: 51234,
                url: "http://127.0.0.1:51234/"));

            var actionButton =
                (Button)Find<StackPanel>(host, "PanelActionButtons").Children[0];

            // 真实用户的连点 / 双击
            actionButton.RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
            actionButton.RaiseEvent(new RoutedEventArgs(Button.ClickEvent));

            await WaitUntilAsync(() => gateway.ActionCalls.Count >= 1);
            Assert.Single(gateway.ActionCalls);
            Assert.False(actionButton.IsEnabled);

            gateway.ActionGate!.SetResult();
            await WaitUntilAsync(() => gateway.ActionCalls.Count == 1 &&
                ((Button)Find<StackPanel>(host, "PanelActionButtons").Children[0]).IsEnabled);

            // 动作结束后按钮恢复可用，且仍然只发过一次动作
            Assert.Single(gateway.ActionCalls);
        });
    }

    /// <summary>
    /// 自动唤起只打开**页面当前渲染**的地址：动作返回的旧地址若已被重载后的页面
    /// 顶替（换包/重复 start 会关掉旧端口），绝不打开那个死地址。
    /// </summary>
    [Fact]
    public void Host_ActionUrlSupersededByReload_OpensRenderedUrlOnly()
    {
        UiSmokeTests.RunOnUiThreadAsync(async () =>
        {
            var gateway = new FakePluginPageGateway();
            gateway.PendingLoads.Enqueue(ActionResult(
                state: "running",
                statusText: "正在浏览：http://127.0.0.1:60002/",
                actionCount: 1,
                itemCount: 1,
                port: 60002,
                url: "http://127.0.0.1:60002/"));
            var host = NewHost(gateway);
            host.ApplyLoad(ActionResult(
                state: "idle", statusText: "未启动", actionCount: 1, itemCount: 1, port: 0));

            var opened = new List<string>();
            host.BrowserOpener = url =>
            {
                opened.Add(url);
                return true;
            };

            // 动作返回的地址（60001）在重载后已不是页面显示的地址（60002）
            await host.ApplyActionResultAsync(
                ActionResultWithUrl("http://127.0.0.1:60001/", refresh: true));

            Assert.Equal(new[] { "http://127.0.0.1:60002/" }, opened);
            Assert.Equal("http://127.0.0.1:60002/", host.CurrentActionUrl);
        });
    }

    [Fact]
    public void Host_ActionFolderChoice_ShowsButtonAndReloadsWithFolder()
    {
        UiSmokeTests.RunOnUiThreadAsync(async () =>
        {
            var gateway = new FakePluginPageGateway();
            gateway.PendingLoads.Enqueue(ActionResult(
                state: "idle",
                statusText: "已选目录：D:\\zip-fixture（1 个 ZIP）",
                actionCount: 1,
                itemCount: 1,
                port: 0,
                folder: @"D:\zip-fixture",
                allowFolderChoice: true));
            var host = NewHost(gateway);
            host.FolderPicker = () => @"D:\zip-fixture";

            // 首个状态：allow_folder_choice=true → action 模板也显示「选择文件夹…」
            host.ApplyLoad(ActionResult(
                state: "idle",
                statusText: "未选择目录",
                actionCount: 1,
                itemCount: 0,
                port: 0,
                allowFolderChoice: true));
            Assert.True(host.IsActionFolderChoiceVisible);
            var button = Find<Button>(host, "BtnActionChooseFolder");
            Assert.Equal(Visibility.Visible, button.Visibility);
            Assert.Equal("选择文件夹…", button.Content as string);

            // 等价于用户点击：文件夹选择器已注入假实现
            button.RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
            await WaitUntilAsync(() => gateway.LoadCalls.Count >= 1);

            using (JsonDocument document = JsonDocument.Parse(gateway.LoadCalls[0]))
            {
                JsonElement root = document.RootElement;
                Assert.Equal(1, root.GetProperty("page").GetInt32());
                Assert.Equal(-1, root.GetProperty("item_index").GetInt32());
                Assert.Equal(@"D:\zip-fixture", root.GetProperty("folder").GetString());
            }
            Assert.Equal(@"D:\zip-fixture", host.CurrentFolder);

            // Python 不允许时按钮隐藏（可见性只跟载荷布尔值走）
            host.ApplyLoad(ActionResult(
                state: "idle",
                statusText: "未选择目录",
                actionCount: 1,
                itemCount: 0,
                port: 0,
                allowFolderChoice: false));
            Assert.False(host.IsActionFolderChoiceVisible);
            Assert.Equal(
                Visibility.Collapsed,
                Find<Button>(host, "BtnActionChooseFolder").Visibility);
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

    /// <summary>
    /// 触发 DataGrid 的 Measure/Arrange（模拟真机可视区），让行虚拟化真正创建
    /// 可视行容器（封面只对已创建容器取图）。
    /// </summary>
    private static void LayoutList(PluginPageHost host, double width = 900, double height = 600)
    {
        var grid = Find<DataGrid>(host, "GridRows");
        if (grid.Parent is not FrameworkElement panel)
        {
            throw new InvalidOperationException("GridRows 没有可布局的父容器。");
        }

        panel.Measure(new Size(width, height));
        panel.Arrange(new Rect(0, 0, width, height));
        panel.UpdateLayout();
        grid.UpdateLayout();
    }

    /// <summary>在视觉树里按条件找第一个后代（封面格模板断言用）。</summary>
    private static T? FindVisualChild<T>(DependencyObject root, Func<T, bool> predicate)
        where T : DependencyObject
    {
        int count = VisualTreeHelper.GetChildrenCount(root);
        for (int index = 0; index < count; index++)
        {
            DependencyObject child = VisualTreeHelper.GetChild(root, index);
            if (child is T match && predicate(match))
            {
                return match;
            }

            if (FindVisualChild(child, predicate) is { } nested)
            {
                return nested;
            }
        }

        return null;
    }

    /// <summary>
    /// 收藏夹形态的 list payload（阶段 4-4 扩展）：每行带 thumb_book_id，
    /// 页级带 row_double_click_action。
    /// </summary>
    private static PluginPageLoadResultDto CoverListResult(
        IEnumerable<string> thumbBookIds,
        string rowDoubleClickAction,
        int page = 1)
    {
        string[] rows = thumbBookIds
            .Select((bookId, index) =>
                $"{{\"book_id\":\"{bookId}\",\"thumb_book_id\":\"{bookId}\","
                + $"\"title\":\"标题{index + 1}\",\"author\":\"作者{index + 1}\","
                + "\"page_count\":10,\"click_count\":20,\"updated\":\"2026-10-01\"}")
            .ToArray();
        string actionJson = rowDoubleClickAction
            .Replace("\\", "\\\\")
            .Replace("\"", "\\\"");

        string json = $$"""
        {
          "ok": true,
          "template": "list",
          "title": "收藏夹",
          "data": {
            "columns": [
              {"key": "title", "title": "标题"},
              {"key": "author", "title": "作者", "width": 160}
            ],
            "rows": [{{string.Join(",", rows)}}],
            "page": {{page}},
            "page_size": 20,
            "total": {{rows.Length}},
            "total_pages": 1,
            "row_actions": [
              {"id": "detail", "title": "详情", "primary": true},
              {"id": "download", "title": "下载"}
            ],
            "row_double_click_action": "{{actionJson}}",
            "empty_text": "收藏夹是空的"
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
        string emptyText = "",
        bool allowFolderChoice = false,
        string folder = "")
    {
        var items = Enumerable.Range(0, itemCount)
            .Select(index => $"{{\"name\":\"{index + 1:000}.zip\",\"path\":\"C:/z/{index + 1:000}.zip\"}}");
        // 路径里的反斜杠与引号按 JSON 规则转义（测试只喂普通路径）
        string folderJson = folder.Replace("\\", "\\\\").Replace("\"", "\\\"");

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
            "empty_text": "{{emptyText}}",
            "folder": "{{folderJson}}",
            "allow_folder_choice": {{(allowFolderChoice ? "true" : "false")}}
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
        int port,
        string url = "",
        string folder = "",
        bool allowFolderChoice = false)
    {
        var items = Enumerable.Range(0, itemCount)
            .Select(index => $"{{\"name\":\"{index + 1:000}.zip\",\"path\":\"C:/z/{index + 1:000}.zip\"}}");
        var actions = Enumerable.Range(0, actionCount)
            .Select(index => $"{{\"id\":\"act{index}\",\"title\":\"动作{index}\"}}");
        // 路径 / 网址 / 文案里的反斜杠与引号按 JSON 规则转义（测试只喂普通路径）
        string statusJson = statusText.Replace("\\", "\\\\").Replace("\"", "\\\"");
        string urlJson = url.Replace("\\", "\\\\").Replace("\"", "\\\"");
        string folderJson = folder.Replace("\\", "\\\\").Replace("\"", "\\\"");

        string json = $$"""
        {
          "ok": true,
          "template": "action",
          "title": "ZIP 网页浏览",
          "data": {
            "items": [{{string.Join(",", items)}}],
            "state": "{{state}}",
            "status_text": "{{statusJson}}",
            "actions": [{{string.Join(",", actions)}}],
            "port": {{port}},
            "url": "{{urlJson}}",
            "folder": "{{folderJson}}",
            "allow_folder_choice": {{(allowFolderChoice ? "true" : "false")}}
          }
        }
        """;

        return Parse<PluginPageLoadResultDto>(json);
    }

    /// <summary>action 动作成功返回：与 zip_web 的 handle_start 同形（refresh + data.url）。</summary>
    private static PluginPageActionResultDto ActionResultWithUrl(string url, bool refresh = false)
    {
        string urlJson = url.Replace("\\", "\\\\").Replace("\"", "\\\"");
        string json = $$"""
        {
          "ok": true,
          "refresh": {{(refresh ? "true" : "false")}},
          "message": "浏览服务已启动",
          "data": {
            "items": [{"name": "001.zip", "path": "C:/z/001.zip"}],
            "state": "running",
            "status_text": "正在浏览：{{urlJson}}",
            "actions": [{"id": "stop", "title": "停止浏览服务"}],
            "port": 51234,
            "url": "{{urlJson}}",
            "folder": "",
            "allow_folder_choice": true
          }
        }
        """;

        return Parse<PluginPageActionResultDto>(json);
    }

    private static PluginPageLoadResultDto ListResultWithFilters(
        string folderValue,
        string sortValue)
    {
        string json = $$"""
        {
          "ok": true,
          "template": "list",
          "title": "收藏夹",
          "data": {
            "columns": [
              {"key": "title", "title": "标题"},
              {"key": "author", "title": "作者"}
            ],
            "rows": [{"title": "标题A", "author": "作者A", "book_id": "123"}],
            "page": 1,
            "page_size": 20,
            "total": 1,
            "total_pages": 1,
            "row_actions": [
              {"id": "detail", "title": "详情", "primary": true},
              {"id": "download", "title": "下载"}
            ],
            "filters": [
              {"key": "folder_id", "label": "收藏夹",
               "options": [
                 {"value": "0", "label": "全部"},
                 {"value": "7", "label": "子夹"}
               ],
               "value": "{{folderValue}}"},
              {"key": "sort", "label": "排序",
               "options": [
                 {"value": "mr", "label": "最近收藏"},
                 {"value": "mv", "label": "最多点击"},
                 {"value": "mp", "label": "最多图片"}
               ],
               "value": "{{sortValue}}"}
            ],
            "empty_text": "收藏夹是空的"
          }
        }
        """;

        return Parse<PluginPageLoadResultDto>(json);
    }

    /// <summary>假 intent 转发器：只记录收到的意图（离线、不打开任何窗口）。</summary>
    private sealed class FakeUiIntentHandler : IUiIntentHandler
    {
        public List<PluginPageIntentDto> Intents { get; } = [];

        public void HandleIntent(PluginPageIntentDto intent) => Intents.Add(intent);
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

        /// <summary>
        /// 可选：动作返回前先等这个信号（验证"动作未完成时不响应第二次点击"）。
        /// 为 null 时立即返回 <see cref="ActionResult"/>。
        /// </summary>
        public TaskCompletionSource? ActionGate { get; set; }

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
            PluginPageActionResultDto result =
                ActionResult ?? new PluginPageActionResultDto { Ok = true };
            if (ActionGate is { } gate)
            {
                return CompleteAfterAsync(gate.Task, result);
            }

            return Task.FromResult(result);
        }

        private static async Task<T> CompleteAfterAsync<T>(Task gate, T value)
        {
            await gate;
            return value;
        }

        public Task<byte[]?> ImageAsync(string pluginId, string pageId, int index)
        {
            ImageCalls.Add(index);
            return Task.FromResult(ImageResult);
        }
    }
}
