using System.Text.Json;
using JML.Bridge;
using JML.Bridge.Dto;
using Xunit;

namespace JML.Tests;

/// <summary>
/// 契约反序列化测试：用真实 JSON 字符串（成功/失败两种）逐 DTO 断言字段，
/// 证明 C# 能稳定读到 Python 的 snake_case 字段与 error_code。
/// </summary>
public class BridgeDtoContractTests
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

    [Fact]
    public void ParseIdsResult_ReadsSuccessAndFailure()
    {
        ParseIdsResultDto ok = Parse<ParseIdsResultDto>(
            """{"ok": true, "book_ids": ["651712", "007"], "count": 2}""");

        Assert.True(ok.Ok);
        Assert.Equal(2, ok.Count);
        Assert.Equal(new[] { "651712", "007" }, ok.BookIds);
        Assert.Equal("", ok.ErrorCode);
        Assert.Equal("", ok.ErrorMessage);

        ParseIdsResultDto fail = Parse<ParseIdsResultDto>(
            """{"ok": false, "error_code": "INVALID_ARGUMENT", "error_message": "编号列表为空"}""");

        Assert.False(fail.Ok);
        Assert.Equal("INVALID_ARGUMENT", fail.ErrorCode);
        Assert.Equal("编号列表为空", fail.ErrorMessage);
        Assert.Empty(fail.BookIds);
    }

    [Fact]
    public void StartDownloadResult_ReadsSuccessAndFailure()
    {
        StartDownloadResultDto ok = Parse<StartDownloadResultDto>(
            """{"ok": true, "batch_id": "b1", "total": 3, "status": "running"}""");

        Assert.True(ok.Ok);
        Assert.Equal("b1", ok.BatchId);
        Assert.Equal(3, ok.Total);
        Assert.Equal("running", ok.Status);

        StartDownloadResultDto fail = Parse<StartDownloadResultDto>(
            """{"ok": false, "error_code": "INVALID_ARGUMENT", "error_message": "编号必须是纯数字: 'abc'"}""");

        Assert.False(fail.Ok);
        Assert.Equal("INVALID_ARGUMENT", fail.ErrorCode);
        Assert.Contains("纯数字", fail.ErrorMessage);
    }

    [Fact]
    public void CancelDownloadResult_ReadsSuccessAndFailure()
    {
        CancelDownloadResultDto ok = Parse<CancelDownloadResultDto>(
            """{"ok": true, "batch_id": "b1", "cancelled": true}""");

        Assert.True(ok.Ok);
        Assert.Equal("b1", ok.BatchId);
        Assert.True(ok.Cancelled);

        CancelDownloadResultDto fail = Parse<CancelDownloadResultDto>(
            """{"ok": false, "error_code": "BATCH_NOT_FOUND", "error_message": "找不到下载批次: x"}""");

        Assert.False(fail.Ok);
        Assert.Equal("BATCH_NOT_FOUND", fail.ErrorCode);
    }

    [Fact]
    public void BatchStatus_ReadsFinishedResultAndNestedItems()
    {
        const string json = """
        {
          "ok": true,
          "batch_id": "b1",
          "status": "finished",
          "total": 2,
          "cancel_requested": false,
          "result": {
            "ok": true,
            "batch_id": "b1",
            "total": 2,
            "success": 1,
            "failed": 0,
            "cancelled": 1,
            "items": [
              {
                "book_id": "1",
                "title": "标题",
                "ok": true,
                "zip_path": "C:/downloads/标题-JM1.zip",
                "error_code": "",
                "error_message": "",
                "cancelled": false
              },
              {
                "book_id": "2",
                "title": "",
                "ok": false,
                "zip_path": "",
                "error_code": "CANCELLED",
                "error_message": "已取消",
                "cancelled": true
              }
            ]
          }
        }
        """;

        BatchStatusDto status = Parse<BatchStatusDto>(json);

        Assert.True(status.Ok);
        Assert.Equal("finished", status.Status);
        Assert.False(status.CancelRequested);
        Assert.NotNull(status.Result);
        Assert.Equal(1, status.Result!.Success);
        Assert.Equal(1, status.Result.Cancelled);
        Assert.Equal(2, status.Result.Items.Count);
        Assert.True(status.Result.Items[0].Ok);
        Assert.Equal("C:/downloads/标题-JM1.zip", status.Result.Items[0].ZipPath);
        Assert.False(status.Result.Items[1].Ok);
        Assert.True(status.Result.Items[1].Cancelled);
        Assert.Equal("CANCELLED", status.Result.Items[1].ErrorCode);
        Assert.Equal("已取消", status.Result.Items[1].ErrorMessage);
    }

    [Fact]
    public void BatchStatus_ReadsRunningAndNotFoundFailure()
    {
        BatchStatusDto running = Parse<BatchStatusDto>(
            """{"ok": true, "batch_id": "b1", "status": "running", "total": 2, "cancel_requested": false, "result": null}""");

        Assert.True(running.Ok);
        Assert.Equal("running", running.Status);
        Assert.Null(running.Result);

        BatchStatusDto fail = Parse<BatchStatusDto>(
            """{"ok": false, "error_code": "BATCH_NOT_FOUND", "error_message": "找不到下载批次: b9"}""");

        Assert.False(fail.Ok);
        Assert.Equal("BATCH_NOT_FOUND", fail.ErrorCode);
        Assert.Null(fail.Result);
    }

    [Fact]
    public void SearchResultPage_ReadsSuccessAndFailure()
    {
        const string json = """
        {
          "ok": true,
          "keyword": "测试",
          "page": 2,
          "total_pages": 5,
          "results": [
            {
              "book_id": "123",
              "title": "标题",
              "author": "作者",
              "page_count": 42,
              "tags": ["标签A", "标签B"],
              "cover_url": "https://example.invalid/cover.jpg"
            }
          ]
        }
        """;

        SearchResultPageDto page = Parse<SearchResultPageDto>(json);

        Assert.True(page.Ok);
        Assert.Equal("测试", page.Keyword);
        Assert.Equal(2, page.Page);
        Assert.Equal(5, page.TotalPages);
        SearchResultItemDto item = Assert.Single(page.Results);
        Assert.Equal("123", item.BookId);
        Assert.Equal(42, item.PageCount);
        Assert.Equal(new[] { "标签A", "标签B" }, item.Tags);

        SearchResultPageDto fail = Parse<SearchResultPageDto>(
            """{"ok": false, "error_code": "INVALID_ARGUMENT", "error_message": "参数不合法: 搜索关键词不能为空"}""");

        Assert.False(fail.Ok);
        Assert.Equal("INVALID_ARGUMENT", fail.ErrorCode);
        Assert.Empty(fail.Results);
    }

    [Fact]
    public void BookDetail_ReadsSuccessAndFailure()
    {
        const string json = """
        {
          "ok": true,
          "book_id": "123",
          "title": "标题",
          "author": "作者",
          "page_count": 100,
          "tags": ["标签"],
          "description": "简介…",
          "description_truncated": true,
          "cover_url": "https://example.invalid/cover.jpg"
        }
        """;

        BookDetailDto detail = Parse<BookDetailDto>(json);

        Assert.True(detail.Ok);
        Assert.Equal("123", detail.BookId);
        Assert.Equal(100, detail.PageCount);
        Assert.True(detail.DescriptionTruncated);
        Assert.Equal("简介…", detail.Description);

        BookDetailDto fail = Parse<BookDetailDto>(
            """{"ok": false, "error_code": "INVALID_ARGUMENT", "error_message": "参数不合法: 本子编号不能为空"}""");

        Assert.False(fail.Ok);
        Assert.Equal("INVALID_ARGUMENT", fail.ErrorCode);
    }

    [Fact]
    public void Diagnostics_ReadsRecords()
    {
        DiagnosticsDto diagnostics = Parse<DiagnosticsDto>(
            """{"ok": true, "records": ["[EVENT_HANDLER_FAILED] x", "[COVER_FETCH_FAILED] y"], "count": 2}""");

        Assert.True(diagnostics.Ok);
        Assert.Equal(2, diagnostics.Count);
        Assert.Equal(2, diagnostics.Records.Count);
    }

    [Fact]
    public void TransportFailure_IsSanitized()
    {
        // 桥接层自身兜底失败（不是 Python 业务失败）也必须先脱敏再展示。
        ParseIdsResultDto fail = BridgeFailures.Create<ParseIdsResultDto>(
            new InvalidOperationException(
                "connect failed password=secret123 token=tok789 cookie=session=1"));

        Assert.False(fail.Ok);
        Assert.Equal("UNKNOWN_ERROR", fail.ErrorCode);
        Assert.DoesNotContain("secret123", fail.ErrorMessage);
        Assert.DoesNotContain("tok789", fail.ErrorMessage);
        Assert.DoesNotContain("session=1", fail.ErrorMessage);
    }
}
