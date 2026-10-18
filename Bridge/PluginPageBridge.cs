using System.Text.Encodings.Web;
using System.Text.Json;
using JML.Bridge.Dto;

namespace JML.Bridge;

/// <summary>
/// 插件结构化页面协议桥接（技术方案第三节方案 A）：
/// <c>plugin_page_load</c> / <c>plugin_page_action</c> / <c>plugin_page_image</c>。
///
/// 纪律：
/// - 只转发 Python 返回值，C# 不判断成功失败、不排序、不改文件名；
/// - 页面数据、模板、标题全部来自 Python（模板与标题来自 manifest）；
/// - 图片只返回原始 bytes，<c>BitmapImage</c> 必须由 UI 线程创建（旧缺陷 #7）；
/// - 任何失败都收口成 DTO / null，不向界面抛异常。
/// </summary>
public static class PluginPageBridge
{
    /// <summary>中文不转义的序列化选项（参数里的中文原样交给 Python）。</summary>
    private static readonly JsonSerializerOptions ParametersJsonOptions = new()
    {
        Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
    };

    /// <summary>载入插件页面（同步读盘/业务在后台线程执行）。</summary>
    public static async Task<PluginPageLoadResultDto> LoadAsync(
        string pluginId,
        string pageId,
        IReadOnlyDictionary<string, object?>? parameters = null)
    {
        try
        {
            string paramsJson = SerializeParameters(parameters);
            string pid = pluginId ?? string.Empty;
            string page = pageId ?? string.Empty;
            return await Task.Run(
                () => PythonGateway.CallJson<PluginPageLoadResultDto>(
                    backend => (string)backend.plugin_page_load(pid, page, paramsJson)))
                .ConfigureAwait(false);
        }
        catch (Exception ex)
        {
            BridgeFailures.Log("载入插件页面", ex);
            return BridgeFailures.Create<PluginPageLoadResultDto>(ex);
        }
    }

    /// <summary>执行页面动作；<c>refresh=true</c> 时调用方应重新 LoadAsync。</summary>
    public static async Task<PluginPageActionResultDto> ActionAsync(
        string pluginId,
        string pageId,
        string actionId,
        IReadOnlyDictionary<string, object?>? payload = null)
    {
        try
        {
            string payloadJson = SerializeParameters(payload);
            string pid = pluginId ?? string.Empty;
            string page = pageId ?? string.Empty;
            string action = actionId ?? string.Empty;
            return await Task.Run(
                () => PythonGateway.CallJson<PluginPageActionResultDto>(
                    backend => (string)backend.plugin_page_action(
                        pid, page, action, payloadJson)))
                .ConfigureAwait(false);
        }
        catch (Exception ex)
        {
            BridgeFailures.Log("执行插件页面动作", ex);
            return BridgeFailures.Create<PluginPageActionResultDto>(ex);
        }
    }

    /// <summary>
    /// 取页面图片原图字节；失败返回 null（契约与 fetch_cover 一致）。
    /// 调用方必须回 UI 线程后再创建 <c>BitmapImage</c>。
    /// </summary>
    public static async Task<byte[]?> ImageAsync(string pluginId, string pageId, int index)
    {
        try
        {
            string pid = pluginId ?? string.Empty;
            string page = pageId ?? string.Empty;
            return await Task.Run(
                () => PythonGateway.CallRaw<byte[]>(
                    backend => backend.plugin_page_image(pid, page, index)))
                .ConfigureAwait(false);
        }
        catch (Exception ex)
        {
            BridgeFailures.Log("获取插件页面图片", ex);
            return null;
        }
    }

    /// <summary>参数 dict → JSON 文本；null / 空 dict 都是 <c>{}</c>。</summary>
    internal static string SerializeParameters(IReadOnlyDictionary<string, object?>? parameters)
    {
        if (parameters is null || parameters.Count == 0)
        {
            return "{}";
        }

        return JsonSerializer.Serialize(parameters, ParametersJsonOptions);
    }
}

/// <summary>
/// 插件页面网关抽象：生产实现只转发静态桥接；测试注入假实现（离线、无 Python）。
/// </summary>
internal interface IPluginPageGateway
{
    Task<PluginPageLoadResultDto> LoadAsync(
        string pluginId,
        string pageId,
        IReadOnlyDictionary<string, object?>? parameters);

    Task<PluginPageActionResultDto> ActionAsync(
        string pluginId,
        string pageId,
        string actionId,
        IReadOnlyDictionary<string, object?>? payload);

    Task<byte[]?> ImageAsync(string pluginId, string pageId, int index);
}

/// <summary>生产用插件页面网关（无状态单例）。</summary>
internal sealed class PluginPageGateway : IPluginPageGateway
{
    /// <summary>供 PluginPageHost 默认构造使用。</summary>
    public static PluginPageGateway Instance { get; } = new();

    private PluginPageGateway()
    {
    }

    public Task<PluginPageLoadResultDto> LoadAsync(
        string pluginId,
        string pageId,
        IReadOnlyDictionary<string, object?>? parameters) =>
        PluginPageBridge.LoadAsync(pluginId, pageId, parameters);

    public Task<PluginPageActionResultDto> ActionAsync(
        string pluginId,
        string pageId,
        string actionId,
        IReadOnlyDictionary<string, object?>? payload) =>
        PluginPageBridge.ActionAsync(pluginId, pageId, actionId, payload);

    public Task<byte[]?> ImageAsync(string pluginId, string pageId, int index) =>
        PluginPageBridge.ImageAsync(pluginId, pageId, index);
}
