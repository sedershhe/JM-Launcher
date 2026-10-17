using System.Text.Json.Serialization;

namespace JML.Bridge.Dto;

// 契约 §十：账号与登录态 DTO。
// 字段名与 Python 返回值逐字对齐（snake_case），C# 只展示，不重新判定登录成功与否。

/// <summary>
/// 所有 <c>account_*</c> 入口共用的状态结构（契约 §10.1）。
/// 失败时 Python 仍会给出 <see cref="LoggedIn"/> 与打码用户名，界面可直接展示。
/// </summary>
public record AccountStatusDto : BridgeResultDto
{
    /// <summary>当前是否已登录（以 Python 返回值为准）。</summary>
    [JsonPropertyName("logged_in")]
    public bool LoggedIn { get; init; }

    /// <summary>打码用户名（如 <c>ab***cd</c>）；未配置账号时为空串。</summary>
    [JsonPropertyName("username_masked")]
    public string UsernameMasked { get; init; } = "";

    /// <summary><c>account_logout</c> 额外字段：是否仍保留内存凭据。</summary>
    [JsonPropertyName("credentials_kept")]
    public bool? CredentialsKept { get; init; }

    /// <summary><c>account_clear</c> 额外字段：是否已清空凭据。</summary>
    [JsonPropertyName("credentials_cleared")]
    public bool? CredentialsCleared { get; init; }
}
