using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json;
using JML.Bridge;
using JML.Bridge.Dto;

namespace JML.Infrastructure;

/// <summary>
/// 无界面自检（阶段 5）：<c>JML.exe --selfcheck</c>。
///
/// 职责：只做环境诊断并把人类可读结果写进日志/控制台，
/// **不创建主窗口、不启动下载、不发起网络请求**。
///
/// 检查项（顺序固定，便于脚本断言）：
/// 1. Python 运行时（版本 / DLL 路径）：<see cref="PythonRuntime.Inspect"/>；
/// 2. Python 依赖库：复用 Inspect 的实测结果 + 后端版本冒烟；
/// 3. 插件扫描：目录存在、manifest 可扫描、无坏 manifest；
/// 4. 插件装配：bootstrap 的 loaded / failed / 停用；
/// 5. 服务注册表快照：jm.search/detail/download/account/session + archive_reader；
/// 6. 配置契约：插件设置 schema 可解析 + 主设置 settings.json 可解析。
///
/// 退出码：全部 OK = 0，任一项 FAIL = 1。
///
/// 设计要点：
/// - <see cref="Run"/> 是纯收集（不写盘），xUnit 可直接调用断言；
/// - <see cref="RunAndWrite"/> 负责控制台输出与日志落盘，供 App 调用；
/// - WinExe 默认没有控制台：先尝试 AttachConsole 到父进程（cmd/dotnet run 可见），
///   同时把完整文本写入 <c>%LOCALAPPDATA%\JML\logs\selfcheck-&lt;时间戳&gt;.log</c>。
/// </summary>
public static class SelfCheck
{
    /// <summary>命令行开关（大小写不敏感）。</summary>
    public const string CommandArgument = "--selfcheck";

    /// <summary>主程序服务必须齐备的项（版本均为 1）。</summary>
    private static readonly (string Name, int Version)[] RequiredHostServices =
    [
        ("jm.search", 1),
        ("jm.detail", 1),
        ("jm.download", 1),
        ("jm.account", 1),
        ("jm.session", 1),
    ];

    /// <summary>插件提供的唯一必需服务（zip_browser 启用时）。</summary>
    private const string PluginProvidedService = "archive_reader";

    private const int AttachParentProcess = -1;

    private static readonly HashSet<string> AllowedSettingTypes = new(StringComparer.Ordinal)
    {
        "text", "password", "number", "bool", "select", "path",
    };

    /// <summary>参数里是否带 --selfcheck。</summary>
    public static bool IsRequested(IEnumerable<string>? args)
    {
        return args is not null
            && args.Any(arg => string.Equals(arg, CommandArgument, StringComparison.OrdinalIgnoreCase));
    }

