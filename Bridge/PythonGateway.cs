using System.IO;
using System.Text.Json;
using JML.Bridge.Dto;
using JML.Infrastructure;
using Python.Runtime;

namespace JML.Bridge;

/// <summary>
/// C# → Python 的统一调用入口。
///
/// 职责边界（契约 §一、§九）：
/// 1. 管理 jml_backend 的导入路径（进程内幂等）；
/// 2. 所有 Python 调用在 GIL 内执行（<see cref="PythonRuntime.Invoke{T}"/>）；
/// 3. 把 Python 返回的 JSON 字符串反序列化成强类型 DTO，绝不把 dynamic 漏到 UI；
/// 4. 只做传输层兜底（JSON 失败、凭据脱敏），不做成功/失败、文件名、截断等业务判断。
///
/// 唯一例外：<c>fetch_cover</c> 返回原始 bytes，走 <see cref="CallRaw{T}"/>。
/// </summary>
public static class PythonGateway
{
    private static readonly object ImportGate = new();
    private static bool _imported;

    /// <summary>JSON 选项：snake_case 显式映射 + 大小写不敏感（契约 §一）。</summary>
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNameCaseInsensitive = true,
    };

    /// <summary>
    /// 导入 jml_backend 模块（进程内幂等）。调用方必须已持有 GIL。
    /// 所有桥接类共用这一份实现，不允许各自复制 sys.path 拼装逻辑。
    /// </summary>
    internal static dynamic EnsureBackend()
    {
        lock (ImportGate)
        {
            if (!_imported)
            {
                // Python 后端随输出目录分发；不硬编码任何开发机绝对路径。
                string pythonDir = Path.Combine(AppContext.BaseDirectory, "Python");
                dynamic sys = Py.Import("sys");
                dynamic path = sys.path;
                if (!path.__contains__(pythonDir))
                {
                    path.append(pythonDir);
                }

                _imported = true;
            }
        }

        return Py.Import("jml_backend");
    }

    /// <summary>
    /// 执行一次“返回 JSON 字符串”的后端调用并反序列化为 DTO。
    /// 失败（Python 初始化失败、JSON 非法等）会抛异常，由各 Bridge 方法收口成 DTO 失败形态。
    /// </summary>
    public static T CallJson<T>(Func<dynamic, string> call) where T : class
    {
        return PythonRuntime.Invoke(() =>
        {
            dynamic backend = EnsureBackend();
            string json = call(backend);
            if (string.IsNullOrWhiteSpace(json))
            {
                throw new InvalidOperationException("Python 后端返回了空 JSON。");
            }

            T? dto = JsonSerializer.Deserialize<T>(json, JsonOptions);
            if (dto is null)
            {
                throw new InvalidOperationException("Python 后端返回的 JSON 无法解析。");
            }

            // 安全红线：错误文案展示前再做一次凭据脱敏（Python 侧已脱敏，这里是防线叠加）。
            SanitizeErrorFields(dto);
            return dto;
        });
    }

    /// <summary>
    /// 执行一次“返回原始对象”的后端调用（仅 <c>fetch_cover</c> 的 bytes 用）。
    /// 工作线程只产出字节数据，绝不创建 BitmapImage 等 UI 对象（旧缺陷 #7）。
    /// </summary>
    public static T? CallRaw<T>(Func<dynamic, object?> call) where T : class
    {
        return PythonRuntime.Invoke(() =>
        {
            dynamic backend = EnsureBackend();
            object? raw = call(backend);
            if (raw is null)
            {
                return null;
            }

            if (raw is T typed)
            {
                return typed;
            }

            if (raw is PyObject py)
            {
                using (py)
                {
                    if (py.IsNone())
                    {
                        return null;
                    }

                    return py.As<T>();
                }
            }

            throw new InvalidOperationException(
                $"Python 返回值类型 {raw.GetType().Name} 无法转换为 {typeof(T).Name}。");
        });
    }

    /// <summary>设置诊断落盘目录（传空串关闭）；目录由调用方提供，不在 C# 推导。</summary>
    public static SetLogDirResultDto SetLogDirectory(string path)
    {
        return CallJson<SetLogDirResultDto>(
            backend => (string)backend.set_log_dir(path ?? string.Empty));
    }

    /// <summary>读取已脱敏的诊断环表（limit &lt;= 0 表示全部）。</summary>
    public static DiagnosticsDto GetDiagnostics(int limit = 100)
    {
        return CallJson<DiagnosticsDto>(
            backend => (string)backend.get_diagnostics(limit));
    }

    /// <summary>
    /// 递归脱敏 DTO 里的错误文案。只处理桥接层已知的失败结构，
    /// 对业务字段（title/path/description 等）原样保留。
    /// </summary>
    private static void SanitizeErrorFields(object dto)
    {
        switch (dto)
        {
            case BridgeResultDto result:
                result.ErrorMessage = LogSanitizer.Sanitize(result.ErrorMessage);
                break;
        }

        // 批次状态里嵌套最终结果，最终结果里再嵌套单本条目。
        if (dto is BatchStatusDto status && status.Result is not null)
        {
            SanitizeErrorFields(status.Result);
        }

        if (dto is BatchResultDto batch)
        {
            foreach (DownloadItemDto item in batch.Items)
            {
                SanitizeErrorFields(item);
            }
        }
    }
}
