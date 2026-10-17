using System.Text.Json;
using System.Text.Json.Serialization;

namespace JML.Bridge.Dto;

// 契约 §五：事件信封与各事件 payload。
// 信封固定 {"type": "...", "payload": {...}}；payload 自身也带 type（DTO 忽略该额外字段）。
// batch_id 只对下载类事件存在，C# 按它把日志分到不同窗口（旧缺陷 #6）。

/// <summary>事件类型常量（与 <c>jml.core.protocol</c> 保持一致）。</summary>
public static class PythonEventTypes
{
    public const string DownloadItemStarted = "download.item_started";
    public const string DownloadItemFinished = "download.item_finished";
    public const string DownloadBatchFinished = "download.batch_finished";
    public const string DownloadLog = "download.log";
    public const string SearchPageLoaded = "search.page_loaded";
    public const string PluginNotification = "plugin.notification";
}

/// <summary>
/// Python 事件信封。payload 保留为 <see cref="JsonElement"/>，
/// 由 UI 线程按 <see cref="Type"/> 转成强类型 payload（不在 Python 工作线程做）。
/// </summary>
public record PythonEventEnvelope
{
    [JsonPropertyName("type")]
    public string Type { get; init; } = "";

    [JsonPropertyName("payload")]
    public JsonElement Payload { get; init; }
}

/// <summary><c>download.item_started</c> 的 payload（额外带 batch_id）。</summary>
public record DownloadItemStartedPayload
{
    [JsonPropertyName("book_id")]
    public string BookId { get; init; } = "";

    [JsonPropertyName("title")]
    public string Title { get; init; } = "";

    /// <summary>所属批次；下载线程内产生的事件都有值。</summary>
    [JsonPropertyName("batch_id")]
    public string BatchId { get; init; } = "";
}

/// <summary><c>download.item_finished</c> 的 payload（额外带 batch_id）。</summary>
public record DownloadItemFinishedPayload
{
    [JsonPropertyName("book_id")]
    public string BookId { get; init; } = "";

    [JsonPropertyName("title")]
    public string Title { get; init; } = "";

    [JsonPropertyName("ok")]
    public bool Ok { get; init; }

    /// <summary>成功时为最终 ZIP 路径，失败/取消为空串。</summary>
    [JsonPropertyName("zip_path")]
    public string ZipPath { get; init; } = "";

    [JsonPropertyName("error_code")]
    public string ErrorCode { get; init; } = "";

    [JsonPropertyName("error_message")]
    public string ErrorMessage { get; init; } = "";

    [JsonPropertyName("batch_id")]
    public string BatchId { get; init; } = "";
}

/// <summary><c>download.batch_finished</c> 的 payload（额外带 batch_id）。</summary>
public record DownloadBatchFinishedPayload
{
    [JsonPropertyName("total")]
    public int Total { get; init; }

    [JsonPropertyName("success")]
    public int Success { get; init; }

    [JsonPropertyName("failed")]
    public int Failed { get; init; }

    [JsonPropertyName("cancelled")]
    public int Cancelled { get; init; }

    [JsonPropertyName("batch_id")]
    public string BatchId { get; init; } = "";
}

/// <summary><c>download.log</c> 的 payload（契约 §5.2；非下载线程 batch_id 为空串）。</summary>
public record DownloadLogPayload
{
    /// <summary>info / warning / error（由 Python 决定）。</summary>
    [JsonPropertyName("level")]
    public string Level { get; init; } = "";

    /// <summary>已脱敏的日志正文。</summary>
    [JsonPropertyName("message")]
    public string Message { get; init; } = "";

    /// <summary>批次 id；空串表示全局日志（归入全局日志窗）。</summary>
    [JsonPropertyName("batch_id")]
    public string BatchId { get; init; } = "";
}

/// <summary><c>search.page_loaded</c> 的 payload。</summary>
public record SearchPageLoadedPayload
{
    [JsonPropertyName("keyword")]
    public string Keyword { get; init; } = "";

    [JsonPropertyName("page")]
    public int Page { get; init; }

    [JsonPropertyName("total_pages")]
    public int TotalPages { get; init; }

    [JsonPropertyName("results")]
    public List<SearchResultItemDto> Results { get; init; } = [];
}

/// <summary><c>plugin.notification</c> 的 payload（阶段 2 未使用，协议保留）。</summary>
public record PluginNotificationPayload
{
    [JsonPropertyName("level")]
    public string Level { get; init; } = "";

    [JsonPropertyName("title")]
    public string Title { get; init; } = "";

    [JsonPropertyName("message")]
    public string Message { get; init; } = "";
}

/// <summary>
/// 下载日志事件参数。UI 按 <see cref="BatchId"/> 分窗：
/// 主下载页收自己的批次，详情窗收自己的批次，空串归全局日志。
/// </summary>
public sealed record DownloadLogEventArgs(string Level, string Message, string BatchId)
{
    /// <summary>是否全局日志（非下载线程产生）。</summary>
    public bool IsGlobal => string.IsNullOrEmpty(BatchId);
}

/// <summary><c>get_diagnostics(limit) -> JSON</c> 的返回（契约 §3.15）。</summary>
public record DiagnosticsDto : BridgeResultDto
{
    /// <summary>最近诊断行（已脱敏，形如 <c>[错误码] 消息</c>）。</summary>
    [JsonPropertyName("records")]
    public List<string> Records { get; init; } = [];

    /// <summary>行数（由 Python 统计）。</summary>
    [JsonPropertyName("count")]
    public int Count { get; init; }
}

/// <summary><c>set_log_dir(path) -> JSON</c> 的返回（契约 §3.14）。</summary>
public record SetLogDirResultDto : BridgeResultDto
{
    /// <summary>实际生效的日志目录；空串表示关闭落盘。</summary>
    [JsonPropertyName("log_dir")]
    public string LogDir { get; init; } = "";
}
