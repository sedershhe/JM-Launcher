using System;
using System.Collections.Generic;
using System.IO;
using JML.Infrastructure;
using JML.Pages;
using Xunit;

namespace JML.Tests;

/// <summary>
/// 数据根三级降级链的离线回归：
/// 1) 首选 %APPDATA%/%LOCALAPPDATA% 可写时选 AppData；
/// 2) 首选不可写 → 系统临时目录\JML；
/// 3) 前两级都不可写 → 程序目录 runtime_data；
/// 4) 每级失败原因进入 LastFailures，程序目录诊断标记写入指定目录（测试用临时目录）。
///
/// 通过 <c>AppPaths.ResolveDataRootsForTests</c> 把所有候选根都指向测试输出目录下的
/// 独立随机子目录（受限环境下系统 Temp 可能不可写），不触碰真实用户目录、
/// 不写真实程序目录。
/// </summary>
[CollectionDefinition(AppPathsCollection.Name, DisableParallelization = true)]
public sealed class AppPathsCollection
{
    public const string Name = "AppPathsState";
}

[Collection(AppPathsCollection.Name)]
public sealed class AppPathsTests : IDisposable
{
    private readonly string _root;
    private readonly string _markerDirectory;
    private readonly string _appConfigRoot;
    private readonly string _localDataRoot;
    private readonly string _tempRoot;
    private readonly string _programDataRoot;

    public AppPathsTests()
    {
        AppPaths.ResetForTests();

        _root = Path.Combine(
            AppContext.BaseDirectory,
            "jml-apppaths-test-" + Guid.NewGuid().ToString("N"));
        _markerDirectory = Path.Combine(_root, "program");
        Directory.CreateDirectory(_markerDirectory);

        _appConfigRoot = Path.Combine(_root, "roaming", "JML");
        _localDataRoot = Path.Combine(_root, "local", "JML");
        _tempRoot = Path.Combine(_root, "temp", "JML");
        _programDataRoot = Path.Combine(_root, "program", "runtime_data");
    }

    public void Dispose()
    {
        AppPaths.ResetForTests();
        try
        {
            if (Directory.Exists(_root))
            {
                Directory.Delete(_root, recursive: true);
            }
        }
        catch
        {
            // 测试清理失败不影响结论。
        }
    }

    [Fact]
    public void Resolve_PrefersAppData_WhenBothRootsWritable()
    {
        AppPaths.ResolveDataRootsForTests(
            _ => true,
            _appConfigRoot,
            _localDataRoot,
            _tempRoot,
            _programDataRoot,
            _markerDirectory);

        Assert.Equal(DataRootKind.AppData, AppPaths.RootKind);
        Assert.Equal(_appConfigRoot, AppPaths.ConfigDirectory);
        Assert.Equal(Path.Combine(_localDataRoot, "data"), AppPaths.DataDirectory);
        Assert.Equal(Path.Combine(_localDataRoot, "logs"), AppPaths.LogDirectory);
        Assert.Equal(Path.Combine(_localDataRoot, "cache"), AppPaths.CacheDirectory);
        Assert.Equal(Path.Combine(_localDataRoot, "tmp"), AppPaths.TempDirectory);
        Assert.Equal(Path.Combine(_appConfigRoot, "account.dat"), AppPaths.AccountFilePath);
        Assert.Equal(Path.Combine(_appConfigRoot, "plugins"), AppPaths.PluginDataRoot);
        Assert.Empty(AppPaths.LastFailures);
    }

    [Fact]
    public void Resolve_FallsBackToTemp_WhenAppDataUnwritable()
    {
        var blocked = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
        {
            _appConfigRoot,
            _localDataRoot,
        };

        AppPaths.ResolveDataRootsForTests(
            path => !blocked.Contains(path),
            _appConfigRoot,
            _localDataRoot,
            _tempRoot,
            _programDataRoot,
            _markerDirectory);

        Assert.Equal(DataRootKind.Temp, AppPaths.RootKind);
        Assert.Equal(_tempRoot, AppPaths.ConfigDirectory);
        Assert.Equal(Path.Combine(_tempRoot, "plugins"), AppPaths.PluginDataRoot);
        Assert.Equal(Path.Combine(_tempRoot, "data"), AppPaths.DataDirectory);
        Assert.Equal(Path.Combine(_tempRoot, "logs"), AppPaths.LogDirectory);

        string failure = Assert.Single(AppPaths.LastFailures);
        Assert.Contains("AppData", failure, StringComparison.Ordinal);
        Assert.Contains("写探针失败", failure, StringComparison.Ordinal);
        // 失败原因里两个根都要出现，便于排查是配置根还是数据根不可写
        Assert.Contains(_appConfigRoot, failure, StringComparison.Ordinal);
        Assert.Contains(_localDataRoot, failure, StringComparison.Ordinal);
    }

