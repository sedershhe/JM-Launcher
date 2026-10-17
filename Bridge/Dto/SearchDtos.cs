using System.Text.Json.Serialization;

namespace JML.Bridge.Dto;

// 契约 §3.10：搜索相关 DTO。
// 翻页关键词快照、页码修正、总页数全部以 Python 返回值为准。

/// <summary><c>search(keyword, page, session_key) -> JSON</c> 的返回。</summary>
public record SearchResultPageDto : BridgeResultDto
{
    /// <summary>实际使用的关键词（page &gt; 1 时可能来自会话快照）。</summary>
    [JsonPropertyName("keyword")]
    public string Keyword { get; init; } = "";

    /// <summary>实际页码（≥1）。</summary>
    [JsonPropertyName("page")]
    public int Page { get; init; }

    /// <summary>总页数（≥0）。</summary>
    [JsonPropertyName("total_pages")]
    public int TotalPages { get; init; }

    [JsonPropertyName("results")]
    public List<SearchResultItemDto> Results { get; init; } = [];
}

/// <summary>搜索结果的单本条目（契约 §3.10）。</summary>
public record SearchResultItemDto
{
    [JsonPropertyName("book_id")]
    public string BookId { get; init; } = "";

    [JsonPropertyName("title")]
    public string Title { get; init; } = "";

    /// <summary>作者；数据源未提供时为空串。</summary>
    [JsonPropertyName("author")]
    public string Author { get; init; } = "";

    /// <summary>页数；数据源未提供时为 0。</summary>
    [JsonPropertyName("page_count")]
    public int PageCount { get; init; }

    [JsonPropertyName("tags")]
    public List<string> Tags { get; init; } = [];

    /// <summary>封面 URL；实际图片字节请调 <c>fetch_cover</c>，C# 不自行下载。</summary>
    [JsonPropertyName("cover_url")]
    public string CoverUrl { get; init; } = "";
}
