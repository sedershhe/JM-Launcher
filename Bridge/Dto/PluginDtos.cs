using System.Text.Json.Serialization;

namespace JML.Bridge.Dto;

/// <summary>
/// 插件信息 DTO（对应 Python 侧 bootstrap/list_plugins 返回的 loaded[] / failed[] 元素）。
/// 注意：与 Python 侧 jml_backend.py 的返回结构保持字段一致，这里是唯一契约面。
/// </summary>
public record PluginInfoDto
{
    /// <summary>插件 id（目录名级别的唯一标识）</summary>
    [JsonPropertyName("id")]
    public string Id { get; init; } = "";

    /// <summary>显示名称</summary>
    [JsonPropertyName("name")]
    public string Name { get; init; } = "";

    /// <summary>插件版本</summary>
    [JsonPropertyName("version")]
    public string Version { get; init; } = "";

    /// <summary>插件说明</summary>
    [JsonPropertyName("description")]
    public string Description { get; init; } = "";

    /// <summary>是否为开发期测试插件</summary>
    [JsonPropertyName("development_sample")]
    public bool IsDevelopmentSample { get; init; }

    /// <summary>插件声明的页面列表</summary>
    [JsonPropertyName("pages")]
    public List<PluginPageDto> Pages { get; init; } = [];
}

/// <summary>插件页面声明。</summary>
public record PluginPageDto
{
    [JsonPropertyName("id")]
    public string Id { get; init; } = "";

    [JsonPropertyName("title")]
    public string Title { get; init; } = "";

    /// <summary>页面模板类型：list / browser / action / form</summary>
    [JsonPropertyName("template")]
    public string Template { get; init; } = "";
}

/// <summary>加载失败的插件条目。</summary>
public record LoadErrorDto
{
    [JsonPropertyName("id")]
    public string Id { get; init; } = "";

    /// <summary>中文失败原因（协议不符 / 缺依赖 / 缺服务等）</summary>
    [JsonPropertyName("reason")]
    public string Reason { get; init; } = "";
}

/// <summary>bootstrap / list_plugins 的整体返回。</summary>
public record BootstrapResultDto : BridgeResultDto
{
    [JsonPropertyName("loaded")]
    public List<PluginInfoDto> Loaded { get; init; } = [];

    [JsonPropertyName("failed")]
    public List<LoadErrorDto> Failed { get; init; } = [];

    /// <summary>当前注册表中的服务名 → 版本</summary>
    [JsonPropertyName("services")]
    public Dictionary<string, int> Services { get; init; } = [];
}
