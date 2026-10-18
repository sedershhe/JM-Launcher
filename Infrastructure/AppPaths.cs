using System;
using System.Collections.Generic;
using System.IO;
using System.Text;

namespace JML.Infrastructure;

/// <summary>
/// 数据根类型（三级降级链的当前选中级别）。
/// </summary>
public enum DataRootKind
{
    /// <summary>首选：%APPDATA%\JML（配置）+ %LOCALAPPDATA%\JML（数据/日志/临时）。</summary>
    AppData,

    /// <summary>第二级：Path.GetTempPath()\JML（与 Python 的 tempfile.gettempdir() 同根）。</summary>
    Temp,

    /// <summary>最后兜底：程序运行目录下 runtime_data（AppContext.BaseDirectory\runtime_data）。</summary>
    ProgramDir,
}

/// <summary>
/// 新项目自己的配置 / 数据 / 日志目录。
///
/// 纪律要求（对应交接文档 12.3）：
/// - 首选落在本机用户目录 %APPDATA%\JML 与 %LOCALAPPDATA%\JML 下；
/// - 首选根实测不可写时按顺序降级：系统临时目录\JML → 程序目录\runtime_data；
/// - 严禁指向旧项目 D:\py_lj\runtime_data 或任何旧项目路径。
///
/// “不可写”一律用**实测**判定：创建目录 + 写临时文件 + 删除，而不是只看
/// <see cref="Directory.Exists"/>（目录存在但没有写权限是最常见的故障形态）。
/// 三级选择在启动时解析一次并缓存；选定后写入程序目录下的诊断标记
/// <c>runtime-data-root.txt</c>，内容不含任何凭据，便于用户/支持排查。
/// </summary>
public static class AppPaths
{
    /// <summary>程序目录下的数据根诊断标记文件名（不入库，.gitignore 已忽略）。</summary>
    public const string RuntimeDataRootMarkerFileName = "runtime-data-root.txt";

    private static readonly object Gate = new();
    private static readonly List<string> LevelFailures = new();
    private static readonly List<string> CreationFailures = new();

    private static bool _resolved;
    private static DataRootKind _rootKind = DataRootKind.AppData;
    private static string _configDirectory = "";
    private static string _dataDirectory = "";
    private static string _logDirectory = "";
    private static string _cacheDirectory = "";
    private static string _tempDirectory = "";

    /// <summary>
    /// 配置 / 状态目录（settings.json、account.dat、plugins/disabled.json 与插件配置）。
    /// 首选 <c>%APPDATA%\JML</c>；降级后为临时目录或程序目录下的候选根。
    /// </summary>
    public static string ConfigDirectory
    {
        get
        {
            EnsureResolved();
            lock (Gate)
            {
                return _configDirectory;
            }
        }
    }

    /// <summary>数据目录（下载/插件数据等落盘数据的根）：<c>&lt;数据根&gt;\data</c>。</summary>
    public static string DataDirectory
    {
        get
        {
            EnsureResolved();
            lock (Gate)
            {
                return _dataDirectory;
            }
        }
    }

    /// <summary>日志目录：<c>&lt;数据根&gt;\logs</c>。</summary>
    public static string LogDirectory
    {
        get
        {
            EnsureResolved();
            lock (Gate)
            {
                return _logDirectory;
            }
        }
    }

    /// <summary>缓存目录：<c>&lt;数据根&gt;\cache</c>。</summary>
    public static string CacheDirectory
    {
        get
        {
            EnsureResolved();
            lock (Gate)
            {
                return _cacheDirectory;
            }
        }
    }

    /// <summary>
    /// 下载临时工作目录：<c>&lt;数据根&gt;\tmp</c>。
    /// 启动下载时必须显式传给 Python 的 work_dir，避免崩溃残留落到用户下载夹（契约 §九.4）。
    /// </summary>
    public static string TempDirectory
    {
        get
        {
            EnsureResolved();
            lock (Gate)
            {
                return _tempDirectory;
            }
        }
    }

    /// <summary>
    /// DPAPI 回退凭据文件：<c>&lt;配置目录&gt;\account.dat</c>（内容为 DPAPI 加密后的密文）。
    /// 首选 Windows 凭据管理器，只有它不可用时才使用本文件；绝不明文。
    /// </summary>
    public static string AccountFilePath =>
        Path.Combine(ConfigDirectory, "account.dat");

