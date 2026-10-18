using System.IO;
using JML.Bridge;
using JML.Infrastructure;
using Xunit;

namespace JML.Tests;

/// <summary>
/// 无界面自检（阶段 5）的可测入口：直接调用 <see cref="SelfCheck.Run"/>，
/// 不启动 JML.exe（避免占用 bin 下的 exe/dll 导致构建 MSB3027）。
///
/// 自检会真实初始化 Python、扫描仓库 plugins/ 并 bootstrap；
/// 放在 PythonBackend 串行集合里，避免与其它 Python 全局状态测试互相抢占。
///
/// 清理：Python 侧 bootstrap 成功后会缓存结果，而现有
/// <c>Bootstrap_WithMissingDirectory_ReturnsFailureJson</c> 依赖“进程内尚未成功装配”；
/// 因此每个用例结束都把宿主状态复位，保证测试顺序无关。
/// </summary>
[Collection(PythonBackendCollection.Name)]
public class SelfCheckTests
{
    [Fact]
    public void Run_WithRepoPlugins_AllChecksPass_AndTextHasKeyItems()
    {
        try
        {
            string pluginsDir = FindRepoPluginsDirectory();

            SelfCheckReport report = SelfCheck.Run(pluginsDir);

            Assert.Equal(0, report.ExitCode);
            Assert.True(report.AllPassed, report.Text);
            Assert.Equal(6, report.Items.Count);
            Assert.All(
                report.Items,
                item => Assert.True(item.Ok, $"{item.Name} 未通过：{item.Detail}"));

            // 六项固定检查（顺序与 SelfCheck.Run 一致）
            Assert.Equal(
                new[] { "Python 运行时", "Python 依赖", "插件扫描", "插件装配", "服务注册表", "配置契约" },
                report.Items.Select(item => item.Name).ToArray());

            // 人类可读文本必须给出 OK/FAIL 与关键快照
            Assert.Contains("[OK] Python 运行时", report.Text);
            Assert.Contains("[OK] Python 依赖", report.Text);
            Assert.Contains("[OK] 插件扫描", report.Text);
            Assert.Contains("[OK] 插件装配", report.Text);
            Assert.Contains("[OK] 服务注册表", report.Text);
            Assert.Contains("[OK] 配置契约", report.Text);
            Assert.Contains("jm.search/1", report.Text);
            Assert.Contains("jm.detail/1", report.Text);
            Assert.Contains("jm.download/1", report.Text);
            Assert.Contains("jm.account/1", report.Text);
            Assert.Contains("jm.session/1", report.Text);
            Assert.Contains("archive_reader/1", report.Text);
            Assert.Contains("退出码：0", report.Text);
        }
        finally
        {
            ResetPythonHostState();
        }
    }

    [Fact]
    public void Run_WithMissingPluginsDirectory_ReportsFailureAndExitCode1()
    {
        try
        {
            string missing = Path.Combine(
                Path.GetTempPath(),
                "jml-selfcheck-missing-" + Guid.NewGuid().ToString("N"));

            SelfCheckReport report = SelfCheck.Run(missing);

            Assert.Equal(1, report.ExitCode);
            Assert.False(report.AllPassed);
            SelfCheckItem scan = Assert.Single(report.Items, item => item.Name == "插件扫描");
            Assert.False(scan.Ok);
            Assert.Contains("[FAIL] 插件扫描", report.Text);
            Assert.Contains("退出码：1", report.Text);
        }
        finally
        {
            ResetPythonHostState();
        }
    }

    private static void ResetPythonHostState()
    {
        PythonRuntime.Invoke(() =>
        {
            dynamic backend = PythonGateway.EnsureBackend();
            backend.reset_host_state();
        });
    }

    private static string FindRepoPluginsDirectory()
    {
        string? dir = AppContext.BaseDirectory;
        while (!string.IsNullOrEmpty(dir))
        {
            if (File.Exists(Path.Combine(dir, "JML.csproj")))
            {
                return Path.Combine(dir, "plugins");
            }

            dir = Directory.GetParent(dir)?.FullName;
        }

        throw new InvalidOperationException("找不到仓库根（JML.csproj），无法定位 plugins/。");
    }
}
