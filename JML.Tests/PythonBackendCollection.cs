using Xunit;

namespace JML.Tests;

/// <summary>
/// 所有会触碰 Python 全局状态（事件出口、搜索会话、bootstrap 缓存）的测试
/// 放在同一串行集合里，避免互相抢占。
/// </summary>
[CollectionDefinition("PythonBackend", DisableParallelization = true)]
public sealed class PythonBackendCollection
{
    public const string Name = "PythonBackend";
}
