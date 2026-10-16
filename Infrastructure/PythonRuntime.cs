using System.IO;
using System.Runtime.InteropServices;
using Python.Runtime;

namespace JML.Infrastructure;

/// <summary>
/// Python 运行时（pythonnet）的初始化与生命周期管理。
///
/// 设计约束：
/// 1. 严禁硬编码 Python 安装路径（不写 D:\python，也不写任何旧项目路径），
///    否则换机器即失效——这正是旧项目的已知缺陷，不得重演。
/// 2. Python 引擎整个进程只初始化一次，必须幂等。
/// 3. 多线程调用（WPF 后台线程调 Python）必须 BeginAllowThreads。
/// 4. 所有 Python 调用必须持有 GIL。
/// </summary>
public static class PythonRuntime
{
    private static readonly object _lock = new();
    private static bool _initialized;

    /// <summary>
    /// 初始化 Python 引擎。可重复调用，只有第一次真正生效。
    /// </summary>
    /// <exception cref="InvalidOperationException">找不到可用的 Python 运行时时抛出。</exception>
    public static void Initialize()
    {
        lock (_lock)
        {
            if (_initialized)
            {
                return;
            }

            string dllPath = LocatePythonDll()
                ?? throw new InvalidOperationException(
                    "未找到可用的 Python 运行时。请安装 Python 3.11~3.15（64 位），" +
                    "或通过环境变量 JML_PYTHON_HOME 指定 Python 安装目录。");

            // pythonnet 要求显式指定 Python DLL，否则抛 BadPythonDllException
            Runtime.PythonDLL = dllPath;

            PythonEngine.Initialize();

            // WPF 会在后台线程调用 Python，必须允许线程切换
            PythonEngine.BeginAllowThreads();

            _initialized = true;
        }
    }

    /// <summary>
    /// 是否已初始化。
    /// </summary>
    public static bool IsInitialized
    {
        get { lock (_lock) { return _initialized; } }
    }

    /// <summary>
    /// 按优先级查找 Python DLL：
    /// 环境变量 JML_PYTHON_HOME → PATH 中的 python → 常见安装位置 → 注册表。
    /// </summary>
    /// <returns>找到的 python3xx.dll 完整路径；未找到返回 null。</returns>
    private static string? LocatePythonDll()
    {
        // 支持的次版本号范围：与 pythonnet 3.2.1 的官方支持范围一致（3.11~3.15）
        int[] candidates = { 315, 314, 313, 312, 311 };

        foreach (string dir in CandidateDirectories())
        {
            if (string.IsNullOrWhiteSpace(dir) || !Directory.Exists(dir))
            {
                continue;
            }

            foreach (int v in candidates)
            {
                string path = Path.Combine(dir, $"python{v}.dll");
                if (File.Exists(path))
                {
                    return path;
                }
            }
        }

        return null;
    }

    /// <summary>
    /// 生成待搜索的 Python 目录序列（惰性求值，避免不必要的开销）。
    /// </summary>
    private static IEnumerable<string> CandidateDirectories()
    {
        // 1) 显式指定的最高优先级
        string? envHome = Environment.GetEnvironmentVariable("JML_PYTHON_HOME");
        if (!string.IsNullOrWhiteSpace(envHome))
        {
            yield return envHome;
        }

        // 2) PATH 中能找到的 python 可执行文件所在目录
        string? fromPath = FindPythonDirectoryOnPath();
        if (fromPath is not null)
        {
            yield return fromPath;
        }

        // 3) Windows 常见安装位置（用户级 / 系统级）
        string localAppData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        string programFiles = Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles);

        yield return Path.Combine(localAppData, "Programs", "Python");
        yield return programFiles;

        if (Directory.Exists(Path.Combine(localAppData, "Programs", "Python")))
        {
            foreach (string sub in Directory.GetDirectories(Path.Combine(localAppData, "Programs", "Python")))
            {
                yield return sub;
            }
        }

        // 4) 注册表（Python 官方安装器会写入 PythonCore 项）
        foreach (string key in new[] { @"SOFTWARE\Python\PythonCore", @"SOFTWARE\WOW6432Node\Python\PythonCore" })
        {
            foreach (string installDir in QueryRegistryInstallPaths(key))
            {
                yield return installDir;
            }
        }
    }

    /// <summary>
    /// 在 PATH 环境变量中查找 python.exe，返回其所在目录。
    /// </summary>
    private static string? FindPythonDirectoryOnPath()
    {
        string? pathEnv = Environment.GetEnvironmentVariable("PATH");
        if (string.IsNullOrWhiteSpace(pathEnv))
        {
            return null;
        }

        foreach (string dir in pathEnv.Split(Path.PathSeparator, StringSplitOptions.RemoveEmptyEntries))
        {
            string trimmed = dir.Trim('"');
            if (string.IsNullOrWhiteSpace(trimmed) || !Directory.Exists(trimmed))
            {
                continue;
            }

            string exe = Path.Combine(trimmed, "python.exe");
            if (File.Exists(exe))
            {
                return trimmed;
            }
        }

        return null;
    }

    /// <summary>
    /// 从注册表读取 Python 官方安装器的 InstallPath。
    /// </summary>
    private static IEnumerable<string> QueryRegistryInstallPaths(string baseKey)
    {
        // 注意：C# 不允许在带 catch 的 try 块里 yield return，
        // 因此先收集到列表再返回。
        var results = new List<string>();

        // 注册表读取在沙箱/权限异常时必须静默失败，不能因此中断启动
        try
        {
            using Microsoft.Win32.RegistryKey? root =
                Microsoft.Win32.Registry.LocalMachine.OpenSubKey(baseKey);
            if (root is null)
            {
                return results;
            }

            foreach (string versionKey in root.GetSubKeyNames())
            {
                using Microsoft.Win32.RegistryKey? version = root.OpenSubKey(versionKey);
                using Microsoft.Win32.RegistryKey? installPath = version?.OpenSubKey("InstallPath");
                string? value = installPath?.GetValue("") as string;
                if (!string.IsNullOrWhiteSpace(value))
                {
                    results.Add(value);
                }
            }
        }
        catch
        {
            // 忽略：注册表不可读时直接跳过这一路查找
        }

        return results;
    }

    /// <summary>
    /// 在持有 GIL 的前提下执行 Python 代码。
    /// 这是所有 Python 调用的唯一合法入口。
    /// </summary>
    public static T Invoke<T>(Func<T> action)
    {
        Initialize();
        using (Py.GIL())
        {
            return action();
        }
    }

    /// <summary>
    /// 在持有 GIL 的前提下执行 Python 代码（无返回值）。
    /// </summary>
    public static void Invoke(Action action)
    {
        Initialize();
        using (Py.GIL())
        {
            action();
        }
    }
}
