using System.Collections.Concurrent;
using System.Text.Json;
using JML.Bridge.Dto;
using JML.Infrastructure;

namespace JML.Bridge;

/// <summary>
/// Python 事件回调 → UI 线程的事件中继（技术方案第十节“回调队列化”）。
///
/// 线程纪律（硬性）：
/// 1. Python sink 委托（<see cref="Action{T}"/>，单参数 eventJson）内部**只入队**：
///    不调 Python、不解析 JSON、不同步等待 UI；
/// 2. 入队后通过注入的 <c>uiDispatcher</c> 请求排空；生产环境传
///    <c>Dispatcher.BeginInvoke</c>，xUnit 传同步/可控实现，绝不启动 WPF Application；
/// 3. UI 线程才反序列化信封、按 type 转强类型 payload 并抛事件；
/// 4. 解析失败只记脱敏日志 + 计数，不向 UI 抛异常。
///
/// 下载日志按 <see cref="DownloadLogEventArgs.BatchId"/> 分窗（旧缺陷 #6），
/// 主下载页/详情窗各自过滤自己的批次，空 batch_id 归全局日志窗。
/// </summary>
public sealed class EventRelay : IDisposable
{
    /// <summary>队列容量上限：超限丢弃并计数，防止后台线程无限堆积。</summary>
    public const int DefaultQueueCapacity = 10000;

    private static readonly JsonSerializerOptions EventJsonOptions = new()
    {
        PropertyNameCaseInsensitive = true,
    };

    private readonly ConcurrentQueue<string> _queue = new();
    private readonly Action<Action> _uiDispatcher;
    private readonly int _queueCapacity;

    private Action<string>? _sinkDelegate;
    private int _started;
    private int _pendingCount;
    private int _droppedCount;
    private int _parseFailureCount;
    private int _unknownEventCount;
    private int _handlerFailureCount;
    private int _dispatchFailureCount;
    private int _drainScheduled;
    private int _disposed;

    /// <param name="uiDispatcher">
    /// 把一段排空逻辑编组到 UI 线程。生产传 <c>action =&gt; Dispatcher.BeginInvoke(action)</c>；
    /// 测试传同步实现或先收集再手动执行的实现。
    /// </param>
    /// <param name="queueCapacity">有界队列容量（默认 10000）。</param>
    public EventRelay(Action<Action> uiDispatcher, int queueCapacity = DefaultQueueCapacity)
    {
        _uiDispatcher = uiDispatcher ?? throw new ArgumentNullException(nameof(uiDispatcher));
        if (queueCapacity <= 0)
        {
            throw new ArgumentOutOfRangeException(nameof(queueCapacity), "队列容量必须大于 0。");
        }

        _queueCapacity = queueCapacity;
    }

    /// <summary>队列中尚未被 UI 线程取走的事件数（供测试/诊断）。</summary>
    public int PendingCount => Volatile.Read(ref _pendingCount);

    /// <summary>因超过容量被丢弃的事件数。</summary>
    public int DroppedCount => Volatile.Read(ref _droppedCount);

    /// <summary>JSON/信封/payload 解析失败次数。</summary>
    public int ParseFailureCount => Volatile.Read(ref _parseFailureCount);

    /// <summary>未知事件类型次数（未知类型不丢弃，照样抛给订阅方）。</summary>
    public int UnknownEventCount => Volatile.Read(ref _unknownEventCount);

    /// <summary>订阅方处理器抛异常次数（被捕获并记日志）。</summary>
    public int HandlerFailureCount => Volatile.Read(ref _handlerFailureCount);

    /// <summary>把排空请求投递到 UI 线程失败的次数（例如窗口关闭中）。</summary>
    public int DispatchFailureCount => Volatile.Read(ref _dispatchFailureCount);

    /// <summary>队列容量。</summary>
    public int QueueCapacity => _queueCapacity;

    /// <summary>是否已注册 Python 事件出口。</summary>
    public bool IsStarted => Volatile.Read(ref _started) == 1;

    /// <summary>所有事件的信封（含未知类型）。</summary>
    public event Action<PythonEventEnvelope>? EventReceived;

    /// <summary>未知事件类型（转发原始信封，便于新事件灰度接入）。</summary>
    public event Action<PythonEventEnvelope>? UnknownEventReceived;

    /// <summary>单本开始下载。</summary>
    public event Action<DownloadItemStartedPayload>? DownloadItemStartedReceived;

    /// <summary>单本下载结束（成功/失败/取消）。</summary>
    public event Action<DownloadItemFinishedPayload>? DownloadItemFinishedReceived;

    /// <summary>批次全部结束。</summary>
    public event Action<DownloadBatchFinishedPayload>? DownloadBatchFinishedReceived;

    /// <summary>下载日志；调用方按 <see cref="DownloadLogEventArgs.BatchId"/> 分窗。</summary>
    public event Action<DownloadLogEventArgs>? DownloadLogReceived;

