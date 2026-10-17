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
                AppPaths.EnsureCreated();
                PythonRuntime.Initialize();

                // 诊断落盘目录由 C# 计算后传入，Python 侧不硬编码路径（契约 §九.3）。
                SetLogDirResultDto logDir = PythonGateway.SetLogDirectory(AppPaths.LogDirectory);
                if (!logDir.Ok)
                {
                    LogSanitizer.Log(
                        $"设置诊断目录失败：[{logDir.ErrorCode}] {logDir.ErrorMessage}");
                }

                // 事件出口注册一次；此后所有进度/日志走 EventRelay 队列。
                relay = new EventRelay(uiDispatcher);
                relay.Start();

                _events = relay;
                LastFailureMessage = null;
                LogSanitizer.Log($"Python 后端启动完成，日志目录：{AppPaths.LogDirectory}");
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
}
