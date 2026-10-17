using System.IO;
using JML.Bridge.Dto;

namespace JML.Bridge;

/// <summary>
/// 插件宿主桥接层：C# 与 Python 插件宿主之间的通道。
///
/// 设计约束（对应技术方案第一节“一条铁律”）：
/// 1. 返回值一律是 Python 侧 json.dumps 出的字符串，反序列化为强类型 DTO，
///    杜绝 dynamic Python 对象漏到 UI 层。
/// 2. Python 侧 jml_backend 模块每次进程只 import 一次（统一由 PythonGateway 负责）。
/// 3. 本类不做任何业务判断（哪些插件可用等结论全部来自 Python 返回值）。
/// </summary>
public static class PluginBridge
{
    /// <summary>
    /// 定位插件目录：优先输出目录 plugins/，其次仓库根 plugins/（开发调试期）。
    /// 不硬编码任何盘符；两处都不存在时返回 null（表示“无插件可扫”，不是错误）。
    /// </summary>
    public static string? LocatePluginsDirectory()
    {
        string outputDir = Path.Combine(AppContext.BaseDirectory, "plugins");
        if (Directory.Exists(outputDir))
        {
            return outputDir;
        }

        // 开发调试：仓库根的 plugins/（输出目录未拷贝插件时）
        // 向上找最多 3 层，命中含 JML.csproj 的目录即认为是仓库根
        string? dir = new DirectoryInfo(AppContext.BaseDirectory).Parent?.FullName;
        for (int i = 0; i < 3 && dir is not null; i++)
        {
            string candidate = Path.Combine(dir, "plugins");
            if (Directory.Exists(candidate) && File.Exists(Path.Combine(dir, "JML.csproj")))
            {
                return candidate;
            }
            dir = new DirectoryInfo(dir).Parent?.FullName;
        }

        return null;
    }

    /// <summary>
    /// 启动插件宿主：注册主程序服务 + 扫描并加载插件。
    /// 可重复调用（Python 侧幂等，二次调用返回上次结果）。
    /// </summary>
    public static BootstrapResultDto Bootstrap(string pluginsDir)
    {
        return PythonGateway.CallJson<BootstrapResultDto>(
            backend => (string)backend.bootstrap(pluginsDir));
    }

    /// <summary>
    /// 只扫描校验、不加载插件（供插件管理界面的“重新扫描”使用）。
    /// </summary>
    public static BootstrapResultDto ListPlugins(string pluginsDir)
    {
        return PythonGateway.CallJson<BootstrapResultDto>(
            backend => (string)backend.list_plugins(pluginsDir));
    }
}
