using System.Text.Json.Serialization;

namespace JML.Bridge.Dto;

// 契约《插件启用/停用》一节：set_plugin_enabled / get_plugin_management_state
// 的 DTO。字段名与 Python 返回值逐字对齐；C# 只渲染，不自己判断插件是否可用。

/// <summary>管理状态三态（与 Python plugin_state.STATUS_* 一致）。</summary>
public static class PluginStatuses
{
    public const string Loaded = "loaded";
    public const string Disabled = "disabled";
    public const string Failed = "failed";
}

/// <summary><c>get_plugin_management_state()</c> 的返回。</summary>
public record PluginManagementStateDto : BridgeResultDto
{
    /// <summary>每个插件一行（含坏 manifest 目录）。</summary>
    [JsonPropertyName("plugins")]
    public List<PluginManagementItemDto> Plugins { get; init; } = [];
}

/// <summary>管理界面的一行插件状态。</summary>
public record PluginManagementItemDto
{
    /// <summary>插件 id（停用/启用时回传，不由界面推导）。</summary>
    [JsonPropertyName("id")]
    public string Id { get; init; } = "";

    /// <summary>显示名称。</summary>
    [JsonPropertyName("name")]
    public string Name { get; init; } = "";

    /// <summary>插件版本。</summary>
    [JsonPropertyName("version")]
    public string Version { get; init; } = "";

    /// <summary>插件说明。</summary>
    [JsonPropertyName("description")]
    public string Description { get; init; } = "";

    /// <summary>是否处于启用状态（false = 已停用）。</summary>
    [JsonPropertyName("enabled")]
    public bool Enabled { get; init; } = true;

    /// <summary>loaded / disabled / failed（见 <see cref="PluginStatuses"/>）。</summary>
    [JsonPropertyName("status")]
    public string Status { get; init; } = PluginStatuses.Failed;

    /// <summary>失败/停用原因；空串表示正常。</summary>
    [JsonPropertyName("reason")]
    public string Reason { get; init; } = "";

    /// <summary>
    /// 直接或间接依赖本插件的插件 id（拓扑关系算出）。
    /// 停用前用它提示"会影响谁"。
    /// </summary>
    [JsonPropertyName("dependents")]
    public List<string> Dependents { get; init; } = [];
}

/// <summary><c>set_plugin_enabled()</c> 的返回。</summary>
public record SetPluginEnabledResultDto : BridgeResultDto
{
    /// <summary>被操作的插件 id。</summary>
    [JsonPropertyName("plugin_id")]
    public string PluginId { get; init; } = "";

    /// <summary>操作后的启用状态。</summary>
    [JsonPropertyName("enabled")]
    public bool Enabled { get; init; }

    /// <summary>enabled / disabled（便于界面直接展示）。</summary>
    [JsonPropertyName("status")]
    public string Status { get; init; } = "";
}
