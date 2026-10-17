using System.Diagnostics;
using System.IO;
using JML.Bridge;
using JML.Bridge.Dto;
using JML.Infrastructure;
using Python.Runtime;
using Xunit;

namespace JML.Tests;

/// <summary>
/// Python 真调用集成测试（全部离线，不触网）：
/// 验证 JSON 字符串转换、错误码透传、桥接方法不阻塞调用线程、封面 bytes 入口。
/// </summary>
[Collection(PythonBackendCollection.Name)]
public class BridgeIntegrationTests
{
    [Fact]
    public void Bootstrap_WithMissingDirectory_ReturnsFailureJson()
    {
        string missing = Path.Combine(
            Path.GetTempPath(),
            "jml-plugin-dir-missing-" + Guid.NewGuid().ToString("N"));

        BootstrapResultDto result = PluginBridge.Bootstrap(missing);

        // 证明 (string) 转换与 JSON 解析链路通：拿到合法 DTO 而不是 Python 异常。
        Assert.False(result.Ok);
        Assert.False(string.IsNullOrWhiteSpace(result.ErrorCode));
        Assert.False(string.IsNullOrWhiteSpace(result.ErrorMessage));
        Assert.Empty(result.Loaded);
    }

    [Fact]
    public async Task ParseBookIds_ParsesAllDocumentedSeparators()
    {
        // 契约 §3.7 的分隔符：- , ， 、 ; ； 与空白。
        // 实际切分结果：651712 / 565384 / 1 / 2 / 3 / 4 / 5 / 6 = 8 个（见报告契约缺口）。
        ParseIdsResultDto result =
            await DownloadBridge.ParseBookIds("651712,565384-1；2 3、4，5;6");

        Assert.True(result.Ok);
        Assert.Equal("", result.ErrorCode);
        Assert.Equal(8, result.Count);
        Assert.Equal(
            new[] { "651712", "565384", "1", "2", "3", "4", "5", "6" },
            result.BookIds);
    }

    [Fact]
    public async Task ParseBookIds_WithOnlyInvalidTokens_ReturnsEmptySuccess()
    {
        // 契约 §3.7 明确：parse_book_ids 丢弃非法 token，空结果仍算成功；
        // 整批拒绝只发生在 start_download（严格校验）。
        ParseIdsResultDto result = await DownloadBridge.ParseBookIds("abc");

        Assert.True(result.Ok);
        Assert.Equal("", result.ErrorCode);
        Assert.Equal(0, result.Count);
        Assert.Empty(result.BookIds);
    }

    [Fact]
    public async Task StartDownload_WithInvalidToken_ReturnsInvalidArgumentAndDoesNotBlock()
    {
        PythonRuntime.Initialize();
        string tempDir = Path.Combine(Path.GetTempPath(), "jml-bridge-tests");

        var stopwatch = Stopwatch.StartNew();
        Task<StartDownloadResultDto> task = DownloadBridge.StartDownload(
            new[] { "abc" },
            tempDir,
            tempDir);

        StartDownloadResultDto result = await task.WaitAsync(TimeSpan.FromSeconds(1));
        stopwatch.Stop();

        Assert.False(result.Ok);
        Assert.Equal("INVALID_ARGUMENT", result.ErrorCode);
        Assert.True(
            stopwatch.Elapsed < TimeSpan.FromSeconds(1),
            $"桥接方法必须立即返回，实际耗时 {stopwatch.Elapsed}。");
    }

