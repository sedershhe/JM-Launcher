using JML.Bridge.Dto;
using JML.Infrastructure;

namespace JML.Bridge;

/// <summary>
/// 桥接层传输失败收口（Python 未初始化、JSON 无法解析、GIL 初始化失败等）。
///
/// 注意：这里**不分类业务错误**；Python 已返回失败形态时原样透传。
/// 只有 C# 侧根本没拿到可解析返回值时才使用契约 §1.1 的兜底码 UNKNOWN_ERROR。
/// </summary>
internal static class BridgeFailures
{
    /// <summary>契约 §1.1 的兜底错误码。</summary>
    private const string FallbackErrorCode = "UNKNOWN_ERROR";

    /// <summary>生成一个失败 DTO（错误文案已脱敏）。</summary>
    public static T Create<T>(Exception ex) where T : BridgeResultDto, new()
    {
        return new T
        {
            Ok = false,
            ErrorCode = FallbackErrorCode,
            ErrorMessage = LogSanitizer.Sanitize(
                $"桥接层调用失败：{ex.GetType().Name}: {ex.Message}"),
        };
    }

    /// <summary>记录一次传输失败（日志先脱敏）。</summary>
    public static void Log(string operation, Exception ex)
    {
        LogSanitizer.Log($"{operation}失败：{ex.GetType().Name}: {ex.Message}");
    }
}
