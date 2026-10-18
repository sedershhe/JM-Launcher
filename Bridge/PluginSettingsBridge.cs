using System.Globalization;
using System.Text.Encodings.Web;
using System.Text.Json;
using JML.Bridge.Dto;

namespace JML.Bridge;

/// <summary>
/// 插件设置桥接（阶段 3 任务 3-B）：
/// <c>list_plugin_settings()</c> → 动态表单；<c>set_plugin_setting()</c> → 按 schema 校验写入。
///
/// 纪律（契约 §九）：
/// - 只转发 Python 返回值，不在 C# 重复类型/枚举/范围校验（失败以 error_code 展示）；
/// - 读设置在后台线程执行，绝不阻塞 UI 线程；
/// - 任何失败都收口成 DTO，不向界面抛异常；
/// - password 项 Python 侧不回值，C# 也不缓存任何密码字符串。
/// </summary>
public static class PluginSettingsBridge
{
    /// <summary>执行器（测试可替换以验证失败形态不抛异常）。</summary>
    internal static Func<Func<PluginSettingsDto>, Task<PluginSettingsDto>> ListExecutor { get; set; } =
        work => Task.Run(work);

    /// <summary>写入执行器（测试可替换；生产在后台线程跑同步 Python 调用）。</summary>
    internal static Func<Func<SetPluginSettingResultDto>, Task<SetPluginSettingResultDto>> SetExecutor { get; set; } =
        work => Task.Run(work);

    /// <summary>读取已加载插件的设置定义（同步读盘，已异步包装）。</summary>
    public static async Task<PluginSettingsDto> ListAsync()
    {
        try
        {
            return await ListExecutor(
                () => PythonGateway.CallJson<PluginSettingsDto>(
                    backend => (string)backend.list_plugin_settings()))
                .ConfigureAwait(false);
        }
        catch (Exception ex)
        {
            BridgeFailures.Log("读取插件设置", ex);
            return BridgeFailures.Create<PluginSettingsDto>(ex);
        }
    }

    /// <summary>
    /// 写入单个设置项。<paramref name="valueJson"/> 必须是合法 JSON 文本
    /// （文本值要带引号）；空值按 JSON null 传给 Python，由 Python 侧判定非法。
    /// </summary>
    public static async Task<SetPluginSettingResultDto> SetAsync(
        string pluginId,
        string key,
        string valueJson)
    {
        try
        {
            string pid = pluginId ?? string.Empty;
            string name = key ?? string.Empty;
            string json = string.IsNullOrWhiteSpace(valueJson) ? "null" : valueJson;
            return await SetExecutor(
                () => PythonGateway.CallJson<SetPluginSettingResultDto>(
                    backend => (string)backend.set_plugin_setting(pid, name, json)))
                .ConfigureAwait(false);
        }
        catch (Exception ex)
        {
            BridgeFailures.Log("写入插件设置", ex);
            return BridgeFailures.Create<SetPluginSettingResultDto>(ex);
        }
    }
}

/// <summary>插件设置网关抽象：生产实现只转发静态桥接，测试可注入假实现（离线）。</summary>
internal interface IPluginSettingsGateway
{
    Task<PluginSettingsDto> ListAsync();

    Task<SetPluginSettingResultDto> SetAsync(string pluginId, string key, string valueJson);
}

/// <summary>生产用插件设置网关（无状态单例）。</summary>
internal sealed class PluginSettingsGateway : IPluginSettingsGateway
{
    /// <summary>供设置页默认构造使用。</summary>
    public static PluginSettingsGateway Instance { get; } = new();

    private PluginSettingsGateway()
    {
    }

    public Task<PluginSettingsDto> ListAsync() => PluginSettingsBridge.ListAsync();

    public Task<SetPluginSettingResultDto> SetAsync(
        string pluginId,
        string key,
        string valueJson) => PluginSettingsBridge.SetAsync(pluginId, key, valueJson);
}

/// <summary>
/// 设置值 ↔ JSON 文本的纯转换（不含任何校验业务）：
/// - 读：把 Python 返回的标量转成界面可显示文本；
/// - 写：把控件值转成 JSON 文本，类型不符时 Python 会返回 INVALID_ARGUMENT。
/// </summary>
public static class PluginSettingValues
{
    /// <summary>中文不转义的序列化选项（与 Python <c>ensure_ascii=False</c> 对齐，
    /// 避免同一中文值因转义形式不同被误判为"已改动"而重复写盘）。</summary>
    private static readonly JsonSerializerOptions TextJsonOptions = new()
    {
        Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
    };

    /// <summary>JSON 标量 → 显示文本（null/缺省 → 空串）。</summary>
    public static string ToText(JsonElement? value)
    {
        if (value is not { } element)
        {
            return "";
        }

        return element.ValueKind switch
        {
            JsonValueKind.String => element.GetString() ?? "",
            JsonValueKind.True => "true",
            JsonValueKind.False => "false",
            JsonValueKind.Number => element.GetRawText(),
            JsonValueKind.Null or JsonValueKind.Undefined => "",
            _ => element.GetRawText(),
        };
    }

    /// <summary>JSON 标量 → 布尔（仅 true / "true" / 非零数字为真）。</summary>
    public static bool ToBool(JsonElement? value)
    {
        if (value is not { } element)
        {
            return false;
        }

        return element.ValueKind switch
        {
            JsonValueKind.True => true,
            JsonValueKind.String => bool.TryParse(element.GetString(), out bool parsed) && parsed,
            JsonValueKind.Number => element.TryGetDouble(out double number) && number != 0,
            _ => false,
        };
    }

    /// <summary>文本 → JSON 字符串字面量（带引号，中文不转义）。</summary>
    public static string TextToJson(string? text) =>
        JsonSerializer.Serialize(text ?? "", TextJsonOptions);

    /// <summary>布尔 → JSON 字面量。</summary>
    public static string BoolToJson(bool value) => value ? "true" : "false";

    /// <summary>
    /// 数字输入 → JSON 字面量：整数按整数（integer 类型才通过校验），
    /// 否则按浮点；两者都解析不了就原样当 JSON 字符串发出，由 Python 判类型错误。
    /// </summary>
    public static string NumberToJson(string? text)
    {
        string raw = (text ?? "").Trim();
        if (long.TryParse(raw, NumberStyles.Integer, CultureInfo.InvariantCulture, out long integer))
        {
            return integer.ToString(CultureInfo.InvariantCulture);
        }

        if (double.TryParse(raw, NumberStyles.Float, CultureInfo.InvariantCulture, out double number))
        {
            return number.ToString("R", CultureInfo.InvariantCulture);
        }

        return TextToJson(raw);
    }

    /// <summary>select 选项 → 原始 JSON 文本（保留字符串/数字/布尔类型）。</summary>
    public static string OptionToJson(JsonElement option) => option.GetRawText();
}
