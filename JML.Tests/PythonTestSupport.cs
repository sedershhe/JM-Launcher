using JML.Bridge;
using JML.Bridge.Dto;
using JML.Infrastructure;
using Python.Runtime;
using Xunit;

namespace JML.Tests;

/// <summary>Python 真调用测试的公共工具（不联网，只碰本地后端）。</summary>
internal static class PythonTestSupport
{
    /// <summary>调用后端 emit_test_event（要求 payload 含 type，且与事件类型一致）。</summary>
    public static string EmitTestEvent(string eventType, Dictionary<string, string> payload)
    {
        return PythonRuntime.Invoke(() =>
        {
            dynamic backend = PythonGateway.EnsureBackend();

            // 必须显式构造 PyDict：dynamic 调用不会把 C# Dictionary 自动转成 Python dict，
            // 事件协议校验要求 payload 是 dict（禁止裸 CLR 对象）。
            using var pyPayload = new PyDict();
            foreach ((string key, string value) in payload)
            {
                pyPayload.SetItem(key, new PyString(value));
            }

            return (string)backend.emit_test_event(eventType, pyPayload);
        });
    }

    /// <summary>构造符合协议校验的测试 payload。</summary>
    public static Dictionary<string, string> Payload(string eventType, params (string Key, string Value)[] fields)
    {
        var payload = new Dictionary<string, string> { ["type"] = eventType };
        foreach ((string key, string value) in fields)
        {
            payload[key] = value;
        }

        return payload;
    }
}