    [Fact]
    public async Task Search_WithEmptyKeyword_ReturnsInvalidArgumentWithoutSecrets()
    {
        // 用独立 session_key，避免翻页快照互相影响。
        string sessionKey = "test-empty-keyword-" + Guid.NewGuid().ToString("N");

        SearchResultPageDto result = await SearchBridge.Search("", 1, sessionKey);

        Assert.False(result.Ok);
        Assert.Equal("INVALID_ARGUMENT", result.ErrorCode);
        Assert.False(string.IsNullOrWhiteSpace(result.ErrorMessage));

        string message = result.ErrorMessage;
        Assert.DoesNotContain("password", message, StringComparison.OrdinalIgnoreCase);
        Assert.DoesNotContain("token", message, StringComparison.OrdinalIgnoreCase);
        Assert.DoesNotContain("cookie", message, StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public async Task GetDetail_WithEmptyId_ReturnsInvalidArgument()
    {
        BookDetailDto result = await DetailBridge.GetDetail("");

        Assert.False(result.Ok);
        Assert.Equal("INVALID_ARGUMENT", result.ErrorCode);
    }

    [Fact]
    public async Task GetProgress_WithUnknownBatch_ReturnsBatchNotFoundAndKeepsId()
    {
        string batchId = "no-such-batch-" + Guid.NewGuid().ToString("N");

        BatchStatusDto result = await DownloadBridge.GetProgress(batchId);

        Assert.False(result.Ok);
        Assert.Equal("BATCH_NOT_FOUND", result.ErrorCode);

        // 关键断言：传入的 id 必须真的到达 Python。
        // 参数若再次在桥接层丢失，Python 收到空串会返回 INVALID_ARGUMENT，
        // 或 error_message 不含该 id，这条会失败。
        Assert.Contains(batchId, result.ErrorMessage, StringComparison.Ordinal);
        Assert.Null(result.Result);
    }

    [Fact]
    public async Task GetProgress_WithEmptyId_ReturnsInvalidArgument()
    {
        BatchStatusDto result = await DownloadBridge.GetProgress("");

        Assert.False(result.Ok);
        Assert.Equal("INVALID_ARGUMENT", result.ErrorCode);
        Assert.Null(result.Result);
    }

    [Fact]
    public async Task GetProgress_WithRegisteredBatch_ReturnsSameBatchId()
    {
        // 先注入 Python 侧假数据源（空章节 → 单本必然失败，但不触网），
        // 再经公开 StartDownload / GetProgress 走完整链路，证明真实批次 id 的查询可用。
        PythonRuntime.Initialize();

        // 不用系统 Temp：受限测试宿主对 Temp 无写权限；输出目录由测试项目自己拥有。
        string targetDir = Path.Combine(
            AppContext.BaseDirectory,
            "bridge-progress-" + Guid.NewGuid().ToString("N"));

        PythonRuntime.Invoke(() =>
        {
            // 先走统一入口把 Python/ 加入 sys.path（与生产路径一致）。
            PythonGateway.EnsureBackend();
            dynamic jmSource = Py.Import("jml.core.jm_source");

            // 用 type() 就地构造假数据源，不 import 测试夹具、不产生网络请求。
            dynamic fake = PythonEngine.Eval(
                "type('_BridgeFakeSource', (), {"
                + "'get_album': lambda self, book_id: {'title': '桥接测试', 'chapters': []},"
                + "'iter_chapter_images': lambda self, book_id, chapter, dest_dir: iter(())"
                + "})()");
            jmSource.set_source(fake);
        });

        string batchId = "";
        try
        {
            StartDownloadResultDto started = await DownloadBridge.StartDownload(
                new[] { "1" },
                targetDir,
                targetDir);
            Assert.True(started.Ok, started.ErrorMessage);
            batchId = started.BatchId;
            Assert.False(string.IsNullOrWhiteSpace(batchId));

            BatchStatusDto result = await DownloadBridge.GetProgress(batchId);

            Assert.True(result.Ok);
            Assert.Equal(batchId, result.BatchId);
            Assert.Contains(result.Status, new[] { "running", "finished" });
            Assert.Equal(1, result.Total);

            // 契约 §四：未结束时 result 为 null；已结束则是对象。
            if (result.Status == "finished")
            {
                Assert.NotNull(result.Result);
            }
        }
        finally
        {
            // 等后台线程收口并还原数据源，避免污染同集合的其它用例。
            string cleanupBatchId = batchId;
            PythonRuntime.Invoke(() =>
            {
                dynamic downloader = Py.Import("jml.core.downloader");
                dynamic jmSource = Py.Import("jml.core.jm_source");
                if (cleanupBatchId.Length > 0)
                {
                    downloader.wait_batch(cleanupBatchId, 10.0);
                }

                jmSource.reset_source();
            });
        }
    }

    [Fact]
    public async Task FetchCover_WithEmptyId_ReturnsNull()
    {
        byte[]? bytes = await SearchBridge.FetchCover("");

        Assert.Null(bytes);
    }

    [Fact]
    public void PythonGateway_CallRaw_ConvertsPythonBytes()
    {
        // 不联网地验证唯一非 JSON 入口的 bytes 转换（fetch_cover 的成功路径同型）。
        byte[]? bytes = PythonGateway.CallRaw<byte[]>(
            _ => PythonEngine.Eval("bytes([1, 2, 3, 4])"));

        Assert.Equal(new byte[] { 1, 2, 3, 4 }, bytes);
    }
}
