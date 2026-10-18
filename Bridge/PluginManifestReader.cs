using System.IO;
using System.Text.Json;

namespace JML.Bridge;

/// <summary>manifest 中声明的单个功能页（只读展示信息）。</summary>
public sealed record PluginManifestPageInfo
{
    public string Id { get; init; } = "";

    public string Title { get; init; } = "";

    public string Template { get; init; } = "";
}

/// <summary>
/// manifest（<c>plugin.json</c>）的只读展示信息。
///
/// 边界：这里只给插件详情窗展示「功能页 / 依赖插件 / 提供所需服务」用，
/// **不参与**插件能否加载、是否启用的判断（这些结论只来自 Python）。
/// </summary>
public sealed record PluginManifestInfo
{
    public string Id { get; init; } = "";

    public string DirectoryName { get; init; } = "";

    public List<PluginManifestPageInfo> Pages { get; init; } = [];

    public List<string> Dependencies { get; init; } = [];

    public List<string> Provides { get; init; } = [];

    public List<string> Requires { get; init; } = [];
}

/// <summary>
/// 插件 manifest 只读读取器（插件详情窗专用）。
///
/// 为什么在 C# 读：现有 Python 管理状态接口只返回
/// id / name / version / description / enabled / status / reason / dependents，
/// 不含 manifest 里的 pages / dependencies / services。这里只做描述性读取，
/// 不校验、不缓存状态、不改变插件装配结果；读取失败一律跳过，
/// 界面显示「（无）」而不是抛出异常。
/// </summary>
public static class PluginManifestReader
{
    private const string ManifestFileName = "plugin.json";

    /// <summary>读取插件目录下所有合法 <c>plugin.json</c>；按 manifest id 建索引。</summary>
    public static IReadOnlyDictionary<string, PluginManifestInfo> ReadAll(string? pluginsDir)
    {
        var result = new Dictionary<string, PluginManifestInfo>(StringComparer.Ordinal);
        if (string.IsNullOrWhiteSpace(pluginsDir) || !Directory.Exists(pluginsDir))
        {
            return result;
        }

        string[] directories;
        try
        {
            directories = Directory.GetDirectories(pluginsDir);
        }
        catch
        {
            // 无权限 / 目录被占用：详情窗显示「（无）」，不影响主列表。
            return result;
        }

        foreach (string directory in directories)
        {
            string path = Path.Combine(directory, ManifestFileName);
            if (!File.Exists(path))
            {
                continue;
            }

            PluginManifestInfo? info = TryReadFile(path);
            if (info is not null)
            {
                result.TryAdd(info.Id, info);
            }
        }

        return result;
    }

    /// <summary>读取单个 manifest；文件损坏 / 顶层不是对象时返回 null（跳过）。</summary>
    private static PluginManifestInfo? TryReadFile(string path)
    {
        try
        {
            using JsonDocument document = JsonDocument.Parse(File.ReadAllText(path));
            JsonElement root = document.RootElement;
            if (root.ValueKind != JsonValueKind.Object)
            {
                return null;
            }

            string directoryName = Path.GetFileName(Path.GetDirectoryName(path) ?? string.Empty);
            string id = ReadString(root, "id");
            if (string.IsNullOrWhiteSpace(id))
            {
                id = directoryName;
            }

            if (string.IsNullOrWhiteSpace(id))
            {
                return null;
            }

            return new PluginManifestInfo
            {
                Id = id,
                DirectoryName = directoryName,
                Pages = ReadPages(root),
                Dependencies = ReadDependencies(root),
                Provides = ReadServices(root, "provides"),
                Requires = ReadServices(root, "requires"),
            };
        }
        catch
        {
            // 坏 manifest 已在管理列表显示为「加载失败」；详情窗不再重复报错。
            return null;
        }
    }

    private static List<PluginManifestPageInfo> ReadPages(JsonElement root)
    {
        var pages = new List<PluginManifestPageInfo>();
        if (!root.TryGetProperty("pages", out JsonElement array)
            || array.ValueKind != JsonValueKind.Array)
        {
            return pages;
        }

        foreach (JsonElement item in array.EnumerateArray())
        {
            if (item.ValueKind != JsonValueKind.Object)
            {
                continue;
            }

            string id = ReadString(item, "id");
            if (string.IsNullOrWhiteSpace(id))
            {
                continue;
            }

            string title = ReadString(item, "title");
            pages.Add(new PluginManifestPageInfo
            {
                Id = id,
                Title = string.IsNullOrWhiteSpace(title) ? id : title,
                Template = ReadString(item, "template"),
            });
        }

        return pages;
    }

    private static List<string> ReadDependencies(JsonElement root)
    {
        var dependencies = new List<string>();
        if (!root.TryGetProperty("dependencies", out JsonElement block)
            || block.ValueKind != JsonValueKind.Object
            || !block.TryGetProperty("plugins", out JsonElement array)
            || array.ValueKind != JsonValueKind.Array)
        {
            return dependencies;
        }

        foreach (JsonElement item in array.EnumerateArray())
        {
            if (item.ValueKind == JsonValueKind.String
                && !string.IsNullOrWhiteSpace(item.GetString()))
            {
                dependencies.Add(item.GetString()!.Trim());
            }
        }

        return dependencies;
    }

    private static List<string> ReadServices(JsonElement root, string kind)
    {
        var services = new List<string>();
        if (!root.TryGetProperty("services", out JsonElement block)
            || block.ValueKind != JsonValueKind.Object
            || !block.TryGetProperty(kind, out JsonElement array)
            || array.ValueKind != JsonValueKind.Array)
        {
            return services;
        }

        foreach (JsonElement item in array.EnumerateArray())
        {
            if (item.ValueKind != JsonValueKind.Object)
            {
                continue;
            }

            string name = ReadString(item, "name");
            if (string.IsNullOrWhiteSpace(name))
            {
                continue;
            }

            string version = ReadVersion(item);
            services.Add(version.Length > 0 ? $"{name}/{version}" : name);
        }

        return services;
    }

    private static string ReadVersion(JsonElement item)
    {
        if (!item.TryGetProperty("version", out JsonElement value))
        {
            return string.Empty;
        }

        return value.ValueKind switch
        {
            JsonValueKind.Number => value.GetRawText(),
            JsonValueKind.String => (value.GetString() ?? string.Empty).Trim(),
            _ => string.Empty,
        };
    }

    private static string ReadString(JsonElement element, string name)
    {
        if (!element.TryGetProperty(name, out JsonElement value)
            || value.ValueKind != JsonValueKind.String)
        {
            return string.Empty;
        }

        return (value.GetString() ?? string.Empty).Trim();
    }
}
