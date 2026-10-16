using System.IO;
using JML.Infrastructure;
using Python.Runtime;
using Xunit;

namespace JML.Tests;

/// <summary>
/// pythonnet 桥接冒烟测试。
/// 用于证明"C# 能真正调起 Python 并加载到业务库"，这是后续一切开发的前提。
/// </summary>
public class PythonRuntimeTests
{
    [Fact]
    public void PythonRuntime_Initialize_Succeeds()
    {
        PythonRuntime.Initialize();
        Assert.True(PythonRuntime.IsInitialized);
    }

    [Fact]
    public void PythonRuntime_CanImportJmcomic()
    {
        string version = PythonRuntime.Invoke(() =>
        {
            dynamic jm = Py.Import("jmcomic");
            return (string)jm.__version__;
        });

        Assert.False(string.IsNullOrWhiteSpace(version));
    }

    [Fact]
    public void PythonRuntime_CanCallBackendHealthCheck()
    {
        string result = PythonRuntime.Invoke(() =>
        {
            // Python 目录已随输出目录分发
            string pythonDir = Path.Combine(AppContext.BaseDirectory, "Python");
            dynamic sys = Py.Import("sys");
            dynamic path = sys.path;
            path.append(pythonDir);

            dynamic backend = Py.Import("jml_backend");
            dynamic health = backend.health_check();
            return (string)health["python_version"].ToString();
        });

        Assert.False(string.IsNullOrWhiteSpace(result));
    }
}
