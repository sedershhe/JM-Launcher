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
    /// 确保所有目录存在。应在应用启动时调用一次。
    /// </summary>
    public static void EnsureCreated()
    {
        foreach (string dir in new[] { ConfigDirectory, DataDirectory, LogDirectory, CacheDirectory })
        {
            Directory.CreateDirectory(dir);
        }
    }
}
