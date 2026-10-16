namespace JML.Infrastructure;

/// <summary>
/// Python 环境检测结果。
/// 由于本项目要求"Python 由用户自行安装"，程序必须能明确告诉用户：
/// 找到了什么、缺什么、该怎么装。
/// </summary>
public sealed class PythonEnvironmentReport
{
    /// <summary>整体是否可用（Python 版本合规且依赖库齐全）</summary>
    public bool IsReady { get; set; }

    /// <summary>找到的 Python DLL 完整路径；未找到为 null</summary>
    public string? PythonDllPath { get; set; }

    /// <summary>Python 版本，例如 "3.13.7"；未能获取为 null</summary>
    public string? PythonVersion { get; set; }

    /// <summary>已装且可用的依赖库（名称 → 版本）</summary>
    public Dictionary<string, string> AvailablePackages { get; } = new();

    /// <summary>缺失或导入失败的依赖库名称</summary>
    public List<string> MissingPackages { get; } = new();

    /// <summary>问题描述（面向用户，中文）</summary>
    public List<string> Problems { get; } = new();

    /// <summary>修复指引（面向用户，中文）</summary>
    public List<string> Instructions { get; } = new();

    /// <summary>
    /// 生成一段可直接展示给用户的完整报告文本。
    /// 注意：本方法不输出任何凭据信息。
    /// </summary>
    public string ToUserText()
    {
        var lines = new List<string>();

        lines.Add(IsReady ? "Python 环境检查：通过" : "Python 环境检查：未通过");
        lines.Add("");

        if (PythonVersion is not null)
        {
            lines.Add($"Python 版本：{PythonVersion}");
        }

        if (AvailablePackages.Count > 0)
        {
            lines.Add("已安装依赖：" + string.Join("、",
                AvailablePackages.Select(kv => $"{kv.Key} {kv.Value}")));
        }

        if (MissingPackages.Count > 0)
        {
            lines.Add("缺失依赖：" + string.Join("、", MissingPackages));
        }

        if (Problems.Count > 0)
        {
            lines.Add("");
            lines.Add("问题：");
            foreach (string p in Problems)
            {
                lines.Add("  · " + p);
            }
        }

        if (Instructions.Count > 0)
        {
            lines.Add("");
            lines.Add("处理办法：");
            foreach (string i in Instructions)
            {
                lines.Add("  · " + i);
            }
        }

        return string.Join(Environment.NewLine, lines);
    }
}
