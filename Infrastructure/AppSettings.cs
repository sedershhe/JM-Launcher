using System.IO;
using System.Text.Json;

namespace JML.Infrastructure;

/// <summary>
/// 应用设置（%APPDATA%/JML/settings.json）。
/// 读写失败一律回退默认值，绝不让设置文件问题弄崩主程序。
/// 注意：本文件不存任何凭据（账号走 CredentialStore：Windows 凭据管理器 / DPAPI）。
/// </summary>
public sealed class AppSettings
{
    /// <summary>
    /// 【已迁移，仅首启兼容读取】旧版禁用的插件 id 列表。
    /// 阶段 4-5 起唯一真源是 Python 侧 <c>%APPDATA%/JML/plugins/disabled.json</c>：
    /// <c>PluginManageBridge</c> 首次进入插件管理时把这里的非空值迁移过去并清空，
    /// 之后本字段保持为空，不再作为任何启用/停用判断依据。
    /// 保留属性是为了让迁移代码与旧设置文件仍可读取（不再新增写入来源）。
    /// </summary>
    public List<string> DisabledPlugins { get; set; } = [];

    /// <summary>上次选择的下载目录（阶段 3 接入；只存路径，绝不存凭据）</summary>
    public string DownloadDir { get; set; } = "";

    private static readonly object Gate = new();

    private static string SettingsPath
    {
        get
        {
            string appData = Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData);
            return Path.Combine(appData, "JML", "settings.json");
        }
    }

    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        WriteIndented = true,
    };

    /// <summary>
    /// 解析实际可用的下载目录：配置目录存在就用它，否则回退桌面。
    /// 下载页只做这一处路径选择，避免 C# 侧各自拼装默认路径。
    /// </summary>
    public static string ResolveDownloadDirectory(string? configured)
    {
        if (!string.IsNullOrWhiteSpace(configured) && Directory.Exists(configured))
        {
            return configured;
        }

        return Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory);
    }

    /// <summary>从磁盘加载；文件不存在或损坏时返回全默认值。</summary>
    public static AppSettings Load()
    {
        lock (Gate)
        {
            try
            {
                if (!File.Exists(SettingsPath))
                {
                    return new AppSettings();
                }

                string json = File.ReadAllText(SettingsPath);
                return JsonSerializer.Deserialize<AppSettings>(json, JsonOptions) ?? new AppSettings();
            }
            catch
            {
                // 坏 JSON / 占用 / 权限：回退默认，不崩
                return new AppSettings();
            }
        }
    }

    /// <summary>保存到磁盘；失败静默（下次启动回退默认，可接受）。</summary>
    public void Save()
    {
        lock (Gate)
        {
            try
            {
                string? dir = Path.GetDirectoryName(SettingsPath);
                if (!string.IsNullOrEmpty(dir))
                {
                    Directory.CreateDirectory(dir);
                }

                File.WriteAllText(SettingsPath, JsonSerializer.Serialize(this, JsonOptions));
            }
            catch
            {
                // 保存失败不崩：设置是可再生的数据
            }
        }
    }
}