    /// <summary>
    /// 收集自检结果（不写日志、不输出控制台）。测试与 App 共用同一份逻辑。
    /// </summary>
    /// <param name="pluginsDirectory">
    /// 插件目录；null 表示按生产规则自动定位（输出目录 plugins/ → 仓库根 plugins/）。
    /// 测试从 JML.Tests 的输出目录启动，自动定位到不了仓库根，因此显式传入。
    /// </param>
    public static SelfCheckReport Run(string? pluginsDirectory = null)
    {
        List<SelfCheckItem> items = [];

        // 1) Python 运行时 + 2) 依赖：Inspect 内部已逐项实测，失败只返回报告不抛异常。
        PythonEnvironmentReport? environment = null;
        try
        {
            environment = PythonRuntime.Inspect();
        }
        catch (Exception ex)
        {
            environment = null;
            items.Add(Item("Python 运行时", false, Brief(ex)));
        }

        if (environment is not null)
        {
            bool runtimeOk =
                !string.IsNullOrWhiteSpace(environment.PythonDllPath)
                && !string.IsNullOrWhiteSpace(environment.PythonVersion);
            string runtimeDetail = runtimeOk
                ? $"Python {environment.PythonVersion}（64 位；DLL：{environment.PythonDllPath}）"
                : JoinReasons(environment.Problems, "未检测到 Python 运行时");
            items.Add(Item("Python 运行时", runtimeOk, runtimeDetail));

            items.Add(BuildDependencyItem(environment));
        }
        else
        {
            items.Add(Item(
                "Python 依赖",
                false,
                "Python 运行时不可用，无法检查依赖库。"));
        }

        // 3/4/5) 插件扫描、装配、服务注册表：共用一次定位结果。
        string? pluginsDir = string.IsNullOrWhiteSpace(pluginsDirectory)
            ? PluginBridge.LocatePluginsDirectory()
            : pluginsDirectory;

        int manifestCount = 0;
        bool directoryOk = !string.IsNullOrWhiteSpace(pluginsDir) && Directory.Exists(pluginsDir);
        if (directoryOk)
        {
            try
            {
                manifestCount = Directory
                    .EnumerateDirectories(pluginsDir!)
                    .Count(dir => File.Exists(Path.Combine(dir, "plugin.json")));
            }
            catch
            {
                manifestCount = 0;
            }
        }

        BootstrapResultDto? scan = null;
        if (!directoryOk)
        {
            items.Add(Item("插件扫描", false, "未找到插件目录（plugins/）。"));
        }
        else if (manifestCount == 0)
        {
            items.Add(Item("插件扫描", false, $"目录存在但没有 plugin.json：{pluginsDir}"));
        }
        else
        {
            try
            {
                scan = PluginBridge.ListPlugins(pluginsDir!);
                List<LoadErrorDto> unexpected = scan.Failed
                    .Where(failure => !IsExpectedScanFailure(failure))
                    .ToList();
                bool ok = scan.Ok && unexpected.Count == 0;
                string detail =
                    $"{pluginsDir}（{manifestCount} 个插件目录；"
                    + $"扫描可用 {scan.Loaded.Count}、停用/依赖阻断 {scan.Failed.Count - unexpected.Count}"
                    + (unexpected.Count > 0 ? $"、坏插件 {unexpected.Count}" : "")
                    + "）";

                List<string> extra = scan.Loaded
                    .Select(plugin => $"{plugin.Id} {plugin.Version}")
                    .ToList();
                extra.AddRange(unexpected.Select(failure =>
                    $"失败：{failure.Id}：{failure.Reason}"));

                items.Add(Item("插件扫描", ok, detail, extra));
            }
            catch (Exception ex)
            {
                items.Add(Item("插件扫描", false, Brief(ex)));
            }
        }

        BootstrapResultDto? bootstrap = null;
        if (!directoryOk || manifestCount == 0)
        {
            items.Add(Item("插件装配", false, "插件目录不可用，跳过 bootstrap。"));
        }
        else
        {
            try
            {
                bootstrap = PluginBridge.Bootstrap(pluginsDir!);
                List<LoadErrorDto> disabled = bootstrap.Failed
                    .Where(failure => !failure.Enabled)
                    .ToList();
                List<LoadErrorDto> hardFailures = bootstrap.Failed
                    .Where(failure => failure.Enabled && !IsDependencyBlock(failure))
                    .ToList();
                bool allDisabled = manifestCount > 0 && disabled.Count >= manifestCount;
                bool ok = bootstrap.Ok
                    && hardFailures.Count == 0
                    && (bootstrap.Loaded.Count > 0 || allDisabled);

                string detail =
                    $"已加载 {bootstrap.Loaded.Count}、停用 {disabled.Count}、失败 {hardFailures.Count}"
                    + (bootstrap.Ok ? "" : $"（{bootstrap.ErrorCode}: {bootstrap.ErrorMessage}）");

                List<string> extra = bootstrap.Loaded
                    .Select(plugin => $"已加载：{plugin.Id} {plugin.Version}（{plugin.Name}）")
                    .ToList();
                extra.AddRange(disabled.Select(item => $"停用：{item.Id}（{item.Reason}）"));
                extra.AddRange(hardFailures.Select(item => $"失败：{item.Id}（{item.Reason}）"));

                items.Add(Item("插件装配", ok, detail, extra));
            }
            catch (Exception ex)
            {
                items.Add(Item("插件装配", false, Brief(ex)));
            }
        }

        items.Add(BuildServiceItem(bootstrap));

        // 6) 配置契约
        items.Add(BuildConfigItem());

        bool allPassed = items.Count > 0 && items.All(item => item.Ok);
        string text = Compose(items, allPassed);
        return new SelfCheckReport(allPassed, allPassed ? 0 : 1, text, items);
    }