    /// <summary>搜索成功页加载。</summary>
    public event Action<SearchPageLoadedPayload>? SearchPageLoadedReceived;

    /// <summary>插件通知（阶段 2 未使用，协议保留）。</summary>
    public event Action<PluginNotificationPayload>? PluginNotificationReceived;

    /// <summary>
    /// 注册 Python 事件出口（<c>set_event_sink</c>）。可重复调用，已启动时直接返回。
    /// 失败会抛异常，由启动装配统一收口为可读提示；不在这里吞掉。
    /// </summary>
    public void Start()
    {
        ObjectDisposedException.ThrowIf(Volatile.Read(ref _disposed) == 1, this);

        if (Interlocked.Exchange(ref _started, 1) == 1)
        {
            return;
        }

        // 强引用：pythonnet 委托若无可达引用会被 GC，Python 再回调就会失效。
        _sinkDelegate ??= OnPythonEvent;

        try
        {
            BridgeResultDto result = PythonGateway.CallJson<BridgeResultDto>(
                backend => (string)backend.set_event_sink(_sinkDelegate));

            if (!result.Ok)
            {
                throw new InvalidOperationException(
                    $"注册 Python 事件出口失败：{result.ErrorCode} {result.ErrorMessage}");
            }
        }
        catch
        {
            Interlocked.Exchange(ref _started, 0);

            // 若 Python 侧已登记 sink 但本地后续步骤失败，重新置空，避免残留回调。
            try
            {
                PythonGateway.CallJson<BridgeResultDto>(
                    backend => (string)backend.clear_event_sink());
            }
            catch
            {
                // 清理失败不覆盖原始异常
            }

            throw;
        }
    }

    /// <summary>
    /// 取消订阅（<c>clear_event_sink</c>）并清空待处理队列。关闭窗口时可安全重复调用。
    /// </summary>
    public void Stop()
    {
        if (Interlocked.Exchange(ref _started, 0) == 0)
        {
            return;
        }

        try
        {
            PythonGateway.CallJson<BridgeResultDto>(
                backend => (string)backend.clear_event_sink());
        }
        catch (Exception ex)
        {
            // 关闭阶段 Python 可能已不可用；不让退出流程崩溃。
            LogSanitizer.Log($"移除事件出口失败：{ex.GetType().Name}: {ex.Message}");
        }
        finally
        {
            _sinkDelegate = null;
            ClearQueue();
        }
    }

    /// <inheritdoc />
    public void Dispose()
    {
        if (Interlocked.Exchange(ref _disposed, 1) == 1)
        {
            return;
        }

        Stop();
    }

    /// <summary>
    /// Python 工作线程上的 sink 回调。这里**只做一件事**：入队 + 请求 UI 排空。
    /// 严禁在此调用 Python（GIL 重入/死锁）或同步等待 UI。
    /// </summary>
    private void OnPythonEvent(string eventJson)
    {
        if (Volatile.Read(ref _started) == 0)
        {
            return;
        }

        Enqueue(eventJson);
    }

    /// <summary>
    /// 测试注入入口：绕过 Python 直接投递一条事件 JSON。
    /// 生产路径只由 <see cref="OnPythonEvent"/> 调用，行为与真实 sink 完全一致。
    /// </summary>
    internal void Ingest(string eventJson)
    {
        Enqueue(eventJson);
    }

    /// <summary>入队并请求排空（容量超限时丢弃并计数，不阻塞调用线程）。</summary>
    private void Enqueue(string eventJson)
    {
        if (string.IsNullOrWhiteSpace(eventJson))
        {
            Interlocked.Increment(ref _parseFailureCount);
            return;
        }

        int pending = Interlocked.Increment(ref _pendingCount);
        if (pending > _queueCapacity)
        {
            Interlocked.Decrement(ref _pendingCount);
            Interlocked.Increment(ref _droppedCount);
            return;
        }

        _queue.Enqueue(eventJson);
        RequestDrain();
    }

    /// <summary>请求 UI 线程排空；同一时刻只排一次，避免每个事件都建一个 Dispatcher 操作。</summary>
    private void RequestDrain()
    {
        if (Interlocked.Exchange(ref _drainScheduled, 1) == 1)
        {
            return;
        }

        try
        {
            _uiDispatcher(DrainOnUiThread);
        }
        catch (Exception ex)
        {
            // 窗口关闭/Dispatch 已停：恢复标志，后续事件仍可重试。
            Interlocked.Exchange(ref _drainScheduled, 0);
            Interlocked.Increment(ref _dispatchFailureCount);
            LogSanitizer.Log($"事件排空调度失败：{ex.GetType().Name}: {ex.Message}");
        }
    }

