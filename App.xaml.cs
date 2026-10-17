using System.Windows;
using JML.Bridge;
using PlainToolkit.UI;

namespace JML;

/// <summary>
/// 应用程序入口。
/// 职责仅限：初始化 PCL2 风格主题 + 启动桥接装配 + 启动主窗口。
/// 按项目纪律，C# 层不做任何业务功能逻辑，业务逻辑全部在 Python 侧。
/// </summary>
public partial class App : Application
{
    protected override void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);

        // 初始化 PCL2 风格主题支持（必须在 UI 线程、在窗口创建前调用）
        ThemeHelper.Initialize(Resources);

        // PCL2 主色调为深色风格：false = 深色，true = 浅色
        ThemeHelper.SetTheme(false);

        // 桥接启动装配：目录 → PythonRuntime → set_log_dir → EventRelay。
        // 不在这里 bootstrap 插件（保持阶段 1 的插件页时机）。
        // 失败不崩溃：给出可读提示，PythonSetupPage 仍会负责环境检测与引导。
        bool started = RuntimeBootstrap.Start(action => Dispatcher.BeginInvoke(action));
        if (!started)
        {
            string message = RuntimeBootstrap.LastFailureMessage ?? "Python 后端启动失败。";
            MessageBox.Show(
                message + Environment.NewLine + Environment.NewLine +
                "程序仍可打开。进入主界面前会显示 Python 环境检测结果，请按提示修复后重试。",
                "JML 启动提示",
                MessageBoxButton.OK,
                MessageBoxImage.Warning);
        }
    }

    protected override void OnExit(ExitEventArgs e)
    {
        // 移除 Python 事件出口，避免进程退出阶段 Python 回调到已释放的 WPF 对象。
        RuntimeBootstrap.Stop();
        base.OnExit(e);
    }
}