    /// <summary>
    /// 生产入口：跑自检 → 写日志 → 尝试输出控制台 → 返回退出码。
    /// App.OnStartup 拿到退出码后调用 <c>Shutdown(code)</c>。
    /// </summary>
    public static int RunAndWrite(string? pluginsDirectory = null)
    {
        TryAttachParentConsole();

        SelfCheckReport report;
        try
        {
            report = Run(pluginsDirectory);
        }
        catch (Exception ex)
        {
            // 理论上 Run 内部逐项收口，这里只做最后防线：自检本身绝不把进程带崩。
            report = new SelfCheckReport(
                false,
                1,
                $"JML 自检未能完成：{Brief(ex)}",
                [Item("自检执行", false, Brief(ex))]);
        }

        string stamp = DateTime.Now.ToString("yyyyMMdd-HHmmss-fff", CultureInfo.InvariantCulture);
        string? logPath = WriteLog(report.Text, stamp, out string logError);
        string output = report.Text + Environment.NewLine;
        output += logPath is not null
            ? "日志：" + logPath
            : "日志：写入失败（" + AppPaths.LogDirectory + "）：" + logError;

        TryWriteConsole(output);
        return report.ExitCode;
    }

    // ------------------------------------------------------------------
    // 单项检查
    // ------------------------------------------------------------------

    private static SelfCheckItem BuildDependencyItem(PythonEnvironmentReport environment)
    {
        List<string> extra = environment.AvailablePackages
            .Select(pair => $"{pair.Key} {pair.Value}")
            .ToList();

        string backendVersion = "";
        string backendFailure = "";
        try
        {
            backendVersion = PythonRuntime.Invoke(() =>
            {
                dynamic backend = PythonGateway.EnsureBackend();
                return (string)backend.version();
            });
        }
        catch (Exception ex)
        {
            backendFailure = Brief(ex);
        }

        bool ok = environment.MissingPackages.Count == 0
            && string.IsNullOrEmpty(backendFailure);

        int totalPackages =
            environment.AvailablePackages.Count + environment.MissingPackages.Count;
        if (ok)
        {
            return Item(
                "Python 依赖",
                true,
                $"依赖 {environment.AvailablePackages.Count}/{totalPackages} 可用；"
                + $"后端 {backendVersion}",
                extra);
        }

        List<string> reasons = [];
        if (environment.MissingPackages.Count > 0)
        {
            reasons.Add("缺失：" + string.Join("、", environment.MissingPackages));
        }

        if (!string.IsNullOrEmpty(backendFailure))
        {
            reasons.Add("后端导入失败：" + backendFailure);
        }

        return Item("Python 依赖", false, string.Join("；", reasons), extra);
    }

    private static SelfCheckItem BuildServiceItem(BootstrapResultDto? bootstrap)
    {
        if (bootstrap is null)
        {
            return Item("服务注册表", false, "插件装配未成功，无法取得服务快照。");
        }

        Dictionary<string, int> services = bootstrap.Services;
        List<string> missing = RequiredHostServices
            .Where(required =>
                !services.TryGetValue(required.Name, out int version) || version != required.Version)
            .Select(required => $"{required.Name}/{required.Version}")
            .ToList();

        bool pluginDisabled = bootstrap.Failed.Any(
            failure => failure.Id == "zip_browser" && !failure.Enabled);
        bool pluginServicePresent =
            services.TryGetValue(PluginProvidedService, out int pluginServiceVersion)
            && pluginServiceVersion == 1;

        bool ok = bootstrap.Ok && missing.Count == 0
            && (pluginServicePresent || pluginDisabled);

        string detail;
        if (ok)
        {
            string pluginNote = pluginServicePresent
                ? $"{PluginProvidedService}/1"
                : $"{PluginProvidedService}/1 未注册（zip_browser 已停用）";
            detail = $"快照 {services.Count} 项：{string.Join("、", services.Select(pair => $"{pair.Key}/{pair.Value}"))}"
                + "；" + pluginNote;
        }
        else
        {
            List<string> reasons = [];
            if (!bootstrap.Ok)
            {
                reasons.Add($"bootstrap 失败（{bootstrap.ErrorCode}: {bootstrap.ErrorMessage}）");
            }

            if (missing.Count > 0)
            {
                reasons.Add("缺少主程序服务：" + string.Join("、", missing));
            }

            if (!pluginServicePresent && !pluginDisabled)
            {
                reasons.Add($"缺少 {PluginProvidedService}/1");
            }

            detail = string.Join("；", reasons);
        }

        List<string> extra = services
            .OrderBy(pair => pair.Key, StringComparer.Ordinal)
            .Select(pair => $"{pair.Key}/{pair.Value}")
            .ToList();
        return Item("服务注册表", ok, detail, extra);
    }

