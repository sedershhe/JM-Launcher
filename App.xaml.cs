using System.Windows;
using PlainToolkit.UI;

namespace JML;

/// <summary>
/// 应用程序入口。
/// 职责仅限：初始化 PCL2 风格主题 + 启动主窗口。
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
    }
}
