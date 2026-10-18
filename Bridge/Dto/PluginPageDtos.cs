using System.Text.Json;
using System.Text.Json.Serialization;

namespace JML.Bridge.Dto;

/// <summary>
/// 插件结构化页面协议 DTO（技术方案第三节方案 A）。
///
/// 字段名与 Python <c>jml_backend.plugin_page_load / plugin_page_action</c>
/// 的 JSON 逐字对齐（snake_case）；行数据用 <see cref="JsonElement"/> 保留
/// 任意行内字段，C# 侧不做字段裁剪/改名。
///
/// 模板与标题**只来自 manifest**（Python 决定），C# 只按 template 选渲染器，
/// 插件不得返回任意界面描述。
/// </summary>

/// <summary><c>plugin_page_load</c> 的整体返回。</summary>
public record PluginPageLoadResultDto : BridgeResultDto
{
    /// <summary>模板类型：list / browser / action / form</summary>
    [JsonPropertyName("template")]
    public string Template { get; init; } = "";

    /// <summary>页面标题（manifest 声明值）</summary>
    [JsonPropertyName("title")]
    public string Title { get; init; } = "";

    /// <summary>模板 payload；失败时可能为 Undefined/Null。</summary>
    [JsonPropertyName("data")]
    public JsonElement Data { get; init; }
}

/// <summary><c>plugin_page_action</c> 的返回。</summary>
public record PluginPageActionResultDto : BridgeResultDto
{
    /// <summary>true 表示界面应整页重新调用 <c>plugin_page_load</c></summary>
    [JsonPropertyName("refresh")]
    public bool Refresh { get; init; }

    /// <summary>该模板的新 payload（可为 null）</summary>
    [JsonPropertyName("data")]
    public JsonElement? Data { get; init; }

    /// <summary>状态文案（原样展示）</summary>
    [JsonPropertyName("message")]
    public string Message { get; init; } = "";

    /// <summary>
    /// 给 C# 的转发意图（阶段 4-4 协议扩展；缺省 null）。
    /// Python 侧原样透传，C# 只交给主窗口转发，不做业务判断。
    /// </summary>
    [JsonPropertyName("intent")]
    public PluginPageIntentDto? Intent { get; init; }
}

/// <summary>
/// 页面动作的转发意图（阶段 4-4）：收藏夹「详情 / 下载」用。
/// C# 只按 <see cref="Kind"/> 转发给主窗口既有路径，不解释业务。
/// </summary>
public record PluginPageIntentDto
{
    /// <summary>意图类型：detail / download（插件与主窗口约定）</summary>
    [JsonPropertyName("kind")]
    public string Kind { get; init; } = "";

    /// <summary>单个本子号（detail 使用）</summary>
    [JsonPropertyName("book_id")]
    public string BookId { get; init; } = "";

    /// <summary>批量本子号（download 使用）</summary>
    [JsonPropertyName("book_ids")]
    public List<string> BookIds { get; init; } = [];
}

/// <summary>list 模板的列定义。</summary>
public record PluginPageColumnDto
{
    [JsonPropertyName("key")]
    public string Key { get; init; } = "";

    [JsonPropertyName("title")]
    public string Title { get; init; } = "";

    /// <summary>可选列宽（像素）；缺省为空表示自动宽度。</summary>
    [JsonPropertyName("width")]
    public double? Width { get; init; }
}

/// <summary>list 模板的行操作按钮。</summary>
public record PluginPageRowActionDto
{
    [JsonPropertyName("id")]
    public string Id { get; init; } = "";

    [JsonPropertyName("title")]
    public string Title { get; init; } = "";

    /// <summary>主按钮（用 PrimaryButton 样式）</summary>
    [JsonPropertyName("primary")]
    public bool Primary { get; init; }
}

/// <summary>list 模板筛选下拉的一个选项（阶段 4-4 协议扩展）。</summary>
public record PluginPageFilterOptionDto
{
    [JsonPropertyName("value")]
    public string Value { get; init; } = "";

    [JsonPropertyName("label")]
    public string Label { get; init; } = "";
}

/// <summary>
/// list 模板的可选筛选下拉（阶段 4-4）：C# 按 <see cref="Key"/> 把选中的
/// <see cref="Value"/> 作为页面加载参数回传，内容由插件决定。
/// </summary>
public record PluginPageFilterDto
{
    [JsonPropertyName("key")]
    public string Key { get; init; } = "";

    [JsonPropertyName("label")]
    public string Label { get; init; } = "";

    [JsonPropertyName("options")]
    public List<PluginPageFilterOptionDto> Options { get; init; } = [];

    /// <summary>当前值（与某个 option 的 value 对应）</summary>
    [JsonPropertyName("value")]
    public string Value { get; init; } = "";
}

/// <summary>list 模板 payload。</summary>
public record PluginListPageDto
{
    [JsonPropertyName("columns")]
    public List<PluginPageColumnDto> Columns { get; init; } = [];