    /// <summary>
    /// 插件配置与插件数据根：<c>&lt;配置目录&gt;\plugins</c>。
    /// 插件配置（config.json）、disabled.json、插件数据目录（data）都跟随这里，
    /// 与 Python 侧 <c>jml.host.paths.plugin_data_root()</c> 是同一个目录。
    /// </summary>
    public static string PluginDataRoot =>
        Path.Combine(ConfigDirectory, "plugins");

    /// <summary>当前选中的数据根级别（启动时解析一次并缓存）。</summary>
    public static DataRootKind RootKind
    {
        get
        {
            EnsureResolved();
            lock (Gate)
            {
                return _rootKind;
            }
        }
    }

    /// <summary>
    /// 最近一次解析 / 创建中记录的失败说明（已脱敏）：先列三级降级中失败的级别，
    /// 再列 <see cref="EnsureCreated"/> 中创建失败的子目录。返回副本。
    /// </summary>
    public static IReadOnlyList<string> LastFailures
    {
        get
        {
            EnsureResolved();
            lock (Gate)
            {
                var result = new List<string>(LevelFailures.Count + CreationFailures.Count);
                result.AddRange(LevelFailures);
                result.AddRange(CreationFailures);
                return result;
            }
        }
    }

    /// <summary>
    /// 实测目录可写：创建目录 → 写一个临时文件 → 删除临时文件。
    ///
    /// 这是“不可写”判定的唯一实现，C# 侧不允许只凭 <see cref="Directory.Exists"/> 判断。
    /// 任一步失败即返回 false；探测文件在失败时也会尽力清理。本方法不抛异常。
    /// </summary>
    public static bool ProbeWritable(string directory)
    {
        if (string.IsNullOrWhiteSpace(directory))
        {
            return false;
        }

        string probe = Path.Combine(
            directory,
            $".jml-write-probe-{Guid.NewGuid():N}.tmp");

        try
        {
            Directory.CreateDirectory(directory);
            using (var stream = new FileStream(
                       probe,
                       FileMode.CreateNew,
                       FileAccess.Write,
                       FileShare.None))
            {
                stream.WriteByte(0x4A); // 'J'
                stream.Flush();
            }

            File.Delete(probe);
            return true;
        }
        catch
        {
            TryDeleteFile(probe);
            return false;
        }
    }

    /// <summary>
    /// 解析并缓存三级数据根。可重复调用；重复调用会重新探测并按当时结果覆盖缓存。
    /// 启动路径只调用一次（<c>RuntimeBootstrap.Start</c>），测试通过注入的
    /// <paramref name="isWritable"/> 断言三级顺序，避免真实探测污染用户目录。
    /// </summary>
    /// <param name="isWritable">
    /// 可注入的“目录可写”探测；null 使用真实的 <see cref="ProbeWritable"/>。
    /// 参数是候选根目录（不是某个子目录）。
    /// </param>
    /// <param name="programDirectory">
    /// 诊断标记写入目录；null 使用 <see cref="AppContext.BaseDirectory"/>。
    /// 测试传临时目录，避免污染真实程序目录。
    /// </param>
    public static void ResolveDataRoots(
        Func<string, bool>? isWritable = null,
        string? programDirectory = null)
    {
        Func<string, bool> probe = isWritable ?? ProbeWritable;
        string markerDirectory = string.IsNullOrWhiteSpace(programDirectory)
            ? AppContext.BaseDirectory
            : programDirectory;

        ResolveFromCandidates(BuildCandidates(), probe, markerDirectory);
    }

    /// <summary>
    /// 测试专用：用完全隔离的候选根（临时目录）验证三级顺序、<see cref="EnsureCreated"/>
    /// 的落点与诊断标记，不触碰真实 %APPDATA%、%TEMP% 或程序目录。
    /// </summary>
    internal static void ResolveDataRootsForTests(
        Func<string, bool> isWritable,
        string appDataRoot,
        string localDataRoot,
        string tempRoot,
        string programDirRoot,
        string markerDirectory)
    {
        var candidates = new List<RootLayout>
        {
            BuildLayout(DataRootKind.AppData, "AppData", appDataRoot, localDataRoot),
            BuildLayout(DataRootKind.Temp, "Temp", tempRoot, tempRoot),
            BuildLayout(DataRootKind.ProgramDir, "ProgramDir", programDirRoot, programDirRoot),
        };
        ResolveFromCandidates(candidates, isWritable, markerDirectory);
    }