    [Fact]
    public void Resolve_FallsBackToProgramDir_WhenAppDataAndTempUnwritable()
    {
        var blocked = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
        {
            _appConfigRoot,
            _localDataRoot,
            _tempRoot,
        };

        AppPaths.ResolveDataRootsForTests(
            path => !blocked.Contains(path),
            _appConfigRoot,
            _localDataRoot,
            _tempRoot,
            _programDataRoot,
            _markerDirectory);

        Assert.Equal(DataRootKind.ProgramDir, AppPaths.RootKind);
        Assert.Equal(_programDataRoot, AppPaths.ConfigDirectory);
        Assert.Equal(Path.Combine(_programDataRoot, "data"), AppPaths.DataDirectory);
        Assert.Equal(Path.Combine(_programDataRoot, "plugins"), AppPaths.PluginDataRoot);

        Assert.Equal(2, AppPaths.LastFailures.Count);
        Assert.Contains(AppPaths.LastFailures, item => item.Contains("AppData", StringComparison.Ordinal));
        Assert.Contains(AppPaths.LastFailures, item => item.Contains("Temp", StringComparison.Ordinal));
    }

    [Fact]
    public void Resolve_AllLevelsUnwritable_StillSelectsProgramDir_AndRecordsAllFailures()
    {
        AppPaths.ResolveDataRootsForTests(
            _ => false,
            _appConfigRoot,
            _localDataRoot,
            _tempRoot,
            _programDataRoot,
            _markerDirectory);

        Assert.Equal(DataRootKind.ProgramDir, AppPaths.RootKind);
        Assert.Equal(3, AppPaths.LastFailures.Count);
        Assert.Contains(AppPaths.LastFailures, item => item.Contains("AppData", StringComparison.Ordinal));
        Assert.Contains(AppPaths.LastFailures, item => item.Contains("Temp", StringComparison.Ordinal));
        Assert.Contains(AppPaths.LastFailures, item => item.Contains("ProgramDir", StringComparison.Ordinal));
    }

    [Fact]
    public void EnsureCreated_CreatesEverySubdirectoryUnderSelectedRoot()
    {
        var blocked = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
        {
            _appConfigRoot,
            _localDataRoot,
        };

        AppPaths.ResolveDataRootsForTests(
            path => !blocked.Contains(path),
            _appConfigRoot,
            _localDataRoot,
            _tempRoot,
            _programDataRoot,
            _markerDirectory);

        AppPaths.EnsureCreated();

        Assert.True(Directory.Exists(AppPaths.ConfigDirectory));
        Assert.True(Directory.Exists(AppPaths.DataDirectory));
        Assert.True(Directory.Exists(AppPaths.LogDirectory));
        Assert.True(Directory.Exists(AppPaths.CacheDirectory));
        Assert.True(Directory.Exists(AppPaths.TempDirectory));
        Assert.True(Directory.Exists(AppPaths.PluginDataRoot));
        // 只有“降级级别”的失败记录，不应出现子目录创建失败（“目录不可用”）
        Assert.DoesNotContain(
            AppPaths.LastFailures,
            item => item.Contains("目录不可用", StringComparison.Ordinal));
        Assert.Contains(
            AppPaths.LastFailures,
            item => item.Contains("AppData", StringComparison.Ordinal));
    }