    private static SelfCheckItem BuildConfigItem()
    {
        List<string> problems = [];
        List<string> extra = [];
        int groupCount = 0;
        int fieldCount = 0;

        try
        {
            PluginSettingsDto settings = PythonGateway.CallJson<PluginSettingsDto>(
                backend => (string)backend.list_plugin_settings());
            if (!settings.Ok)
            {
                problems.Add($"插件设置读取失败（{settings.ErrorCode}: {settings.ErrorMessage}）");
            }
            else
            {
                groupCount = settings.Plugins.Count;
                foreach (PluginSettingsGroupDto group in settings.Plugins)
                {
                    if (string.IsNullOrWhiteSpace(group.PluginId))
                    {
                        problems.Add("存在 plugin_id 为空的设置分组");
                    }

                    foreach (PluginSettingFieldDto field in group.Fields)
                    {
                        fieldCount++;
                        if (string.IsNullOrWhiteSpace(field.Key))
                        {
                            problems.Add($"插件 {group.PluginId} 存在 key 为空的设置项");
                        }

                        if (!AllowedSettingTypes.Contains(field.Type))
                        {
                            problems.Add(
                                $"插件 {group.PluginId} 的设置项 {field.Key} 类型非法：{field.Type}");
                        }
                    }

                    extra.Add($"{group.PluginId}：{group.Fields.Count} 个设置项");
                }
            }
        }
        catch (Exception ex)
        {
            problems.Add("插件设置 schema 解析失败：" + Brief(ex));
        }

        // 主设置文件没有独立 schema，这里做最小契约校验：必须是 JSON 对象，
        // 已知字段类型正确；文件不存在视为首次运行。
        string settingsPath = Path.Combine(AppPaths.ConfigDirectory, "settings.json");
        if (File.Exists(settingsPath))
        {
            try
            {
                using JsonDocument document = JsonDocument.Parse(File.ReadAllText(settingsPath));
                ValidateMainSettings(document.RootElement);
                extra.Add("主设置 settings.json：可解析");
            }
            catch (Exception ex)
            {
                problems.Add("主设置 settings.json 解析失败：" + Brief(ex));
            }
        }
        else
        {
            extra.Add("主设置 settings.json：不存在（首次运行，使用默认值）");
        }

        bool ok = problems.Count == 0;
        string detail = ok
            ? $"插件设置 {groupCount} 组 / {fieldCount} 字段可解析；主设置契约通过"
            : string.Join("；", problems);
        return Item("配置契约", ok, detail, extra);
    }

    /// <summary>主设置最小契约：顶层对象，已知字段类型正确。</summary>
    private static void ValidateMainSettings(JsonElement root)
    {
        if (root.ValueKind != JsonValueKind.Object)
        {
            throw new InvalidDataException("顶层必须是 JSON 对象");
        }

        foreach (JsonProperty property in root.EnumerateObject())
        {
            if (string.Equals(property.Name, "DisabledPlugins", StringComparison.OrdinalIgnoreCase))
            {
                if (property.Value.ValueKind != JsonValueKind.Array)
                {
                    throw new InvalidDataException("DisabledPlugins 必须是字符串数组");
                }

                foreach (JsonElement item in property.Value.EnumerateArray())
                {
                    if (item.ValueKind != JsonValueKind.String)
                    {
                        throw new InvalidDataException("DisabledPlugins 只能是字符串数组");
                    }
                }
            }
            else if (string.Equals(property.Name, "DownloadDir", StringComparison.OrdinalIgnoreCase))
            {
                if (property.Value.ValueKind != JsonValueKind.String)
                {
                    throw new InvalidDataException("DownloadDir 必须是字符串");
                }
            }
        }
    }

    // ------------------------------------------------------------------
    // 输出 / 落盘
    // ------------------------------------------------------------------

