using System.IO;
using JML.Bridge.Dto;
using JML.Infrastructure;

namespace JML.Bridge;

/// <summary>
/// 启动装配：目录 → Python 运行时 → 诊断落盘 → 事件中继。
///
/// 明确**不在这里 bootstrap 插件**：插件装配保持阶段 1 的插件页时机，
/// 避免启动期读盘/import 插件卡住窗口（若要改，须先做成不阻塞 UI 的实现）。
///
/// 失败处理：Python 找不到/初始化失败时只记录可读提示并返回 false，
/// 绝不抛穿启动流程；阶段 1 的 PythonSetupPage 仍负责环境检测与引导。
/// </summary>
public static class RuntimeBootstrap
{
    private static readonly object Gate = new();
    private static EventRelay? _events;

    /// <summary>事件中继（UI 层订阅下载日志/进度用）；未成功启动时为 null。</summary>
    public static EventRelay? Events => _events;

    /// <summary>最近一次启动失败的可读原因（已脱敏）；成功时为 null。</summary>
    public static string? LastFailureMessage { get; private set; }

    /// <summary>是否已完成启动装配。</summary>
    public static bool IsStarted => _events is not null;

    /// <summary>
    /// 执行启动装配。可重复调用；已成功时直接返回 true。
    /// </summary>
    /// <param name="uiDispatcher">
    /// 事件排空的 UI 编组器。生产传 <c>action =&gt; Dispatcher.BeginInvoke(action)</c>；
    /// xUnit 不调用本方法，直接用可注入的 <see cref="EventRelay"/>。
    /// </param>
    public static bool Start(Action<Action> uiDispatcher)
    {
        lock (Gate)
        {
            if (_events is not null)
            {
                return true;
            }

            EventRelay? relay = null;
            try
            {
                // 启动时解析一次并缓存三级数据根（%APPDATA%/%LOCALAPPDATA% → 临时目录 →
                // 程序目录 runtime_data），随后所有子目录都基于选定根创建。解析失败不抛，
                // 具体级别与原因记录在 AppPaths.LastFailures + 程序目录诊断标记里。
                AppPaths.ResolveDataRoots();
                AppPaths.EnsureCreated();
                PythonRuntime.Initialize();

                // 数据根必须在 Python 初始化之后、任何写盘入口之前注入：插件配置、
                // disabled.json、插件数据目录与诊断日志都跟随它（契约 §十三）。
                BridgeResultDto dataRoot = PythonGateway.CallJson<BridgeResultDto>(
                    backend => (string)backend.set_data_root(
                        AppPaths.ConfigDirectory,
                        AppPaths.PluginDataRoot,
                        AppPaths.LogDirectory));
                if (!dataRoot.Ok)
                {
                    LogSanitizer.Log(
                        $"设置 Python 数据目录失败（回退到 Python 默认路径）：" +
                        $"[{dataRoot.ErrorCode}] {dataRoot.ErrorMessage}");
                }

                // 诊断落盘目录由 C# 计算后传入，Python 侧不硬编码路径（契约 §九.3）。
                SetLogDirResultDto logDir = PythonGateway.SetLogDirectory(AppPaths.LogDirectory);
                if (!logDir.Ok)
                {
                    LogSanitizer.Log(
                        $"设置诊断目录失败：[{logDir.ErrorCode}] {logDir.ErrorMessage}");
                }

                // 同一份 runtime-data-root.txt 追加 Python 进程诊断：sys.executable / 环境变量 /
                // 配置根、临时根、下载目录的写探针（含 errno/winerror）。路径由 C# 传入，
                // Python 不自行拼装；失败只写日志，绝不阻断启动。
                WritePythonProcessDiagnostics();

                // 事件出口注册一次；此后所有进度/日志走 EventRelay 队列。
                relay = new EventRelay(uiDispatcher);
                relay.Start();

                // 阶段 4-5：旧 AppSettings.DisabledPlugins → Python disabled.json
                // 的一次性迁移。没有旧数据时零开销；失败只记日志，不阻断启动。
                try
                {
                    PluginManageBridge.MigrateLegacyDisabledPluginsAtStartup();
                }
                catch (Exception ex)
                {
                    BridgeFailures.Log("启动期迁移插件禁用列表", ex);
                }

                _events = relay;
                LastFailureMessage = null;
                LogSanitizer.Log(
                    $"Python 后端启动完成，数据根：{AppPaths.RootKind}，日志目录：{AppPaths.LogDirectory}");
                return true;
            }
            catch (Exception ex)
            {
                relay?.Dispose();
                LastFailureMessage = LogSanitizer.Sanitize(
                    $"Python 后端启动失败：{ex.GetType().Name}: {ex.Message}");
                LogSanitizer.Log(LastFailureMessage);
                return false;
            }
        }
    }

    /// <summary>停止事件中继（应用退出时调用）；可重复调用。</summary>
    public static void Stop()
    {
        lock (Gate)
        {
            _events?.Dispose();
            _events = null;
        }
    }

    /// <summary>
    /// 在 <c>set_data_root</c> 之后追加一次 Python 进程诊断。
    ///
    /// 诊断文件路径由 C# 计算并传入（<see cref="AppPaths.RuntimeDataRootMarkerPath"/> +
    /// startup-diagnostics.log），Python 不自行拼装；Python 同时把同一份文本写进
    /// <c>logs/startup-diagnostics.log</c>（Python 侧失败也只影响该文件）。
    /// 本方法不抛异常、不改变启动成败。
    /// </summary>
    private static void WritePythonProcessDiagnostics()
    {
        try
        {
            string markerPath = AppPaths.RuntimeDataRootMarkerPath;
            string startupLogPath = Path.Combine(
                AppPaths.LogDirectory, "startup-diagnostics.log");
            // 只取“用户配置过的下载目录”；首次运行没有配置就传空串，Python 记录“未指定”。
            string downloadDir = AppSettings.Load().DownloadDir ?? string.Empty;

            string text = PythonRuntime.Invoke(() =>
            {
                dynamic backend = PythonGateway.EnsureBackend();
                return (string)backend.write_process_diagnostics(
                    markerPath,
                    startupLogPath,
                    downloadDir,
                    AppPaths.TempDirectory);
            });

            if (string.IsNullOrWhiteSpace(text))
            {
                LogSanitizer.Log(
                    "Python 进程诊断返回空文本，未追加到 runtime-data-root.txt。");
                return;
            }

            // Python 把“哪个目标文件追加失败”附在返回文本里（不向 C# 抛异常）。
            if (text.Contains("追加失败", StringComparison.Ordinal))
            {
                LogSanitizer.Log(
                    "Python 进程诊断已生成，但至少一个目标文件追加失败（详见 Python 诊断环表）。");
                return;
            }

            LogSanitizer.Log(
                $"Python 进程诊断已追加：{markerPath}（同一份也写入 {startupLogPath}）");
        }
        catch (Exception ex)
        {
            // 诊断本身失败不得影响启动，只把自己的失败记进 jml 日志。
            LogSanitizer.Log(
                $"Python 进程诊断写入失败：{ex.GetType().Name}: {ex.Message}");
        }
    }
}
