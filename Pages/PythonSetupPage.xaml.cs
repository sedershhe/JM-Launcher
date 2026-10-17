using System.Windows;
using System.Windows.Controls;
using JML.Infrastructure;

namespace JML.Pages;

/// <summary>
/// Python 环境检测引导页（阶段 1）。
/// 展示检测结果，提供"重新检测"；检测通过后允许进入主界面。
/// 注意：PythonRuntime.Inspect 可能较慢且会初始化 Python 引擎，必须在后台线程执行。
/// </summary>
public partial class PythonSetupPage : UserControl
{
    /// <summary>检测通过（用户点击"进入主界面"）时触发。</summary>
    public event EventHandler? SetupCompleted;

    public PythonSetupPage()
    {
        InitializeComponent();
        Loaded += (_, _) => RunInspect();
    }

    /// <summary>执行一次检测（后台线程跑 Inspect，结果回 UI 线程展示）。</summary>
    private void RunInspect()
    {
        BtnRetry.IsEnabled = false;
        BtnContinue.IsEnabled = false;
        TextReport.Text = "正在检测 Python 环境，请稍候…";

        Task.Run(() =>
        {
                PythonEnvironmentReport report;
                try
                {
                    report = PythonRuntime.Inspect();
                }
                catch (Exception ex)
                {
                    // 兜底：Inspect 内部已尽量自包容，这里仍防止意外异常打穿界面
                    report = new PythonEnvironmentReport { IsReady = false };
                    report.Problems.Add("检测过程出现异常：" + LogSanitizer.Sanitize(ex.Message));
                }
                return report;
            })
            .ContinueWith(t =>
            {
                PythonEnvironmentReport report = t.Result;
                TextReport.Text = report.ToUserText();
                BtnRetry.IsEnabled = true;
                BtnContinue.IsEnabled = report.IsReady;
            }, TaskScheduler.FromCurrentSynchronizationContext());
    }

    private void BtnRetry_Click(object sender, RoutedEventArgs e)
    {
        RunInspect();
    }

    private void BtnContinue_Click(object sender, RoutedEventArgs e)
    {
        SetupCompleted?.Invoke(this, EventArgs.Empty);
    }
}