    /// <summary>UI 线程执行：先释放“已调度”标志，再一次性取空队列。</summary>
    private void DrainOnUiThread()
    {
        Interlocked.Exchange(ref _drainScheduled, 0);
        while (_queue.TryDequeue(out string? eventJson))
        {
            Interlocked.Decrement(ref _pendingCount);

            try
            {
                Handle(eventJson);
            }
            catch (Exception ex)
            {
                Interlocked.Increment(ref _parseFailureCount);
                LogSanitizer.Log($"事件解析失败：{ex.GetType().Name}: {ex.Message}");
            }
        }
    }

    /// <summary>反序列化信封 → 按 type 转强类型 payload → 抛对应事件。</summary>
    private void Handle(string eventJson)
    {
        PythonEventEnvelope? envelope =
            JsonSerializer.Deserialize<PythonEventEnvelope>(eventJson, EventJsonOptions);

        if (envelope is null || string.IsNullOrWhiteSpace(envelope.Type))
        {
            throw new JsonException("事件信封缺少 type 字段。");
        }

        // 先完成 payload 解析，成功后才对外抛信封/强类型事件；
        // 这样畸形 payload 只会增加解析失败计数，不会把半成品事件送到 UI。
        Action? typedEvent;
        switch (envelope.Type)
        {
            case PythonEventTypes.DownloadItemStarted:
            {
                DownloadItemStartedPayload payload =
                    ReadPayload<DownloadItemStartedPayload>(envelope);
                typedEvent = () => DownloadItemStartedReceived?.Invoke(payload);
                break;
            }

            case PythonEventTypes.DownloadItemFinished:
            {
                DownloadItemFinishedPayload payload =
                    ReadPayload<DownloadItemFinishedPayload>(envelope);

                // 桥接层再叠一道脱敏（Python 侧已脱敏），不改语义。
                if (!string.IsNullOrEmpty(payload.ErrorMessage))
                {
                    payload = payload with
                    {
                        ErrorMessage = LogSanitizer.Sanitize(payload.ErrorMessage),
                    };
                }

                typedEvent = () => DownloadItemFinishedReceived?.Invoke(payload);
                break;
            }

            case PythonEventTypes.DownloadBatchFinished:
            {
                // 该 payload 只有计数与 batch_id，没有自由文本，无需二次脱敏。
                DownloadBatchFinishedPayload payload =
                    ReadPayload<DownloadBatchFinishedPayload>(envelope);
                typedEvent = () => DownloadBatchFinishedReceived?.Invoke(payload);
                break;
            }

            case PythonEventTypes.DownloadLog:
            {
                DownloadLogPayload payload = ReadPayload<DownloadLogPayload>(envelope);

                // 桥接层再叠一道脱敏（Python 侧已脱敏），不改语义。
                DownloadLogEventArgs args = new(
                    payload.Level,
                    LogSanitizer.Sanitize(payload.Message),
                    payload.BatchId);
                typedEvent = () => DownloadLogReceived?.Invoke(args);
                break;
            }

            case PythonEventTypes.SearchPageLoaded:
            {
                SearchPageLoadedPayload payload =
                    ReadPayload<SearchPageLoadedPayload>(envelope);
                typedEvent = () => SearchPageLoadedReceived?.Invoke(payload);
                break;
            }

            case PythonEventTypes.PluginNotification:
            {
                PluginNotificationPayload payload =
                    ReadPayload<PluginNotificationPayload>(envelope);
                typedEvent = () => PluginNotificationReceived?.Invoke(payload);
                break;
            }

            default:
                Interlocked.Increment(ref _unknownEventCount);
                typedEvent = () => UnknownEventReceived?.Invoke(envelope);
                break;
        }

        SafeInvoke(() => EventReceived?.Invoke(envelope));
        SafeInvoke(typedEvent);
    }

    /// <summary>读取强类型 payload；缺失/null 视为解析失败，由上层计数并记日志。</summary>
    private static T ReadPayload<T>(PythonEventEnvelope envelope) where T : class
    {
        if (envelope.Payload.ValueKind is JsonValueKind.Undefined or JsonValueKind.Null)
        {
            throw new JsonException($"事件 {envelope.Type} 缺少 payload。");
        }

        T? payload = envelope.Payload.Deserialize<T>(EventJsonOptions);
        return payload ?? throw new JsonException($"事件 {envelope.Type} 的 payload 无法解析。");
    }

    /// <summary>订阅方异常不得打穿 UI：捕获、计数、脱敏记日志。</summary>
    private void SafeInvoke(Action action)
    {
        try
        {
            action();
        }
        catch (Exception ex)
        {
            Interlocked.Increment(ref _handlerFailureCount);
            LogSanitizer.Log($"事件订阅方处理失败：{ex.GetType().Name}: {ex.Message}");
        }
    }

    /// <summary>清空未处理队列（Stop/Dispose 用）。</summary>
    private void ClearQueue()
    {
        while (_queue.TryDequeue(out _))
        {
            Interlocked.Decrement(ref _pendingCount);
        }
    }
}
