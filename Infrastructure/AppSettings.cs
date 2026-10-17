using System.IO;
using System.Text.Json;

namespace JML.Infrastructure;

/// <summary>
/// 应用设置（%APPDATA%/JML/settings.json）。
/// 读写失败一律回退默认值，绝不让设置文件问题弄崩主程序。
/// 注意：本文件不存任何凭据（账号走凭据管理器/DPAPI，阶段 3 实现）。
/// </summary>
public sealed class AppSettings
{
    /// <summary>禁用的插件 id 列表</summary>
    public List<string> DisabledPlugins { get; set; } = [];

    /// <summary>上次选择的下载目录（阶段 3 使用，先占位）</summary>
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
