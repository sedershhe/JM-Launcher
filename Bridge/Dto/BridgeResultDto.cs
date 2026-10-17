using System.Text.Json.Serialization;

namespace JML.Bridge.Dto;

/// <summary>
/// 桥接 DTO 的公共失败形态。
///
/// 契约 §一：成功为 <c>{"ok": true, ...}</c>，失败为
/// <c>{"ok": false, "error_code": "...", "error_message": "..."}</c>。
/// 所有返回 DTO 统一继承本类，避免每个 DTO 重复声明三个字段。
///
/// 注意：<see cref="ErrorCode"/> / <see cref="ErrorMessage"/> 使用 set 访问器，
/// 便于桥接层在反序列化后统一做凭据脱敏（不改变 Python 的业务判断）。
/// </summary>
public record BridgeResultDto
{
    /// <summary>是否成功。C# 只读这个值，不做成功/失败的业务推断。</summary>
    [JsonPropertyName("ok")]
    public bool Ok { get; init; }

    /// <summary>错误码（成功时为空串）。取值见契约 §1.1。</summary>
    [JsonPropertyName("error_code")]
    public string ErrorCode { get; set; } = "";

    /// <summary>中文错误原因（成功时为空串；必须直接展示，不再二次分类）。</summary>
    [JsonPropertyName("error_message")]
    public string ErrorMessage { get; set; } = "";
}
