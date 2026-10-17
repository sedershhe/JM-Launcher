using JML.Bridge;
using JML.Bridge.Dto;
using Xunit;

namespace JML.Tests;

/// <summary>
/// 事件信封解析测试：6 类已知事件 + 未知类型 + 畸形 JSON。
/// 使用可注入的同步/可控 dispatcher，不启动 WPF Application。
/// </summary>
public class EventRelayTests
{
    /// <summary>把“排空请求”先收集起来，由测试决定何时执行（模拟 UI 线程）。</summary>
    private sealed class RecordingDispatcher
    {
        public List<Action> Scheduled { get; } = [];

        public void Dispatch(Action action)
        {
            lock (Scheduled)
            {
                Scheduled.Add(action);
            }
        }

        public void RunAll()
        {
            while (true)
            {
                Action? next;
                lock (Scheduled)
                {
                    if (Scheduled.Count == 0)
                    {
                        return;
                    }

                    next = Scheduled[0];
                    Scheduled.RemoveAt(0);
                }

                next();
            }
        }
    }

    private static EventRelay NewRelay(out RecordingDispatcher dispatcher, int capacity = 10000)
    {
        dispatcher = new RecordingDispatcher();
        return new EventRelay(dispatcher.Dispatch, capacity);
    }

    [Fact]
    public void DownloadItemStarted_IsParsedToStrongType()
    {
        using EventRelay relay = NewRelay(out RecordingDispatcher dispatcher);
        DownloadItemStartedPayload? received = null;
        relay.DownloadItemStartedReceived += payload => received = payload;

        relay.Ingest(
            """{"type":"download.item_started","payload":{"type":"download.item_started","book_id":"123","title":"标题","batch_id":"b1"}}""");
        Assert.Null(received);
        dispatcher.RunAll();

        Assert.NotNull(received);
        Assert.Equal("123", received!.BookId);
        Assert.Equal("标题", received.Title);
        Assert.Equal("b1", received.BatchId);
        Assert.Equal(0, relay.PendingCount);
    }

    [Fact]
    public void DownloadItemFinished_IsParsedToStrongType()
    {
        using EventRelay relay = NewRelay(out RecordingDispatcher dispatcher);
        DownloadItemFinishedPayload? received = null;
        relay.DownloadItemFinishedReceived += payload => received = payload;

        relay.Ingest(
            """{"type":"download.item_finished","payload":{"type":"download.item_finished","book_id":"123","title":"标题","ok":true,"zip_path":"C:/x.zip","error_code":"","error_message":"","batch_id":"b1"}}""");
        dispatcher.RunAll();

        Assert.NotNull(received);
        Assert.True(received!.Ok);
        Assert.Equal("C:/x.zip", received.ZipPath);
        Assert.Equal("b1", received.BatchId);
    }

    [Fact]
    public void DownloadBatchFinished_IsParsedToStrongType()
    {
        using EventRelay relay = NewRelay(out RecordingDispatcher dispatcher);
        DownloadBatchFinishedPayload? received = null;
        relay.DownloadBatchFinishedReceived += payload => received = payload;

        relay.Ingest(
            """{"type":"download.batch_finished","payload":{"type":"download.batch_finished","total":3,"success":2,"failed":1,"cancelled":0,"batch_id":"b1"}}""");
        dispatcher.RunAll();

        Assert.NotNull(received);
        Assert.Equal(3, received!.Total);
        Assert.Equal(2, received.Success);
        Assert.Equal(1, received.Failed);
        Assert.Equal("b1", received.BatchId);
    }

    [Fact]
    public void DownloadLog_IsParsedAndKeepsBatchIdForWindowing()
    {
        using EventRelay relay = NewRelay(out RecordingDispatcher dispatcher);
        DownloadLogEventArgs? received = null;
        relay.DownloadLogReceived += args => received = args;

        relay.Ingest(
            """{"type":"download.log","payload":{"type":"download.log","level":"info","message":"开始下载","batch_id":"b9"}}""");
        dispatcher.RunAll();

        Assert.NotNull(received);
        Assert.Equal("info", received!.Level);
        Assert.Equal("开始下载", received.Message);
        Assert.Equal("b9", received.BatchId);
        Assert.False(received.IsGlobal);

        // 非下载线程的日志 batch_id 为空串 → 全局日志窗
        relay.Ingest(
            """{"type":"download.log","payload":{"type":"download.log","level":"warning","message":"宿主初始化","batch_id":""}}""");
        dispatcher.RunAll();
        Assert.True(received!.IsGlobal);
    }

