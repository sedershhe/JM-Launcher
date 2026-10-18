using System.Text.Json;
using JML.Bridge.Dto;
using JML.Infrastructure;

namespace JML.Bridge;

/// <summary>
/// 插件启用/停用桥接（阶段 4-5）。
///
/// 所有权边界（计划书 8.2 / 10.4）：
/// - **唯一真源是 Python 侧的 <c>%APPDATA%/JML/plugins/disabled.json</c>**；
/// - C# 只做三件事：转发调用、展示 Python 返回的状态、迁移一次旧
///   <see cref="AppSettings.DisabledPlugins"/>；
/// - 不在 C# 判断"这个插件是否启用"，也不缓存禁用列表。
///
/// 迁移：首次进入插件管理（Bootstrap / ListPlugins）时，若旧设置字段非空，
/// 把它作为**本次调用的显式禁用集合**传给 Python（保证停用立即影响这次装配），
/// 随后逐项调用 <c>set_plugin_enabled</c> 落入 disabled.json，最后清空旧字段。
/// Python 调用失败时保留旧字段，下次启动重试。
/// </summary>
public static class PluginManageBridge
{
    private static readonly object MigrationGate = new();

    /// <summary>旧 AppSettings.DisabledPlugins 是否已处理完（进程内只做一次）。</summary>
    private static bool _legacyMigrationDone;

    /// <summary>
    /// 启动插件宿主（注册主程序服务 + 装配插件）。
    /// 调用前会处理一次旧设置迁移；<paramref name="disabledIdsJson"/> 为空表示
    /// 沿用 Python 磁盘上的唯一真源。
    /// </summary>
    public static BootstrapResultDto Bootstrap(
        string pluginsDir,
        string disabledIdsJson = "")
    {
        List<string>? pending;
        string effective = PrepareDisabledArgument(disabledIdsJson, out pending);

        BootstrapResultDto result = PythonGateway.CallJson<BootstrapResultDto>(
            backend => (string)backend.bootstrap(pluginsDir, effective));

        CompleteLegacyMigration(pending, result.Ok);
        return result;
    }

    /// <summary>只扫描不加载（重新扫描按钮）；参数语义同 <see cref="Bootstrap"/>。</summary>
    public static BootstrapResultDto ListPlugins(
        string pluginsDir,
        string disabledIdsJson = "")
    {
        List<string>? pending;
        string effective = PrepareDisabledArgument(disabledIdsJson, out pending);

        BootstrapResultDto result = PythonGateway.CallJson<BootstrapResultDto>(
            backend => (string)backend.list_plugins(pluginsDir, effective));

        CompleteLegacyMigration(pending, result.Ok);
        return result;
    }

    /// <summary>
    /// 启用/停用插件：写 Python 的 disabled.json（原子写）。
    /// 传输层失败也收口成失败 DTO，交给界面展示，不向 UI 抛异常。
    /// </summary>
    public static SetPluginEnabledResultDto SetPluginEnabled(
        string pluginId,
        bool enabled)
    {
        string pid = pluginId ?? string.Empty;
        try
        {
            return PythonGateway.CallJson<SetPluginEnabledResultDto>(
                backend => (string)backend.set_plugin_enabled(pid, enabled));
        }
        catch (Exception ex)
        {
            BridgeFailures.Log("切换插件启用状态", ex);
            return BridgeFailures.Create<SetPluginEnabledResultDto>(ex);
        }
    }

    /// <summary>读取插件管理状态（含 dependents，供停用前确认框使用）。</summary>
    public static PluginManagementStateDto GetManagementState(string pluginsDir)
    {
        return PythonGateway.CallJson<PluginManagementStateDto>(
            backend => (string)backend.get_plugin_management_state(pluginsDir));
    }

    /// <summary>
    /// 启动期入口：若旧 <see cref="AppSettings.DisabledPlugins"/> 非空，做一次性迁移。
    ///
    /// 没有旧数据时**不扫描插件目录**（零启动开销）；有旧数据时借一次
    /// <c>list_plugins</c>（只扫描、不 import）得到合法 id 集合，再逐项写入
    /// disabled.json 并清空旧字段。任何失败都只记日志，绝不阻断启动。
    /// </summary>
    public static void MigrateLegacyDisabledPluginsAtStartup()
    {
        lock (MigrationGate)
        {
            if (_legacyMigrationDone)
            {
                return;
            }

            if (AppSettings.Load().DisabledPlugins.All(string.IsNullOrWhiteSpace))
            {
                _legacyMigrationDone = true;
                return;
            }
        }

        string? pluginsDir = PluginBridge.LocatePluginsDirectory();
        if (pluginsDir is null)
        {
            return; // 没有插件目录：保留旧设置，等插件管理页再迁移
        }

        try
        {
            // ListPlugins 内部会 Prepare/Complete 一次迁移（只扫描，不加载插件）
            ListPlugins(pluginsDir);
        }
        catch (Exception ex)
        {
            BridgeFailures.Log("启动期迁移插件禁用列表", ex);
        }
    }