    /// <summary>行数据：每行是 key → 任意 JSON 值（保留插件自定义字段）。</summary>
    [JsonPropertyName("rows")]
    public List<Dictionary<string, JsonElement>> Rows { get; init; } = [];

    [JsonPropertyName("page")]
    public int Page { get; init; } = 1;

    [JsonPropertyName("page_size")]
    public int PageSize { get; init; } = 50;

    [JsonPropertyName("total")]
    public int Total { get; init; }

    /// <summary>总页数（可选扩展；缺省 0 表示由 page/page_size 推算）。</summary>
    [JsonPropertyName("total_pages")]
    public int TotalPages { get; init; }

    [JsonPropertyName("row_actions")]
    public List<PluginPageRowActionDto> RowActions { get; init; } = [];

    /// <summary>
    /// 可选筛选下拉（阶段 4-4）：每项渲染成一个下拉框，变更后以
    /// <c>{page: 1, &lt;key&gt;: &lt;value&gt;}</c> 重新 plugin_page_load。
    /// 缺省空列表表示不渲染筛选栏。
    /// </summary>
    [JsonPropertyName("filters")]
    public List<PluginPageFilterDto> Filters { get; init; } = [];

    [JsonPropertyName("empty_text")]
    public string EmptyText { get; init; } = "";
}

/// <summary>browser / action 模板的文件条目。</summary>
public record PluginPageFileItemDto
{
    [JsonPropertyName("name")]
    public string Name { get; init; } = "";

    /// <summary>条目标识（路径或包内标识）；C# 原样回传给 Python，不解析。</summary>
    [JsonPropertyName("path")]
    public string Path { get; init; } = "";
}

/// <summary>browser 模板 payload。</summary>
public record PluginBrowserPageDto
{
    /// <summary>文件列表（ZIP 等）；排序由 Python 保证。</summary>
    [JsonPropertyName("items")]
    public List<PluginPageFileItemDto> Items { get; init; } = [];

    [JsonPropertyName("page")]
    public int Page { get; init; } = 1;

    [JsonPropertyName("page_size")]
    public int PageSize { get; init; } = 50;

    [JsonPropertyName("total")]
    public int Total { get; init; }

    [JsonPropertyName("total_pages")]
    public int TotalPages { get; init; }

    /// <summary>当前打开的文件在 items 中的下标；-1 表示只看文件列表。</summary>
    [JsonPropertyName("item_index")]
    public int ItemIndex { get; init; } = -1;

    /// <summary>当前图片下标（0 起）。</summary>
    [JsonPropertyName("image_index")]
    public int ImageIndex { get; init; }

    /// <summary>当前文件内的图片总数（"第 n 页 / 共 N 页"的 N）。</summary>
    [JsonPropertyName("image_count")]
    public int ImageCount { get; init; }

    [JsonPropertyName("empty_text")]
    public string EmptyText { get; init; } = "";
}

/// <summary>action 模板的动作按钮。</summary>
public record PluginPageActionDto
{
    [JsonPropertyName("id")]
    public string Id { get; init; } = "";

    [JsonPropertyName("title")]
    public string Title { get; init; } = "";
}

/// <summary>action 模板 payload。</summary>
public record PluginActionPageDto
{
    [JsonPropertyName("items")]
    public List<PluginPageFileItemDto> Items { get; init; } = [];

    /// <summary>状态：idle / running（原样展示，C# 不解释）</summary>
    [JsonPropertyName("state")]
    public string State { get; init; } = "";

    [JsonPropertyName("status_text")]
    public string StatusText { get; init; } = "";

    [JsonPropertyName("actions")]
    public List<PluginPageActionDto> Actions { get; init; } = [];

    /// <summary>监听端口（0 表示未启动；只做展示）</summary>
    [JsonPropertyName("port")]
    public int Port { get; init; }

    [JsonPropertyName("empty_text")]
    public string EmptyText { get; init; } = "";
}

/// <summary>
/// 模板 payload 的强类型转换：只做 JSON → DTO 的形状转换，
/// 不含任何业务判断（是否空、怎么排序、成功失败全在 Python）。
/// </summary>
public static class PluginPageData
{
    private static readonly JsonSerializerOptions Options = new()
    {
        PropertyNameCaseInsensitive = true,
    };

    /// <summary>data 是否为 JSON 对象（否则视为协议违约，返回 null）。</summary>
    public static bool IsObject(JsonElement data) => data.ValueKind == JsonValueKind.Object;

    public static PluginListPageDto? ToList(JsonElement data) =>
        IsObject(data)
            ? JsonSerializer.Deserialize<PluginListPageDto>(data.GetRawText(), Options)
            : null;

    public static PluginBrowserPageDto? ToBrowser(JsonElement data) =>
        IsObject(data)
            ? JsonSerializer.Deserialize<PluginBrowserPageDto>(data.GetRawText(), Options)
            : null;

    public static PluginActionPageDto? ToAction(JsonElement data) =>
        IsObject(data)
            ? JsonSerializer.Deserialize<PluginActionPageDto>(data.GetRawText(), Options)
            : null;
}