    [Fact]
    public void Marker_ContainsSelectedRootAndFailures_WithoutCredentials()
    {
        var blocked = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
        {
            _appConfigRoot,
            _localDataRoot,
        };

        AppPaths.ResolveDataRootsForTests(
            path => !blocked.Contains(path),
            _appConfigRoot,
            _localDataRoot,
            _tempRoot,
            _programDataRoot,
            _markerDirectory);

        string markerPath = Path.Combine(
            _markerDirectory,
            AppPaths.RuntimeDataRootMarkerFileName);
        Assert.True(File.Exists(markerPath));

        string content = File.ReadAllText(markerPath);
        Assert.Contains("RootKind：Temp", content, StringComparison.Ordinal);
        Assert.Contains(_tempRoot, content, StringComparison.Ordinal);
        Assert.Contains("AppData 级不可写", content, StringComparison.Ordinal);
        Assert.Contains("时间：", content, StringComparison.Ordinal);
        Assert.DoesNotContain("password", content, StringComparison.OrdinalIgnoreCase);
        Assert.DoesNotContain("token", content, StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public void ProbeWritable_ReallyWritesAndRefusesFilePathParent()
    {
        Assert.True(AppPaths.ProbeWritable(Path.Combine(_root, "probe-ok")));
        Assert.False(AppPaths.ProbeWritable(""));

        string filePath = Path.Combine(_root, "not-a-directory.txt");
        File.WriteAllText(filePath, "x");
        Assert.False(AppPaths.ProbeWritable(Path.Combine(filePath, "child")));
    }

    [Fact]
    public void ProbeWritableDetailed_ReportsProbeFileAndFailureStage()
    {
        string okDirectory = Path.Combine(_root, "detailed-ok");
        WriteProbeOutcome success = AppPaths.ProbeWritableDetailed(okDirectory);

        Assert.True(success.Writable);
        Assert.Equal(okDirectory, success.Directory);
        Assert.StartsWith(".jml-write-probe-", success.ProbeFileName, StringComparison.Ordinal);
        Assert.Equal(
            Path.Combine(okDirectory, success.ProbeFileName),
            success.ProbeFilePath);
        // 判定仍是“实测后清理”：成功探针不留下任何文件。
        Assert.False(File.Exists(success.ProbeFilePath));

        string blocker = Path.Combine(_root, "detailed-blocker.txt");
        File.WriteAllText(blocker, "x");
        WriteProbeOutcome failure = AppPaths.ProbeWritableDetailed(
            Path.Combine(blocker, "child"));

        Assert.False(failure.Writable);
        Assert.Equal("创建目录", failure.FailureStage);
        Assert.False(string.IsNullOrEmpty(failure.ExceptionType));
        Assert.NotEqual(0, failure.HResult);
        Assert.StartsWith(
            Path.Combine(blocker, "child"),
            failure.ProbeFilePath,
            StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public void Marker_ContainsProcessAndEnvironmentDiagnostics_FromInjectedSnapshot()
    {
        var snapshot = new ProcessDiagnosticsSnapshot(
            UserName: "tester",
            IdentityName: @"DOMAIN\tester",
            Elevated: "否",
            IntegrityLevel: "Medium",
            IntegrityDetail: "RID=0x2000（S-1-16-8192）",
            AppDataFolder: @"C:\Users\tester\AppData\Roaming",
            LocalAppDataFolder: @"C:\Users\tester\AppData\Local",
            TempPath: @"C:\Users\tester\AppData\Local\Temp\",
            ProcessPath: @"C:\apps\JML\JML.exe",
            Architecture: "X64",
            Is64BitProcess: true,
            SessionId: 3);

        AppPaths.ResolveDataRootsForTests(
            _ => true,
            _appConfigRoot,
            _localDataRoot,
            _tempRoot,
            _programDataRoot,
            _markerDirectory,
            snapshot);

        string content = ReadMarker();
        Assert.Contains("RootKind：AppData", content, StringComparison.Ordinal);
        Assert.Contains("进程位数：64 位（X64）", content, StringComparison.Ordinal);
        Assert.Contains("会话 Id：3", content, StringComparison.Ordinal);
        Assert.Contains(@"WindowsIdentity：DOMAIN\tester", content, StringComparison.Ordinal);
        Assert.Contains("Environment.UserName：tester", content, StringComparison.Ordinal);
        Assert.Contains("是否管理员（IsElevated）：否", content, StringComparison.Ordinal);
        Assert.Contains("令牌完整性级别：Medium", content, StringComparison.Ordinal);
        Assert.Contains("RID=0x2000", content, StringComparison.Ordinal);
        Assert.Contains(
            @"Environment.GetFolderPath(ApplicationData)：C:\Users\tester\AppData\Roaming",
            content,
            StringComparison.Ordinal);
        Assert.Contains(
            @"Environment.GetFolderPath(LocalApplicationData)：C:\Users\tester\AppData\Local",
            content,
            StringComparison.Ordinal);
        Assert.Contains(@"Path.GetTempPath()：C:\Users\tester\AppData\Local\Temp\", content, StringComparison.Ordinal);
        Assert.Contains("程序目录可写：是", content, StringComparison.Ordinal);
        Assert.Contains("探针文件：.jml-write-probe-", content, StringComparison.Ordinal);
        Assert.Contains("完整路径：", content, StringComparison.Ordinal);
        Assert.Equal(
            Path.Combine(_markerDirectory, AppPaths.RuntimeDataRootMarkerFileName),
            AppPaths.RuntimeDataRootMarkerPath);
    }

    [Fact]
    public void Marker_RecordsProbeExceptionTypeHResultAndSanitizedMessage()
    {
        // 注入探针抛异常：类型 + HResult 必须进诊断报告，凭据必须脱敏。
        AppPaths.ResolveDataRootsForTests(
            _ => throw new UnauthorizedAccessException(
                "无法写入 password=top-secret token=VSEKRIT"),
            _appConfigRoot,
            _localDataRoot,
            _tempRoot,
            _programDataRoot,
            _markerDirectory,
            FakeSnapshot());

        string content = ReadMarker();
        Assert.Contains("UnauthorizedAccessException", content, StringComparison.Ordinal);
        Assert.Contains("HResult：0x80070005", content, StringComparison.Ordinal);
        Assert.Contains("失败阶段：注入探针", content, StringComparison.Ordinal);
        Assert.Contains("***", content, StringComparison.Ordinal);
        Assert.DoesNotContain("top-secret", content, StringComparison.Ordinal);
        Assert.DoesNotContain("VSEKRIT", content, StringComparison.Ordinal);
        // 失败原因同时进入 LastFailures，且带异常摘要。
        Assert.Contains(
            AppPaths.LastFailures,
            item => item.Contains("UnauthorizedAccessException", StringComparison.Ordinal));
    }

    [Fact]
    public void Marker_TruncatesLongProbeMessage()
    {
        string longMessage = "长" + new string('X', 900);

        AppPaths.ResolveDataRootsForTests(
            _ => throw new InvalidOperationException(longMessage),
            _appConfigRoot,
            _localDataRoot,
            _tempRoot,
            _programDataRoot,
            _markerDirectory,
            FakeSnapshot());

        string content = ReadMarker();
        Assert.Contains("…", content, StringComparison.Ordinal);
        Assert.DoesNotContain(new string('X', 400), content, StringComparison.Ordinal);
    }

    [Fact]
    public void CaptureProcessDiagnostics_ReadsRealIdentityAndIntegrityLevel()
    {
        // P/Invoke 回归：GetSidSubAuthorityCount 返回的是指针，曾把地址低字节当计数，
        // 导致完整性级别显示成随机值。这里直接断言真实读数落在已知级别集合里。
        ProcessDiagnosticsSnapshot snapshot = AppPaths.CaptureProcessDiagnostics();

        Assert.Contains(
            snapshot.IntegrityLevel,
            new[] { "Low", "Medium", "High", "System", "Protected", "Untrusted" });
        Assert.DoesNotContain("未知", snapshot.IntegrityLevel, StringComparison.Ordinal);
        Assert.False(string.IsNullOrWhiteSpace(snapshot.UserName));
        Assert.False(string.IsNullOrWhiteSpace(snapshot.IdentityName));
        Assert.True(snapshot.SessionId >= 0);
        Assert.False(string.IsNullOrWhiteSpace(snapshot.ProcessPath));
    }

    private static ProcessDiagnosticsSnapshot FakeSnapshot() => new(
        UserName: "tester",
        IdentityName: @"DOMAIN\tester",
        Elevated: "否",
        IntegrityLevel: "Medium",
        IntegrityDetail: "RID=0x2000（S-1-16-8192）",
        AppDataFolder: @"C:\Users\tester\AppData\Roaming",
        LocalAppDataFolder: @"C:\Users\tester\AppData\Local",
        TempPath: @"C:\Users\tester\AppData\Local\Temp\",
        ProcessPath: @"C:\apps\JML\JML.exe",
        Architecture: "X64",
        Is64BitProcess: true,
        SessionId: 3);

    private string ReadMarker() => File.ReadAllText(Path.Combine(
        _markerDirectory,
        AppPaths.RuntimeDataRootMarkerFileName));

    [Fact]
    public void DataRootHint_OnlyShownForDegradedKinds()
    {
        Assert.Equal("", SettingsPage.BuildDataRootHint(DataRootKind.AppData, "D:/x"));
        Assert.Contains(
            "临时目录",
            SettingsPage.BuildDataRootHint(DataRootKind.Temp, "D:/temp"),
            StringComparison.Ordinal);
        Assert.Contains(
            "runtime_data",
            SettingsPage.BuildDataRootHint(DataRootKind.ProgramDir, "D:/app/runtime_data"),
            StringComparison.Ordinal);
    }
}
