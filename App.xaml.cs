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
        // PCL2 只有浅色一套观感（ModSecret.vb 无暗色色板）：切浅色，
        // 外壳（标题栏/导航/左栏）与页面统一使用 PCL2 默认主题色。
        ApplyPclLightTheme(Resources);

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

    /// <summary>
    /// 切到 PCL2 浅色主题。
    ///
    /// 为什么不用 <c>ThemeHelper.Initialize(appResources)</c>：
    /// 该库只按 <c>Source</c> 字符串里的 "/Colors.xaml" 识别调色板；当 Generic.xaml 由 BAML
    /// 载入时，其内部合并字典的 Source 是相对串 <c>"Colors.xaml"</c>（不含斜杠），
    /// 识别失败后 <c>SetTheme</c> 会静默返回，界面停在 ColorsDark 上。
    /// 这里自己定位 Generic.xaml 及其内部的 Colors / ColorsDark 字典，改走库提供的
    /// 显式重载（不修改外部 UI 库）。
    /// </summary>
    /// <param name="appResources">Application.Resources（顶层合并了 Generic.xaml）。</param>
    public static void ApplyPclLightTheme(ResourceDictionary appResources)
    {
        ResourceDictionary? generic = null;
        foreach (var dict in appResources.MergedDictionaries)
        {
            if (SourceContains(dict, "Generic.xaml"))
            {
                generic = dict;
                break;
            }
        }

        ResourceDictionary? light = null;
        ResourceDictionary? dark = null;
        if (generic is not null)
        {
            foreach (var dict in generic.MergedDictionaries)
            {
                string source = dict.Source?.OriginalString ?? string.Empty;
                if (source.EndsWith("ColorsDark.xaml", StringComparison.OrdinalIgnoreCase))
                {
                    dark = dict;
                }
                else if (source.EndsWith("Colors.xaml", StringComparison.OrdinalIgnoreCase))
                {
                    light = dict;
                }
            }
        }

        if (generic is not null && light is not null && dark is not null)
        {
            ThemeHelper.Initialize(generic, light, dark);
        }
        else
        {
            // 兜底：沿用库自带识别（将来资源结构变化时不硬失败）。
            ThemeHelper.Initialize(appResources);
        }

        ThemeHelper.SetTheme(true);
    }

    private static bool SourceContains(ResourceDictionary dict, string fileName)
    {
        string source = dict.Source?.OriginalString ?? string.Empty;
        return source.IndexOf(fileName, StringComparison.OrdinalIgnoreCase) >= 0;
    }

    protected override void OnExit(ExitEventArgs e)
    {
        // 移除 Python 事件出口，避免进程退出阶段 Python 回调到已释放的 WPF 对象。
        RuntimeBootstrap.Stop();
        base.OnExit(e);
    }
}
