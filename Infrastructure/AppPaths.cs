using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.Principal;
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
    private static string _markerDirectory = "";

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
    /// 诊断标记文件的绝对路径（程序目录或测试注入的目录）。
    /// Python 侧进程诊断追加到同一文件，路径由 C# 传入，Python 不自行拼装。
    /// </summary>
    public static string RuntimeDataRootMarkerPath
    {
        get
        {
            EnsureResolved();
            lock (Gate)
            {
                return Path.Combine(
                    string.IsNullOrWhiteSpace(_markerDirectory)
                        ? AppContext.BaseDirectory
                        : _markerDirectory,
                    RuntimeDataRootMarkerFileName);
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
        return ProbeWritableDetailed(directory).Writable;
    }

    /// <summary>
    /// <see cref="ProbeWritable"/> 的诊断版本：除了成败，还记录具体探针文件名 / 完整路径、
    /// 失败阶段、异常类型、HResult 与脱敏后的 Message（已截断）。
    /// 三级降级链的决策严格沿用 <see cref="WriteProbeOutcome.Writable"/>，语义不变。
    /// </summary>
    internal static WriteProbeOutcome ProbeWritableDetailed(string directory)
    {
        string probeName = NewProbeFileName();
        if (string.IsNullOrWhiteSpace(directory))
        {
            return WriteProbeOutcome.FromMessage(
                "", probeName, "", "路径为空", "目录路径为空或空白");
        }

        string probe;
        try
        {
            probe = Path.Combine(directory, probeName);
        }
        catch (Exception ex)
        {
            return WriteProbeOutcome.FromException(directory, probeName, "", "拼接探针路径", ex);
        }

        try
        {
            Directory.CreateDirectory(directory);
        }
        catch (Exception ex)
        {
            return WriteProbeOutcome.FromException(directory, probeName, probe, "创建目录", ex);
        }

        try
        {
            using (var stream = new FileStream(
                       probe,
                       FileMode.CreateNew,
                       FileAccess.Write,
                       FileShare.None))
            {
                stream.WriteByte(0x4A); // 'J'
                stream.Flush();
            }
        }
        catch (Exception ex)
        {
            TryDeleteFile(probe);
            return WriteProbeOutcome.FromException(directory, probeName, probe, "写探针文件", ex);
        }

        try
        {
            File.Delete(probe);
        }
        catch (Exception ex)
        {
            // 与旧判定保持完全一致：删除失败也算不可写（可能被杀软占用）。
            return WriteProbeOutcome.FromException(directory, probeName, probe, "删除探针文件", ex);
        }

        return WriteProbeOutcome.Success(directory, probeName, probe);
    }

    private static string NewProbeFileName() => $".jml-write-probe-{Guid.NewGuid():N}.tmp";

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
        Func<string, WriteProbeOutcome> probe = isWritable is null
            ? ProbeWritableDetailed
            : WrapBoolProbe(isWritable);
        string markerDirectory = string.IsNullOrWhiteSpace(programDirectory)
            ? AppContext.BaseDirectory
            : programDirectory;

        ResolveFromCandidates(
            BuildCandidates(),
            probe,
            markerDirectory,
            CaptureProcessDiagnosticsSafe());
    }

    /// <summary>
    /// 测试专用：用完全隔离的候选根（临时目录）验证三级顺序、<see cref="EnsureCreated"/>
    /// 的落点与诊断标记，不触碰真实 %APPDATA%、%TEMP% 或程序目录。
    /// </summary>
    /// <param name="processDiagnostics">
    /// 可注入的身份 / 令牌快照；null 表示现场读取（受限测试环境不依赖真实读数）。
    /// </param>
    internal static void ResolveDataRootsForTests(
        Func<string, bool> isWritable,
        string appDataRoot,
        string localDataRoot,
        string tempRoot,
        string programDirRoot,
        string markerDirectory,
        ProcessDiagnosticsSnapshot? processDiagnostics = null)
    {
        ResolveFromCandidates(
            BuildTestCandidates(appDataRoot, localDataRoot, tempRoot, programDirRoot),
            WrapBoolProbe(isWritable),
            markerDirectory,
            processDiagnostics ?? CaptureProcessDiagnosticsSafe());
    }

    /// <summary>测试专用：直接注入“带回执的写探针”，用于断言异常明细与截断。</summary>
    internal static void ResolveDataRootsForTestsDetailed(
        Func<string, WriteProbeOutcome> detailedProbe,
        string appDataRoot,
        string localDataRoot,
        string tempRoot,
        string programDirRoot,
        string markerDirectory,
        ProcessDiagnosticsSnapshot? processDiagnostics = null)
    {
        ResolveFromCandidates(
            BuildTestCandidates(appDataRoot, localDataRoot, tempRoot, programDirRoot),
            detailedProbe,
            markerDirectory,
            processDiagnostics ?? CaptureProcessDiagnosticsSafe());
    }

    private static List<RootLayout> BuildTestCandidates(
        string appDataRoot,
        string localDataRoot,
        string tempRoot,
        string programDirRoot)
    {
        return
        [
            BuildLayout(DataRootKind.AppData, "AppData", appDataRoot, localDataRoot),
            BuildLayout(DataRootKind.Temp, "Temp", tempRoot, tempRoot),
            BuildLayout(DataRootKind.ProgramDir, "ProgramDir", programDirRoot, programDirRoot),
        ];
    }

    /// <summary>把旧的可注入 bool 探针包装成带回执的探针；异常也转成明细（不中断解析）。</summary>
    private static Func<string, WriteProbeOutcome> WrapBoolProbe(Func<string, bool> probe)
    {
        return directory =>
        {
            string probeName = NewProbeFileName();
            string probePath;
            try
            {
                probePath = string.IsNullOrWhiteSpace(directory)
                    ? ""
                    : Path.Combine(directory, probeName);
            }
            catch
            {
                probePath = "";
            }

            try
            {
                return probe(directory)
                    ? WriteProbeOutcome.Success(directory, probeName, probePath)
                    : WriteProbeOutcome.InjectedFailure(directory, probeName, probePath);
            }
            catch (Exception ex)
            {
                return WriteProbeOutcome.FromException(
                    directory, probeName, probePath, "注入探针", ex);
            }
        };
    }

    private static void ResolveFromCandidates(
        List<RootLayout> candidates,
        Func<string, WriteProbeOutcome> probe,
        string markerDirectory,
        ProcessDiagnosticsSnapshot processDiagnostics)
    {
        var failures = new List<string>();
        var probeRecords = new List<LevelProbeRecord>();

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
                    WriteProbeOutcome outcome = SafeProbe(probe, directory);
                    probeRecords.Add(new LevelProbeRecord(candidate.Label, outcome));
                    if (!outcome.Writable)
                    {
                        failedPaths.Add(
                            $"{directory}（写探针失败：{DescribeProbeFailure(outcome)}）");
                    }
                }
            }

            if (failedPaths.Count == 0)
            {
                selected = candidate;
                break;
            }

            failures.Add(LogSanitizer.Sanitize(
                $"{candidate.Label} 级不可写：{string.Join("；", failedPaths)}"));
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
            _markerDirectory = markerDirectory;
            LevelFailures.Clear();
            LevelFailures.AddRange(failures);
            CreationFailures.Clear();
            _resolved = true;
        }

        WriteDiagnosticMarker(
            markerDirectory, selected, failures, probeRecords, processDiagnostics);
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
            _markerDirectory = "";
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

    /// <summary>一条逐级探针回执（级别标签 + 具体目录回执），写进诊断标记。</summary>
    private sealed record LevelProbeRecord(string Label, WriteProbeOutcome Outcome);

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

    private static WriteProbeOutcome SafeProbe(
        Func<string, WriteProbeOutcome> probe,
        string directory)
    {
        try
        {
            return probe(directory) ?? WriteProbeOutcome.FromMessage(
                directory, "", "", "探针返回空结果", "写探针未返回结果");
        }
        catch (Exception ex)
        {
            // 探测函数自身异常按“不可写”处理，但把异常明细留给诊断报告。
            return WriteProbeOutcome.FromException(
                directory, "", "", "探针自身异常", ex);
        }
    }

    /// <summary>失败明细的紧凑单行描述（异常类型 + HResult + 脱敏 Message）。</summary>
    private static string DescribeProbeFailure(WriteProbeOutcome outcome)
    {
        if (string.IsNullOrEmpty(outcome.ExceptionType))
        {
            return string.IsNullOrEmpty(outcome.Message) ? "探针返回不可写" : outcome.Message;
        }

        return $"{outcome.ExceptionType} HResult=0x{outcome.HResult:X8} Message={outcome.Message}";
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
    /// 内容包含：选定级别与目录、三级探针的完整回执（文件名 / 完整路径 / 失败阶段 /
    /// 异常类型 / HResult / 脱敏 Message）、进程身份与令牌完整性级别。
    /// 只包含路径类信息，不含任何凭据；写失败静默。
    /// </summary>
    private static void WriteDiagnosticMarker(
        string programDirectory,
        RootLayout layout,
        IReadOnlyList<string> failures,
        IReadOnlyList<LevelProbeRecord> probeRecords,
        ProcessDiagnosticsSnapshot snapshot)
    {
        try
        {
            string path = Path.Combine(programDirectory, RuntimeDataRootMarkerFileName);
            var builder = new StringBuilder();
            builder.AppendLine("JML 数据目录选择（诊断报告）");
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

            AppendProcessEnvironmentSection(builder, snapshot, programDirectory, probeRecords);

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

    /// <summary>追加“进程与环境诊断”和“逐级写探针明细”两节。</summary>
    private static void AppendProcessEnvironmentSection(
        StringBuilder builder,
        ProcessDiagnosticsSnapshot snapshot,
        string programDirectory,
        IReadOnlyList<LevelProbeRecord> probeRecords)
    {
        builder.AppendLine();
        builder.AppendLine("—— 进程与环境诊断 ——");
        builder.AppendLine(
            $"进程位数：{(snapshot.Is64BitProcess ? "64 位" : "32 位")}（{SafeValue(snapshot.Architecture)}）");
        builder.AppendLine(
            $"会话 Id：{(snapshot.SessionId >= 0 ? snapshot.SessionId.ToString() : "未知")}");
        builder.AppendLine($"进程路径：{SafeValue(snapshot.ProcessPath)}");
        builder.AppendLine($"Environment.UserName：{SafeValue(snapshot.UserName)}");
        builder.AppendLine($"WindowsIdentity：{SafeValue(snapshot.IdentityName)}");
        builder.AppendLine($"是否管理员（IsElevated）：{SafeValue(snapshot.Elevated)}");
        builder.AppendLine($"令牌完整性级别：{SafeValue(snapshot.IntegrityLevel)}");
        if (!string.IsNullOrWhiteSpace(snapshot.IntegrityDetail))
        {
            builder.AppendLine($"令牌完整性检测说明：{SafeValue(snapshot.IntegrityDetail)}");
        }

        builder.AppendLine(
            "环境变量（仅目录类）："
            + string.Join(
                "；",
                new[] { "APPDATA", "LOCALAPPDATA", "TEMP", "TMP", "USERPROFILE" }
                    .Select(name => $"{name}={SafeValue(Environment.GetEnvironmentVariable(name))}")));
        builder.AppendLine(
            $"Environment.GetFolderPath(ApplicationData)：{SafeValue(snapshot.AppDataFolder)}");
        builder.AppendLine(
            $"Environment.GetFolderPath(LocalApplicationData)：{SafeValue(snapshot.LocalAppDataFolder)}");
        builder.AppendLine($"Path.GetTempPath()：{SafeValue(snapshot.TempPath)}");
        builder.AppendLine($"程序目录：{SafeValue(programDirectory)}");

        WriteProbeOutcome programDirProbe = ProbeWritableDetailed(programDirectory);
        builder.AppendLine(programDirProbe.Writable
            ? "程序目录可写：是（再探一次成功）"
            : $"程序目录可写：否（{DescribeProbeFailure(programDirProbe)}）");
        builder.AppendLine($"  探针文件：{SafeValue(programDirProbe.ProbeFileName)}");
        builder.AppendLine($"  完整路径：{SafeValue(programDirProbe.ProbeFilePath)}");

        builder.AppendLine();
        builder.AppendLine("—— 逐级写探针明细（含成功项，便于在 shell 复现） ——");
        if (probeRecords.Count == 0)
        {
            builder.AppendLine("- 无（没有执行任何写探针）");
            return;
        }

        foreach (LevelProbeRecord record in probeRecords)
        {
            WriteProbeOutcome outcome = record.Outcome;
            builder.AppendLine($"[{record.Label}] 目录：{SafeValue(outcome.Directory)}");
            builder.AppendLine($"  结果：{(outcome.Writable ? "可写" : "不可写")}");
            builder.AppendLine($"  探针文件：{SafeValue(outcome.ProbeFileName)}");
            builder.AppendLine($"  完整路径：{SafeValue(outcome.ProbeFilePath)}");
            if (outcome.Writable)
            {
                continue;
            }

            builder.AppendLine($"  失败阶段：{SafeValue(outcome.FailureStage)}");
            builder.AppendLine(
                "  异常：" + (string.IsNullOrEmpty(outcome.ExceptionType)
                    ? "无（探针返回不可写）"
                    : SafeValue(outcome.ExceptionType)));
            builder.AppendLine($"  HResult：0x{outcome.HResult:X8}（{outcome.HResult}）");
            builder.AppendLine($"  Message：{SafeValue(outcome.Message)}");
        }
    }

    private static string SafeValue(string? value)
    {
        if (string.IsNullOrWhiteSpace(value))
        {
            return "（空）";
        }

        return LogSanitizer.Sanitize(value)
            .Replace("\r\n", " ", StringComparison.Ordinal)
            .Replace('\r', ' ')
            .Replace('\n', ' ')
            .Trim();
    }

    // ------------------------------------------------------------------
    // 进程身份 / 令牌完整性级别（判断是否被杀软降权 / 受限令牌的关键证据）
    // ------------------------------------------------------------------

    /// <summary>现场读取进程身份与完整性级别（任何一步失败只降级为“未知”，不抛异常）。</summary>
    internal static ProcessDiagnosticsSnapshot CaptureProcessDiagnostics()
    {
        string userName = SafeRead(() => Environment.UserName);
        string identityName = "未知";
        string elevated = "未知";
        string integrityLevel = "未知";
        string integrityDetail = "";

        try
        {
            using WindowsIdentity identity = WindowsIdentity.GetCurrent();
            identityName = string.IsNullOrWhiteSpace(identity.Name) ? "未知" : identity.Name;
            try
            {
                var principal = new WindowsPrincipal(identity);
                elevated = principal.IsInRole(WindowsBuiltInRole.Administrator)
                    ? "是（管理员）"
                    : "否";
            }
            catch (Exception ex)
            {
                elevated = "未知（" + DescribeException(ex) + "）";
            }
        }
        catch (Exception ex)
        {
            identityName = "未知（" + DescribeException(ex) + "）";
        }

        (integrityLevel, integrityDetail) = ReadTokenIntegrityLevel();

        int sessionId = -1;
        try
        {
            using Process process = Process.GetCurrentProcess();
            sessionId = process.SessionId;
        }
        catch (Exception ex)
        {
            integrityDetail = AppendNote(
                integrityDetail, "会话 Id 读取失败：" + DescribeException(ex));
        }

        return new ProcessDiagnosticsSnapshot(
            userName,
            identityName,
            elevated,
            integrityLevel,
            integrityDetail,
            SafeRead(() => Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData)),
            SafeRead(() => Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData)),
            SafeRead(Path.GetTempPath),
            SafeRead(() => Environment.ProcessPath ?? ""),
            RuntimeInformation.ProcessArchitecture.ToString(),
            Environment.Is64BitProcess,
            sessionId);
    }

    private static ProcessDiagnosticsSnapshot CaptureProcessDiagnosticsSafe()
    {
        try
        {
            return CaptureProcessDiagnostics();
        }
        catch (Exception ex)
        {
            return new ProcessDiagnosticsSnapshot(
                "未知", "未知", "未知", "未知",
                "进程诊断读取失败：" + DescribeException(ex),
                "", "", "", "", "未知", false, -1);
        }
    }

    private static string SafeRead(Func<string> read)
    {
        try
        {
            return read() ?? "";
        }
        catch (Exception ex)
        {
            return "读取失败（" + DescribeException(ex) + "）";
        }
    }

    private static string AppendNote(string existing, string note) =>
        string.IsNullOrEmpty(existing) ? note : existing + "；" + note;

    /// <summary>异常的单行摘要：类型 + HResult + 脱敏截断消息。</summary>
    private static string DescribeException(Exception exception)
    {
        string message = LogSanitizer.Sanitize(exception.Message ?? "")
            .Replace("\r", " ")
            .Replace("\n", " ");
        if (message.Length > 200)
        {
            message = message[..200] + "…";
        }

        return $"{exception.GetType().Name} HResult=0x{exception.HResult:X8} Message={message}";
    }

    /// <summary>
    /// 用 <c>GetTokenInformation(TokenIntegrityLevel)</c> 读取进程令牌完整性级别
    /// （Low / Medium / High / System）——判断是否被安全软件放入受限令牌 / 低完整性沙箱。
    /// 失败时返回“未知”+ Win32 错误码，绝不抛异常。
    /// </summary>
    private static (string Level, string Detail) ReadTokenIntegrityLevel()
    {
        IntPtr token = IntPtr.Zero;
        IntPtr buffer = IntPtr.Zero;
        try
        {
            if (!OpenProcessToken(GetCurrentProcess(), TokenQuery, out token))
            {
                return ("未知", "OpenProcessToken 失败：Win32=" + Marshal.GetLastWin32Error());
            }

            if (!GetTokenInformation(
                    token, TokenIntegrityLevelClass, IntPtr.Zero, 0, out int length))
            {
                int error = Marshal.GetLastWin32Error();
                if (error != ErrorInsufficientBuffer)
                {
                    return ("未知", "GetTokenInformation 取长度失败：Win32=" + error);
                }
            }

            if (length <= 0)
            {
                return ("未知", "GetTokenInformation 返回长度为 0");
            }

            buffer = Marshal.AllocHGlobal(length);
            if (!GetTokenInformation(
                    token, TokenIntegrityLevelClass, buffer, length, out _))
            {
                return ("未知", "GetTokenInformation 失败：Win32=" + Marshal.GetLastWin32Error());
            }

            SidAndAttributes label = Marshal.PtrToStructure<SidAndAttributes>(buffer);
            if (label.Sid == IntPtr.Zero)
            {
                return ("未知", "完整性级别 SID 为空");
            }

            IntPtr countPointer = GetSidSubAuthorityCount(label.Sid);
            if (countPointer == IntPtr.Zero)
            {
                return ("未知", "读取完整性级别 SID 子权威数量失败");
            }

            byte subAuthorityCount = Marshal.ReadByte(countPointer);
            if (subAuthorityCount == 0)
            {
                return ("未知", "完整性级别 SID 没有子权威");
            }

            IntPtr ridPointer = GetSidSubAuthority(
                label.Sid, (uint)(subAuthorityCount - 1));
            if (ridPointer == IntPtr.Zero)
            {
                return ("未知", "读取完整性级别 RID 失败");
            }

            uint rid = unchecked((uint)Marshal.ReadInt32(ridPointer));
            string level = rid switch
            {
                0x0000 => "Untrusted",
                0x1000 => "Low",
                0x2000 => "Medium",
                0x3000 => "High",
                0x4000 => "System",
                0x5000 => "Protected",
                _ => $"未知(0x{rid:X4})",
            };
            return (level, $"RID=0x{rid:X4}（S-1-16-{rid}）");
        }
        catch (Exception ex)
        {
            return ("未知", DescribeException(ex));
        }
        finally
        {
            if (buffer != IntPtr.Zero)
            {
                Marshal.FreeHGlobal(buffer);
            }

            if (token != IntPtr.Zero)
            {
                CloseHandle(token);
            }
        }
    }

    private const int TokenQuery = 0x0008;
    private const int TokenIntegrityLevelClass = 25;
    private const int ErrorInsufficientBuffer = 122;

    [StructLayout(LayoutKind.Sequential)]
    private struct SidAndAttributes
    {
        public IntPtr Sid;
        public uint Attributes;
    }

    [DllImport("kernel32.dll")]
    private static extern IntPtr GetCurrentProcess();

    [DllImport("advapi32.dll", SetLastError = true)]
    private static extern bool OpenProcessToken(
        IntPtr processHandle, uint desiredAccess, out IntPtr tokenHandle);

    [DllImport("advapi32.dll", SetLastError = true)]
    private static extern bool GetTokenInformation(
        IntPtr tokenHandle,
        int tokenInformationClass,
        IntPtr tokenInformation,
        int tokenInformationLength,
        out int returnLength);

    // 注意：GetSidSubAuthorityCount 返回的是“计数所在的指针”（PUCHAR），
    // 不是计数值本身；必须再 Marshal.ReadByte 一次（否则会把地址低字节当计数）。
    [DllImport("advapi32.dll", SetLastError = true)]
    private static extern IntPtr GetSidSubAuthorityCount(IntPtr sid);

    [DllImport("advapi32.dll", SetLastError = true)]
    private static extern IntPtr GetSidSubAuthority(IntPtr sid, uint subAuthorityIndex);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool CloseHandle(IntPtr handle);

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

/// <summary>
/// 一次写探针的完整回执。失败时带失败阶段、异常类型、HResult 与脱敏截断后的 Message，
/// 供 <c>runtime-data-root.txt</c> 对照 shell 复现；<see cref="Writable"/> 与旧探测语义一致。
/// </summary>
internal sealed record WriteProbeOutcome(
    bool Writable,
    string Directory,
    string ProbeFileName,
    string ProbeFilePath,
    string FailureStage,
    string ExceptionType,
    int HResult,
    string Message)
{
    /// <summary>Message 的最大长度：异常文案可能很长，诊断报告不无限膨胀。</summary>
    private const int MaxMessageLength = 300;

    public static WriteProbeOutcome Success(
        string directory, string probeFileName, string probeFilePath) =>
        new(true, directory, probeFileName, probeFilePath, "", "", 0, "");

    public static WriteProbeOutcome InjectedFailure(
        string directory, string probeFileName, string probeFilePath) =>
        new(false, directory, probeFileName, probeFilePath, "注入探针", "", 0, "注入探针返回不可写");

    public static WriteProbeOutcome FromMessage(
        string directory,
        string probeFileName,
        string probeFilePath,
        string stage,
        string message) =>
        new(false, directory, probeFileName, probeFilePath, Clean(stage), "", 0, Clean(message));

    public static WriteProbeOutcome FromException(
        string directory,
        string probeFileName,
        string probeFilePath,
        string stage,
        Exception exception) =>
        new(
            false,
            directory,
            probeFileName,
            probeFilePath,
            Clean(stage),
            exception.GetType().Name,
            exception.HResult,
            Clean(exception.Message));

    private static string Clean(string? text)
    {
        if (string.IsNullOrEmpty(text))
        {
            return "";
        }

        string flat = LogSanitizer.Sanitize(text)
            .Replace("\r\n", " ", StringComparison.Ordinal)
            .Replace('\r', ' ')
            .Replace('\n', ' ')
            .Trim();
        return flat.Length <= MaxMessageLength ? flat : flat[..MaxMessageLength] + "…";
    }
}

/// <summary>
/// 进程身份 / 令牌 / 目录环境快照（诊断报告用）。
/// 测试可注入替身，避免依赖真实令牌完整性级别；真实读取失败时字段降级为“未知”。
/// </summary>
internal sealed record ProcessDiagnosticsSnapshot(
    string UserName,
    string IdentityName,
    string Elevated,
    string IntegrityLevel,
    string IntegrityDetail,
    string AppDataFolder,
    string LocalAppDataFolder,
    string TempPath,
    string ProcessPath,
    string Architecture,
    bool Is64BitProcess,
    int SessionId);
