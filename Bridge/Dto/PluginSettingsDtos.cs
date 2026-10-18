using System.Text.Json;
using System.Text.Json.Serialization;

namespace JML.Bridge.Dto;

// 契约《插件设置项》一节：list_plugin_settings / set_plugin_setting 的 DTO。
// 字段名与 Python 返回值逐字对齐（snake_case）；C# 只渲染，不做校验业务判断。

/// <summary>设置字段类型（Python describe_schema 的受限枚举）。</summary>
public static class PluginSettingFieldTypes
{
    public const string Text = "text";
    public const string Password = "password";
    public const string Number = "number";
    public const string Bool = "bool";
    public const string Select = "select";
    public const string Path = "path";
}

/// <summary>
/// <c>list_plugin_settings()</c> 的返回：已加载插件 → 设置字段定义。
/// 失败形态沿用 <see cref="BridgeResultDto"/>（ok/error_code/error_message）。
/// </summary>
public record PluginSettingsDto : BridgeResultDto
{
    /// <summary>有设置项的已加载插件（无设置项的插件不会出现在这里）。</summary>
    [JsonPropertyName("plugins")]
    public List<PluginSettingsGroupDto> Plugins { get; init; } = [];
}

/// <summary>单个插件的设置分组。</summary>
public record PluginSettingsGroupDto
{
    /// <summary>插件 id（写回设置时使用，不由界面推导）。</summary>
    [JsonPropertyName("plugin_id")]
    public string PluginId { get; init; } = "";

    /// <summary>插件显示名（分组标题）。</summary>
    [JsonPropertyName("plugin_name")]
    public string PluginName { get; init; } = "";

    /// <summary>字段列表（顺序与 schema 声明一致）。</summary>
    [JsonPropertyName("fields")]
    public List<PluginSettingFieldDto> Fields { get; init; } = [];
}

/// <summary>
/// 单个设置字段。<see cref="Value"/> 用 <see cref="JsonElement"/> 保留原始标量类型
/// （string / number / boolean），界面按 <see cref="Type"/> 选择控件与回写 JSON。
/// </summary>
public record PluginSettingFieldDto
{
    /// <summary>schema 里的 key（写回设置时使用）。</summary>
    [JsonPropertyName("key")]
    public string Key { get; init; } = "";

    /// <summary>界面显示标签；Python 侧缺省时已回填为 key。</summary>
    [JsonPropertyName("label")]
    public string Label { get; init; } = "";

    /// <summary>字段类型，取值见 <see cref="PluginSettingFieldTypes"/>。</summary>
    [JsonPropertyName("type")]
    public string Type { get; init; } = PluginSettingFieldTypes.Text;

    /// <summary>schema 默认值（password 项恒为空串）。</summary>
    [JsonPropertyName("default")]
    public JsonElement? DefaultValue { get; init; }

    /// <summary>当前生效值（含默认值回填）；password 项恒为空串。</summary>
    [JsonPropertyName("value")]
    public JsonElement? Value { get; init; }

    /// <summary>select 的可选值（元素可能是字符串/数字/布尔）。</summary>
    [JsonPropertyName("options")]
    public List<JsonElement> Options { get; init; } = [];

    /// <summary>number 下限（无则不出现，DTO 读为 null）。</summary>
    [JsonPropertyName("min")]
    public double? Min { get; init; }

    /// <summary>number 上限（无则不出现，DTO 读为 null）。</summary>
    [JsonPropertyName("max")]
    public double? Max { get; init; }

    /// <summary>
    /// 仅 password 项出现：是否已设置过值。界面据此提示「已设置」，
    /// 但**永远不会拿到明文**。
    /// </summary>
    [JsonPropertyName("has_value")]
    public bool HasValue { get; init; }
}

/// <summary><c>set_plugin_setting()</c> 的返回：成功时回传 key 与（非敏感项的）当前值。</summary>
public record SetPluginSettingResultDto : BridgeResultDto
{
    /// <summary>被写入的字段 key。</summary>
    [JsonPropertyName("key")]
    public string Key { get; init; } = "";

    /// <summary>写入后的值；password 项恒为空串。</summary>
    [JsonPropertyName("value")]
    public JsonElement? Value { get; init; }

    /// <summary>仅 password 项出现：写入后是否已有值（明文不回传）。</summary>
    [JsonPropertyName("has_value")]
    public bool HasValue { get; init; }
}
