using System.Collections.Generic;
using System.IO;

namespace JML.Infrastructure;

/// <summary>
/// 新项目自己的配置 / 数据 / 日志目录。
///
/// 纪律要求（对应交接文档 12.3）：
/// - 全部落在本机用户目录 %APPDATA%\JML 与 %LOCALAPPDATA%\JML 下；
/// - 严禁指向旧项目 D:\py_lj\runtime_data 或任何旧项目路径。
/// </summary>
public static class AppPaths
{
    private static readonly object FailureGate = new();
    private static readonly List<string> FailureList = new();

    /// <summary>配置 / 状态目录：%APPDATA%\JML</summary>
    public static string ConfigDirectory { get; } =
        Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "JML");

    /// <summary>数据目录：%LOCALAPPDATA%\JML\data</summary>
    public static string DataDirectory { get; } =
        Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "JML", "data");

    /// <summary>日志目录：%LOCALAPPDATA%\JML\logs</summary>
    public static string LogDirectory { get; } =
        Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "JML", "logs");

    /// <summary>缓存目录：%LOCALAPPDATA%\JML\cache</summary>
    public static string CacheDirectory { get; } =
        Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "JML", "cache");

    /// <summary>
    /// DPAPI 回退凭据文件：%APPDATA%\JML\account.dat（内容为 DPAPI 加密后的密文）。
    /// 首选 Windows 凭据管理器，只有它不可用时才使用本文件；绝不明文。
    /// </summary>
    public static string AccountFilePath { get; } =
        Path.Combine(ConfigDirectory, "account.dat");

    /// <summary>
    /// 下载临时工作目录：%LOCALAPPDATA%\JML\tmp。
    /// 启动下载时必须显式传给 Python 的 work_dir，避免崩溃残留落到用户下载夹（契约 §九.4）。
    /// </summary>
    public static string TempDirectory { get; } =
        Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "JML", "tmp");

    /// <summary>
    /// 最近一次 <see cref="EnsureCreated"/> 中创建失败的目录说明（已脱敏）。
    /// 全部成功时为空列表；返回副本，调用方改写不影响内部记录。
    /// </summary>
    public static IReadOnlyList<string> LastFailures
    {
        get
        {
            lock (FailureGate)
            {
                return FailureList.ToArray();
            }
        }
    }

    /// <summary>
    /// 确保所有目录存在。应在应用启动时调用一次。
    ///
    /// 单个目录创建失败（权限 / 占用 / 路径非法）**不抛异常**：脱敏后记录到
    /// <see cref="LastFailures"/> 并继续创建其余目录，让程序仍能启动。
    /// Python 后端起不来时仍由 <c>RuntimeBootstrap.LastFailureMessage</c> 兜底提示，
    /// 不改变既有兼容性。临时目录不可写时，下载侧由 Python 再降级到下载目录。
    /// </summary>
    public static void EnsureCreated()
    {
        List<string> failures = new();

        foreach (string dir in new[]
                 {
                     ConfigDirectory, DataDirectory, LogDirectory, CacheDirectory, TempDirectory,
                 })
        {
            TryEnsureDirectory(dir, failures);
        }

        lock (FailureGate)
        {
            FailureList.Clear();
            FailureList.AddRange(failures);
        }
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
}