    /// <summary>
    /// 处理旧 <see cref="AppSettings.DisabledPlugins"/> 的一次性迁移。
    /// 返回本次调用应该传给 Python 的 disabled_json。
    /// </summary>
    private static string PrepareDisabledArgument(
        string disabledIdsJson,
        out List<string>? pendingLegacy)
    {
        pendingLegacy = null;
        lock (MigrationGate)
        {
            if (_legacyMigrationDone)
            {
                return disabledIdsJson ?? string.Empty;
            }

            List<string> legacy = AppSettings.Load().DisabledPlugins
                .Where(id => !string.IsNullOrWhiteSpace(id))
                .Select(id => id.Trim())
                .Distinct(StringComparer.Ordinal)
                .ToList();
            if (legacy.Count == 0)
            {
                _legacyMigrationDone = true;
                return disabledIdsJson ?? string.Empty;
            }

            pendingLegacy = legacy;
            // 调用方显式给了列表时以调用方为准；通常首启这里是空串。
            if (!string.IsNullOrWhiteSpace(disabledIdsJson))
            {
                return disabledIdsJson;
            }

            return JsonSerializer.Serialize(legacy);
        }
    }

    /// <summary>把旧设置里的禁用项写入 Python 真源，成功写入后清空旧字段。</summary>
    private static void CompleteLegacyMigration(List<string>? pendingLegacy, bool callOk)
    {
        if (pendingLegacy is null || !callOk)
        {
            // 本次 Python 调用失败：保留旧字段，下次启动重试，避免丢用户配置。
            return;
        }

        lock (MigrationGate)
        {
            if (_legacyMigrationDone)
            {
                return;
            }

            foreach (string pluginId in pendingLegacy)
            {
                SetPluginEnabledResultDto result = SetPluginEnabled(pluginId, false);
                // NOT_FOUND 表示插件已卸载：该旧配置已无意义，继续迁移其余项。
                if (!result.Ok && result.ErrorCode != "NOT_FOUND")
                {
                    // 真写失败：本轮不清理旧设置，下次启动重试。
                    return;
                }
            }

            try
            {
                AppSettings settings = AppSettings.Load();
                if (settings.DisabledPlugins.Count > 0)
                {
                    settings.DisabledPlugins.Clear();
                    settings.Save();
                }

                _legacyMigrationDone = true;
            }
            catch
            {
                // 清理失败不影响已经落盘的禁用状态：下次启动会重试清理。
            }
        }
    }
}

/// <summary>插件管理网关抽象：生产实现转发静态桥接，测试可注入假实现（离线）。</summary>
internal interface IPluginManageGateway
{
    Task<BootstrapResultDto> BootstrapAsync(string pluginsDir);

    Task<BootstrapResultDto> ListAsync(string pluginsDir);

    Task<PluginManagementStateDto> GetManagementStateAsync(string pluginsDir);

    Task<SetPluginEnabledResultDto> SetEnabledAsync(string pluginId, bool enabled);
}

/// <summary>生产用插件管理网关（所有 Python 调用都在后台线程）。</summary>
internal sealed class PluginManageGateway : IPluginManageGateway
{
    /// <summary>供插件管理视图默认构造使用。</summary>
    public static PluginManageGateway Instance { get; } = new();

    private PluginManageGateway()
    {
    }

    public Task<BootstrapResultDto> BootstrapAsync(string pluginsDir) =>
        Task.Run(() => PluginManageBridge.Bootstrap(pluginsDir));

    public Task<BootstrapResultDto> ListAsync(string pluginsDir) =>
        Task.Run(() => PluginManageBridge.ListPlugins(pluginsDir));

    public Task<PluginManagementStateDto> GetManagementStateAsync(string pluginsDir) =>
        Task.Run(() => PluginManageBridge.GetManagementState(pluginsDir));

    public Task<SetPluginEnabledResultDto> SetEnabledAsync(string pluginId, bool enabled) =>
        Task.Run(() => PluginManageBridge.SetPluginEnabled(pluginId, enabled));
}