    [Fact]
    public void DownloadEvents_AreSanitizedAgainBeforeRaising()
    {
        // 桥接层叠一道脱敏：即使 Python 侧漏网，抛给 UI 前也不得出现凭据。
        using EventRelay relay = NewRelay(out RecordingDispatcher dispatcher);
        DownloadItemFinishedPayload? item = null;
        DownloadLogEventArgs? log = null;
        relay.DownloadItemFinishedReceived += payload => item = payload;
        relay.DownloadLogReceived += args => log = args;

        relay.Ingest(
            """{"type":"download.item_finished","payload":{"type":"download.item_finished","book_id":"1","title":"标题","ok":false,"zip_path":"","error_code":"IO_ERROR","error_message":"token=SECRET-TOKEN-1","batch_id":"b1"}}""");
        relay.Ingest(
            """{"type":"download.log","payload":{"type":"download.log","level":"error","message":"cookie=SESSION-1","batch_id":"b1"}}""");
        dispatcher.RunAll();

        Assert.NotNull(item);
        Assert.DoesNotContain("SECRET-TOKEN-1", item!.ErrorMessage);
        Assert.Contains("token=***", item.ErrorMessage);

        Assert.NotNull(log);
        Assert.DoesNotContain("SESSION-1", log!.Message);
        Assert.Contains("cookie=***", log.Message);
    }

    [Fact]
    public void SearchPageLoaded_IsParsedToStrongType()
    {
        using EventRelay relay = NewRelay(out RecordingDispatcher dispatcher);
        SearchPageLoadedPayload? received = null;
        relay.SearchPageLoadedReceived += payload => received = payload;

        relay.Ingest(
            """{"type":"search.page_loaded","payload":{"type":"search.page_loaded","keyword":"测试","page":2,"total_pages":5,"results":[{"book_id":"1","title":"t","author":"a","page_count":3,"tags":["x"],"cover_url":"u"}]}}""");
        dispatcher.RunAll();

        Assert.NotNull(received);
        Assert.Equal("测试", received!.Keyword);
        Assert.Equal(2, received.Page);
        Assert.Equal(5, received.TotalPages);
        SearchResultItemDto item = Assert.Single(received.Results);
        Assert.Equal("1", item.BookId);
        Assert.Equal(3, item.PageCount);
    }

    [Fact]
    public void PluginNotification_IsParsedToStrongType()
    {
        using EventRelay relay = NewRelay(out RecordingDispatcher dispatcher);
        PluginNotificationPayload? received = null;
        relay.PluginNotificationReceived += payload => received = payload;

        relay.Ingest(
            """{"type":"plugin.notification","payload":{"type":"plugin.notification","level":"info","title":"标题","message":"内容"}}""");
        dispatcher.RunAll();

        Assert.NotNull(received);
        Assert.Equal("info", received!.Level);
        Assert.Equal("标题", received.Title);
        Assert.Equal("内容", received.Message);
    }

    [Fact]
    public void UnknownEvent_IsCountedAndForwardedWithoutCrash()
    {
        using EventRelay relay = NewRelay(out RecordingDispatcher dispatcher);
        PythonEventEnvelope? unknown = null;
        int rawCount = 0;
        relay.EventReceived += _ => rawCount++;
        relay.UnknownEventReceived += envelope => unknown = envelope;

        relay.Ingest("""{"type":"future.event","payload":{"type":"future.event","x":1}}""");
        dispatcher.RunAll();

        Assert.Equal(1, relay.UnknownEventCount);
        Assert.Equal(0, relay.ParseFailureCount);
        Assert.Equal(1, rawCount);
        Assert.NotNull(unknown);
        Assert.Equal("future.event", unknown!.Type);
    }

    [Fact]
    public void MalformedJson_IsCountedAndDoesNotThrow()
    {
        using EventRelay relay = NewRelay(out RecordingDispatcher dispatcher);
        int rawEvents = 0;
        relay.EventReceived += _ => rawEvents++;
        relay.UnknownEventReceived += _ => rawEvents++;

        relay.Ingest("{ this is not json");
        relay.Ingest("""{"type":"download.log","payload":null}""");
        dispatcher.RunAll();

        Assert.Equal(2, relay.ParseFailureCount);
        Assert.Equal(0, relay.PendingCount);
        Assert.Equal(0, rawEvents);
    }

    [Fact]
    public void QueueIsBounded_DropsOverflowAndCounts()
    {
        using EventRelay relay = NewRelay(out RecordingDispatcher dispatcher, capacity: 2);

        const string eventJson =
            """{"type":"plugin.notification","payload":{"type":"plugin.notification","level":"info","title":"t","message":"m"}}""";
        relay.Ingest(eventJson);
        relay.Ingest(eventJson);
        relay.Ingest(eventJson);

        Assert.Equal(2, relay.PendingCount);
        Assert.Equal(1, relay.DroppedCount);
        Assert.Single(dispatcher.Scheduled);

        dispatcher.RunAll();
        Assert.Equal(0, relay.PendingCount);
    }
}
