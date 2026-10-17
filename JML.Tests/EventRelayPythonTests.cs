using JML.Bridge;
using JML.Bridge.Dto;
using Xunit;

namespace JML.Tests;

/// <summary>
/// 真实 Python 事件出口测试（不联网）：
/// set_event_sink 注册后，回调必须只入队、立刻返回；解析在 UI 线程排空时才发生。
/// </summary>
[Collection(PythonBackendCollection.Name)]
public class EventRelayPythonTests
{
    private sealed class RecordingDispatcher
    {
        public List<Action> Scheduled { get; } = [];

        public void Dispatch(Action action)
        {
            lock (Scheduled)
            {
                Scheduled.Add(action);
            }
        }

        public void RunAll()
        {
            while (true)
            {
                Action? next;
                lock (Scheduled)
                {
                    if (Scheduled.Count == 0)
                    {
                        return;
                    }

                    next = Scheduled[0];
                    Scheduled.RemoveAt(0);
                }

                next();
            }
        }
    }

    [Fact]
    public void SetEventSink_ReturnsBeforeUiDrain_AndCallbackOnlyEnqueues()
    {
        var dispatcher = new RecordingDispatcher();
        using var relay = new EventRelay(dispatcher.Dispatch);
        PluginNotificationPayload? received = null;
        relay.PluginNotificationReceived += payload => received = payload;

        relay.Start();
        try
        {
            Assert.True(relay.IsStarted);

            // Python 侧同步派发：若 C# sink 在回调里解析/调 Python，这里会先出事件或阻塞。
            string response = PythonTestSupport.EmitTestEvent(
                PythonEventTypes.PluginNotification,
                PythonTestSupport.Payload(
                    PythonEventTypes.PluginNotification,
                    ("level", "info"),
                    ("title", "t"),
                    ("message", "m")));

            Assert.Contains("\"ok\": true", response);
            Assert.Equal(1, relay.PendingCount);
            Assert.Single(dispatcher.Scheduled);
            Assert.Null(received); // UI 没排空前不得解析/抛事件

            dispatcher.RunAll();

            Assert.Equal(0, relay.PendingCount);
            Assert.NotNull(received);
            Assert.Equal("t", received!.Title);
        }
        finally
        {
            relay.Stop();
        }
    }
}
