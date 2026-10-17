using System.Runtime.CompilerServices;

// xUnit 需要直接验证桥接层内部回调入队语义（不经过 Python 也能注入事件 JSON）。
// 放在 Bridge/ 下，避免改动仓库根 AssemblyInfo.cs。
[assembly: InternalsVisibleTo("JML.Tests")]