    private static string Compose(IReadOnlyList<SelfCheckItem> items, bool allPassed)
    {
        StringBuilder builder = new();
        string version = typeof(SelfCheck).Assembly.GetName().Version?.ToString() ?? "未知";
        builder.AppendLine("JML 自检 v" + version);
        builder.AppendLine("时间：" + DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss", CultureInfo.InvariantCulture));
        builder.AppendLine("程序目录：" + AppContext.BaseDirectory);
        builder.AppendLine("环境：" + RuntimeInformation.OSDescription
            + " / .NET " + Environment.Version
            + " / " + RuntimeInformation.ProcessArchitecture);
        builder.AppendLine(new string('-', 60));

        foreach (SelfCheckItem item in items)
        {
            builder.Append('[').Append(item.Ok ? "OK" : "FAIL").Append("] ")
                .Append(item.Name).Append('：')
                .AppendLine(item.Detail);
            foreach (string line in item.ExtraLines)
            {
                builder.Append("      · ").AppendLine(line);
            }
        }

        builder.AppendLine(new string('-', 60));
        builder.AppendLine(
            "自检结果：" + (allPassed ? "全部通过" : "存在失败项")
            + $"（{items.Count(item => item.Ok)}/{items.Count}）");
        builder.AppendLine("退出码：" + (allPassed ? "0" : "1"));
        return builder.ToString().TrimEnd();
    }

    private static SelfCheckItem Item(
        string name,
        bool ok,
        string detail,
        IEnumerable<string>? extraLines = null)
    {
        return new SelfCheckItem(
            name,
            ok,
            OneLine(detail),
            (extraLines ?? []).Select(OneLine).ToList());
    }

    private static string OneLine(string? text)
    {
        if (string.IsNullOrWhiteSpace(text))
        {
            return "";
        }

        string flat = text
            .Replace("\r\n", " ", StringComparison.Ordinal)
            .Replace('\r', ' ')
            .Replace('\n', ' ')
            .Trim();
        return LogSanitizer.Sanitize(flat);
    }

    private static string Brief(Exception exception)
    {
        return OneLine($"{exception.GetType().Name}: {exception.Message}");
    }

    private static string JoinReasons(IReadOnlyList<string> reasons, string fallback)
    {
        return reasons.Count > 0 ? string.Join("；", reasons) : fallback;
    }

    /// <summary>扫描阶段允许出现的"失败"：停用 / 依赖被停用或未安装（不是坏插件）。</summary>
    private static bool IsExpectedScanFailure(LoadErrorDto failure)
    {
        return !failure.Enabled
            || failure.Reason.StartsWith("缺少依赖插件", StringComparison.Ordinal)
            || string.Equals(failure.Reason, "插件已停用", StringComparison.Ordinal);
    }

    /// <summary>装配阶段允许出现的"失败"：依赖阻断（根因失败会单独计入 hardFailures）。</summary>
    private static bool IsDependencyBlock(LoadErrorDto failure)
    {
        return failure.Reason.StartsWith("缺少依赖插件", StringComparison.Ordinal);
    }

    private static string? WriteLog(string text, string stamp, out string error)
    {
        error = "";
        try
        {
            Directory.CreateDirectory(AppPaths.LogDirectory);
            string path = Path.Combine(AppPaths.LogDirectory, $"selfcheck-{stamp}.log");
            File.WriteAllText(path, text + Environment.NewLine, new UTF8Encoding(false));
            return path;
        }
        catch (Exception ex)
        {
            // 日志不可写不影响退出码（控制台仍会给出完整文本），但要报告具体原因。
            error = OneLine($"{ex.GetType().Name}: {ex.Message}");
            return null;
        }
    }

    private static void TryAttachParentConsole()
    {
        try
        {
            // WinExe 没有控制台；命令行启动时附到父进程控制台，Console.WriteLine 才可见。
            // 已有控制台/无父进程时失败是正常的，忽略。
            AttachConsole(AttachParentProcess);
        }
        catch
        {
            // 非 Windows / 受限环境：只走日志。
        }
    }

    private static void TryWriteConsole(string text)
    {
        try
        {
            Console.OutputEncoding = new UTF8Encoding(false);
        }
        catch
        {
            // 无控制台时设置编码可能抛异常，忽略。
        }

        try
        {
            Console.WriteLine(text);
            Console.Out.Flush();
        }
        catch
        {
            // 控制台不可用：完整文本已落日志。
        }
    }

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool AttachConsole(int dwProcessId);
}

/// <summary>单项自检结果。</summary>
public sealed record SelfCheckItem(
    string Name,
    bool Ok,
    string Detail,
    IReadOnlyList<string> ExtraLines);

/// <summary>一次自检的完整结果（文本 + 退出码 + 单项列表）。</summary>
public sealed record SelfCheckReport(
    bool AllPassed,
    int ExitCode,
    string Text,
    IReadOnlyList<SelfCheckItem> Items);