    private static void ResolveFromCandidates(
        List<RootLayout> candidates,
        Func<string, bool> probe,
        string markerDirectory)
    {
        var failures = new List<string>();

        RootLayout? selected = null;
        foreach (RootLayout candidate in candidates)
        {
            List<string> failedPaths = new();
            if (!candidate.IsValid)
            {
                failedPaths.Add("候选路径为空或不是绝对路径");
            }
            else
            {
                foreach (string directory in candidate.ProbeDirectories)
                {
                    if (!SafeProbe(probe, directory))
                    {
                        failedPaths.Add(directory);
                    }
                }
            }

            if (failedPaths.Count == 0)
            {
                selected = candidate;
                break;
            }

            failures.Add(LogSanitizer.Sanitize(
                $"{candidate.Label} 级不可写：{string.Join("；", failedPaths)}（写探针失败）"));
        }

        if (selected is null)
        {
            // 三级全部失败：仍选定 ProgramDir 作为最后兜底，让程序尽量启动；
            // 具体哪个子目录建不出来由 EnsureCreated 继续记录。
            selected = candidates[^1];
        }

        lock (Gate)
        {
            _rootKind = selected.Kind;
            _configDirectory = selected.ConfigRoot;
            _dataDirectory = Path.Combine(selected.DataRoot, "data");
            _logDirectory = Path.Combine(selected.DataRoot, "logs");
            _cacheDirectory = Path.Combine(selected.DataRoot, "cache");
            _tempDirectory = Path.Combine(selected.DataRoot, "tmp");
            LevelFailures.Clear();
            LevelFailures.AddRange(failures);
            CreationFailures.Clear();
            _resolved = true;
        }

        WriteDiagnosticMarker(markerDirectory, selected, failures);
    }

    /// <summary>
    /// 确保所有目录存在。应在应用启动时调用一次。
    ///
    /// 单个目录创建失败（权限 / 占用 / 路径非法）**不抛异常**：脱敏后记录到
    /// <see cref="LastFailures"/> 并继续创建其余目录，让程序仍能启动。
    /// Python 后端起不来时仍由 <c>RuntimeBootstrap.LastFailureMessage</c> 兜底提示，
    /// 不改变既有兼容性。
    /// </summary>
    public static void EnsureCreated()
    {
        EnsureResolved();

        string[] directories;
        lock (Gate)
        {
            directories = new[]
            {
                _configDirectory, _dataDirectory, _logDirectory,
                _cacheDirectory, _tempDirectory, PluginDataRoot,
            };
        }

        var failures = new List<string>();
        foreach (string dir in directories)
        {
            TryEnsureDirectory(dir, failures);
        }

        lock (Gate)
        {
            CreationFailures.Clear();
            CreationFailures.AddRange(failures);
        }
    }

    /// <summary>测试专用：清空解析缓存，让下一次访问按当前环境重新解析。</summary>
    internal static void ResetForTests()
    {
        lock (Gate)
        {
            _resolved = false;
            _rootKind = DataRootKind.AppData;
            _configDirectory = "";
            _dataDirectory = "";
            _logDirectory = "";
            _cacheDirectory = "";
            _tempDirectory = "";
            LevelFailures.Clear();
            CreationFailures.Clear();
        }
    }

    // ------------------------------------------------------------------
    // 三级候选根
    // ------------------------------------------------------------------

    /// <summary>一个候选数据根：配置根 + 数据根 + 探测目录。</summary>
    private sealed record RootLayout(
        DataRootKind Kind,
        string Label,
        string ConfigRoot,
        string DataRoot,
        IReadOnlyList<string> ProbeDirectories,
        bool IsValid);

    private static List<RootLayout> BuildCandidates()
    {
        string appData = Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData);
        string localAppData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        string temp = Path.GetTempPath();
        string programDataRoot = Path.Combine(AppContext.BaseDirectory, "runtime_data");

