using System.Text.Json.Serialization;

namespace JML.Bridge.Dto;

// 契约 §3.11：本子详情 DTO。
// 简介的 200 字截断只在 Python 做，C# 直接展示 description。

/// <summary><c>get_detail(book_id) -> JSON</c> 的返回。</summary>
public record BookDetailDto : BridgeResultDto
{
    [JsonPropertyName("book_id")]
    public string BookId { get; init; } = "";

    [JsonPropertyName("title")]
    public string Title { get; init; } = "";

    [JsonPropertyName("author")]
    public string Author { get; init; } = "";

    [JsonPropertyName("page_count")]
    public int PageCount { get; init; }

    [JsonPropertyName("tags")]
    public List<string> Tags { get; init; } = [];

    /// <summary>简介；超过 200 字已由 Python 截断并追加省略号。</summary>
    [JsonPropertyName("description")]
    public string Description { get; init; } = "";

    /// <summary>是否发生过截断（由 Python 判定）。</summary>
    [JsonPropertyName("description_truncated")]
    public bool DescriptionTruncated { get; init; }

    [JsonPropertyName("cover_url")]
    public string CoverUrl { get; init; } = "";
}
