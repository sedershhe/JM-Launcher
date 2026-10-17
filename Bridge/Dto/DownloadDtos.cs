using System.Text.Json.Serialization;

namespace JML.Bridge.Dto;

// 契约 §3.7 ~ §3.9、§四：下载相关 DTO。
// 字段名与 Python 返回值逐字对齐（snake_case），C# 不做任何判断/拼装。

/// <summary><c>parse_book_ids(text) -> JSON</c> 的返回。</summary>
public record ParseIdsResultDto : BridgeResultDto
{
    /// <summary>去重保序的纯数字编号；前导零原样保留。</summary>
    [JsonPropertyName("book_ids")]
    public List<string> BookIds { get; init; } = [];

    /// <summary>编号个数（由 Python 统计，C# 不重新计数）。</summary>
    [JsonPropertyName("count")]
    public int Count { get; init; }
}

/// <summary><c>start_download(...) -> JSON</c> 的返回（立即返回，进度走事件）。</summary>
public record StartDownloadResultDto : BridgeResultDto
{
    /// <summary>批次 id，取消/查询进度用。</summary>
    [JsonPropertyName("batch_id")]
    public string BatchId { get; init; } = "";

    /// <summary>去重后的本子数。</summary>
    [JsonPropertyName("total")]
    public int Total { get; init; }

    /// <summary>固定为 <c>running</c>（由 Python 决定）。</summary>
    [JsonPropertyName("status")]
    public string Status { get; init; } = "";
}

/// <summary><c>cancel_download(batch_id) -> JSON</c> 的返回。</summary>
public record CancelDownloadResultDto : BridgeResultDto
{
    [JsonPropertyName("batch_id")]
    public string BatchId { get; init; } = "";

    /// <summary>是否已置取消标志（Python 固定返回 true）。</summary>
    [JsonPropertyName("cancelled")]
    public bool Cancelled { get; init; }
}

/// <summary>
/// 批次状态（契约 §四）：<c>get_progress</c> 与事件共用。
/// <see cref="Result"/> 只在批次结束后有值。
/// </summary>
public record BatchStatusDto : BridgeResultDto
{
    [JsonPropertyName("batch_id")]
    public string BatchId { get; init; } = "";

    /// <summary><c>running</c> / <c>finished</c>。</summary>
    [JsonPropertyName("status")]
    public string Status { get; init; } = "";

    [JsonPropertyName("total")]
    public int Total { get; init; }

    [JsonPropertyName("cancel_requested")]
    public bool CancelRequested { get; init; }

    [JsonPropertyName("result")]
    public BatchResultDto? Result { get; init; }
}

/// <summary>批次最终结果（契约 §四）。</summary>
public record BatchResultDto : BridgeResultDto
{
    [JsonPropertyName("batch_id")]
    public string BatchId { get; init; } = "";

    [JsonPropertyName("total")]
    public int Total { get; init; }

    [JsonPropertyName("success")]
    public int Success { get; init; }

    [JsonPropertyName("failed")]
    public int Failed { get; init; }

    [JsonPropertyName("cancelled")]
    public int Cancelled { get; init; }

    /// <summary>与输入编号顺序一致的单本结果。</summary>
    [JsonPropertyName("items")]
    public List<DownloadItemDto> Items { get; init; } = [];
}

/// <summary>单本下载结果（契约 §四）。</summary>
public record DownloadItemDto : BridgeResultDto
{
    [JsonPropertyName("book_id")]
    public string BookId { get; init; } = "";

    /// <summary>标题；失败且未取到标题时为空串。</summary>
    [JsonPropertyName("title")]
    public string Title { get; init; } = "";

    /// <summary>ZIP 产物绝对路径；失败/取消为空串。</summary>
    [JsonPropertyName("zip_path")]
    public string ZipPath { get; init; } = "";

    /// <summary>仅“被取消”的条目为 true。</summary>
    [JsonPropertyName("cancelled")]
    public bool Cancelled { get; init; }
}