        var candidates = new List<RootLayout>
        {
            BuildLayout(
                DataRootKind.AppData,
                "AppData",
                Path.Combine(appData, "JML"),
                Path.Combine(localAppData, "JML")),
            BuildLayout(
                DataRootKind.Temp,
                "Temp",
                Path.Combine(temp, "JML"),
                Path.Combine(temp, "JML")),
            BuildLayout(
                DataRootKind.ProgramDir,
                "ProgramDir",
                programDataRoot,
                programDataRoot),
        };
        return candidates;
    }

    private static RootLayout BuildLayout(
        DataRootKind kind,
        string label,
        string configRoot,
        string dataRoot)
    {
        bool valid =
            !string.IsNullOrWhiteSpace(configRoot) &&
            !string.IsNullOrWhiteSpace(dataRoot) &&
            Path.IsPathRooted(configRoot) &&
            Path.IsPathRooted(dataRoot);

        // 探测的就是最终要用的根：AppData 的配置（APPDATA\JML）与数据
        // （LOCALAPPDATA\JML）是两个根，必须都探；Temp / ProgramDir 两者相同，去重。
        var probeDirectories = new List<string>();
        foreach (string directory in new[] { configRoot, dataRoot })
        {
            if (string.IsNullOrWhiteSpace(directory) ||
                !Path.IsPathRooted(directory) ||
                probeDirectories.Contains(directory, StringComparer.OrdinalIgnoreCase))
            {
                continue;
            }

            probeDirectories.Add(directory);
        }

        return new RootLayout(kind, label, configRoot, dataRoot, probeDirectories, valid);
    }

    private static bool SafeProbe(Func<string, bool> probe, string directory)
    {
        try
        {
            return probe(directory);
        }
        catch
        {
            // 探测函数自身异常按“不可写”处理，绝不阻断启动。
            return false;
        }
    }

    private static void EnsureResolved()
    {
        lock (Gate)
        {
            if (_resolved)
            {
                return;
            }
        }

        ResolveDataRoots();
    }

    /// <summary>
    /// 单个目录的创建：失败只追加一条脱敏说明，绝不抛出。
    /// </summary>
    private static void TryEnsureDirectory(string dir, List<string> failures)
    {
        try
        {
            Directory.CreateDirectory(dir);
        }
        catch (Exception ex)
        {
            // 脱敏后再保存：账号 / Token / Cookie 等不能进日志或界面。
            failures.Add(LogSanitizer.Sanitize(
                $"目录不可用：{dir}（{ex.GetType().Name}: {ex.Message}）"));
        }
    }

    /// <summary>
    /// 写程序目录下的数据根诊断标记（成功后覆盖）。
    /// 内容只包含选定的路径/级别/时间/失败原因，不含任何凭据；写失败静默。
    /// </summary>
    private static void WriteDiagnosticMarker(
        string programDirectory,
        RootLayout layout,
        IReadOnlyList<string> failures)
    {
        try
        {
            string path = Path.Combine(programDirectory, RuntimeDataRootMarkerFileName);
            var builder = new StringBuilder();
            builder.AppendLine("JML 数据目录选择");
            builder.AppendLine($"时间：{DateTime.Now:yyyy-MM-dd HH:mm:ss zzz}");
            builder.AppendLine($"RootKind：{layout.Kind}");
            builder.AppendLine($"配置目录：{layout.ConfigRoot}");
            builder.AppendLine($"数据目录：{Path.Combine(layout.DataRoot, "data")}");
            builder.AppendLine($"日志目录：{Path.Combine(layout.DataRoot, "logs")}");
            builder.AppendLine($"缓存目录：{Path.Combine(layout.DataRoot, "cache")}");
            builder.AppendLine($"临时目录：{Path.Combine(layout.DataRoot, "tmp")}");
            builder.AppendLine("降级失败记录：");
            if (failures.Count == 0)
            {
                builder.AppendLine("- 无（首选 %APPDATA% / %LOCALAPPDATA% 实测可写）");
            }
            else
            {
                foreach (string failure in failures)
                {
                    builder.AppendLine("- " + LogSanitizer.Sanitize(failure));
                }
            }

            Directory.CreateDirectory(programDirectory);
            File.WriteAllText(
                path,
                builder.ToString(),
                new UTF8Encoding(encoderShouldEmitUTF8Identifier: false));
        }
        catch
        {
            // 诊断标记写不出来不影响程序运行。
        }
    }

    private static void TryDeleteFile(string path)
    {
        try
        {
            if (File.Exists(path))
            {
                File.Delete(path);
            }
        }
        catch
        {
            // 清理失败不影响判定结果。
        }
    }
}
