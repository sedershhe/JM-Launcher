using System;
using System.Collections.Generic;
using System.IO;
using System.Net;
using System.Net.Http;
using System.Text.Json;
using System.Threading.Tasks;
using JML.Bridge;
using JML.Bridge.Dto;
using JML.Infrastructure;
using Python.Runtime;
using Xunit;

namespace JML.Tests;

/// <summary>
/// ZIP 网页浏览（zip_web）真机链路端到端测试：真实 Python 装配 + 真实本机 HTTP。
///
/// 与 Python 侧单测的区别：这里走的是**生产完全相同的路径**
/// （pythonnet 嵌入 → <see cref="PluginPageBridge"/> → bootstrap 仓库 plugins/），
/// 因此能验证“Python 后台线程在 pythonnet 宿主里能否真的服务浏览器请求”这一
/// 单测覆盖不到的点，也能验证 C# 动作后重载页面不会把刚启动的服务弄死。
///
/// 全部离线：只连 127.0.0.1；不启动 JML.exe；不读系统代理。
/// </summary>
[Collection(PythonBackendCollection.Name)]
public class ZipWebBrowserIntegrationTests
{
    private const string PluginId = "zip_web";
    private const string PageId = "zip_web.main";

    [Fact]
    public async Task Start_Reload_RepeatStart_ThenStop_KeepsUrlReachableAllAlong()
    {
        string pluginsDir = FindRepoPluginsDirectory();
        string fixtureDir = Path.Combine(Path.GetDirectoryName(pluginsDir)!, "Draw", "zip-fixture");
        if (!Directory.Exists(fixtureDir))
        {
            return; // 缺少离线测试数据，跳过（不误报失败）
        }

        PythonRuntime.Initialize();
        string url = string.Empty;
        try
        {
            BootstrapResultDto report = PluginBridge.Bootstrap(pluginsDir);
            Assert.True(report.Ok, report.ErrorMessage);
            Assert.Empty(report.Failed);

            // 1) 选目录 → 状态 idle、列出 1.zip
            PluginPageLoadResultDto loaded = await PluginPageBridge.LoadAsync(
                PluginId,
                PageId,
                new Dictionary<string, object?> { ["folder"] = fixtureDir });
            Assert.True(loaded.Ok, loaded.ErrorMessage);
            Assert.Equal("idle", loaded.Data.GetProperty("state").GetString());

            string zipPath = Path.Combine(fixtureDir, "1.zip");
            var item = new Dictionary<string, object?>
            {
                ["name"] = "1.zip",
                ["path"] = zipPath,
            };

            // 2) start：返回可连的 url + refresh
            PluginPageActionResultDto started = await PluginPageBridge.ActionAsync(
                PluginId,
                PageId,
                "start",
                new Dictionary<string, object?> { ["item"] = item, ["item_index"] = 0 });
            Assert.True(started.Ok, started.ErrorMessage);
            Assert.True(started.Refresh);
            Assert.NotNull(started.Data);
            url = started.Data!.Value.GetProperty("url").GetString() ?? "";
            Assert.StartsWith("http://127.0.0.1:", url, StringComparison.Ordinal);

            using HttpClient client = CreateLoopbackClient();

            // 3) 浏览器真的能取到长页与图片（= 用户“手动粘贴 url”的动作）
            await AssertPageAndImageAsync(client, url);

            // 4) 模拟 C# 在 refresh=true 后的整页重载（带同样的 folder 参数）
            PluginPageLoadResultDto refreshed = await PluginPageBridge.LoadAsync(
                PluginId,
                PageId,
                new Dictionary<string, object?> { ["folder"] = fixtureDir });
            Assert.True(refreshed.Ok, refreshed.ErrorMessage);
            Assert.Equal("running", refreshed.Data.GetProperty("state").GetString());
            Assert.Equal(url, refreshed.Data.GetProperty("url").GetString());

            // 5) 重载之后服务必须仍然可用（旧缺陷：这里服务被停掉，用户粘端口打不开）
            await AssertPageAndImageAsync(client, url);

            // 6) 同一个包再次 start：复用同一端口（不把刚展示的地址停掉）
            PluginPageActionResultDto again = await PluginPageBridge.ActionAsync(
                PluginId,
                PageId,
                "start",
                new Dictionary<string, object?> { ["item"] = item, ["item_index"] = 0 });
            Assert.True(again.Ok, again.ErrorMessage);
            Assert.True(again.Refresh);
            Assert.NotNull(again.Data);
            Assert.Equal(url, again.Data!.Value.GetProperty("url").GetString());
            await AssertPageAndImageAsync(client, url);

            // 7) stop 后端口必须关闭
            PluginPageActionResultDto stopped = await PluginPageBridge.ActionAsync(
                PluginId,
                PageId,
                "stop",
                new Dictionary<string, object?>());
            Assert.True(stopped.Ok, stopped.ErrorMessage);
            Assert.True(stopped.Refresh);
            await AssertPortClosedAsync(url);
        }
        finally
        {
            // 断言中途失败也不把本机端口留给后续用例
            if (url.Length > 0)
            {
                try
                {
                    await PluginPageBridge.ActionAsync(
                        PluginId,
                        PageId,
                        "stop",
                        new Dictionary<string, object?>());
                }
                catch
                {
                    // 清理失败不掩盖原始断言
                }
            }

            PythonRuntime.Invoke(() =>
            {
                dynamic backend = PythonGateway.EnsureBackend();
                backend.reset_host_state();
            });
        }
    }

    /// <summary>长页 HTML（第 1 页 / 共 3 页）+ 首图原图字节，两种图片路由都要通。</summary>
    private static async Task AssertPageAndImageAsync(HttpClient client, string url)
    {
        using (HttpResponseMessage page = await client.GetAsync(url))
        {
            Assert.Equal(HttpStatusCode.OK, page.StatusCode);
            string html = await page.Content.ReadAsStringAsync();
            Assert.Contains("第 1 页 / 共 3 页", html, StringComparison.Ordinal);
            // 原版路由 /page/<序号>；/img/<序号> 是兼容别名
            Assert.Contains("src=\"/page/0\"", html, StringComparison.Ordinal);
        }

        foreach (string route in new[] { "page/0", "img/0" })
        {
            using HttpResponseMessage image = await client.GetAsync(url + route);
            Assert.Equal(HttpStatusCode.OK, image.StatusCode);
            Assert.Equal("image/png", image.Content.Headers.ContentType?.MediaType);
            byte[] bytes = await image.Content.ReadAsByteArrayAsync();
            Assert.True(bytes.Length > 0);
        }
    }

    /// <summary>不读系统代理：只连本机回环。</summary>
    private static HttpClient CreateLoopbackClient()
    {
        var handler = new HttpClientHandler
        {
            UseProxy = false,
            AllowAutoRedirect = false,
        };
        return new HttpClient(handler) { Timeout = TimeSpan.FromSeconds(5) };
    }

    private static async Task AssertPortClosedAsync(string url)
    {
        using HttpClient client = CreateLoopbackClient();
        await Assert.ThrowsAnyAsync<Exception>(async () => await client.GetAsync(url));
    }

    private static string FindRepoPluginsDirectory()
    {
        string? dir = AppContext.BaseDirectory;
        while (!string.IsNullOrEmpty(dir))
        {
            if (File.Exists(Path.Combine(dir, "JML.csproj")))
            {
                return Path.Combine(dir, "plugins");
            }

            dir = Directory.GetParent(dir)?.FullName;
        }

        throw new InvalidOperationException("找不到仓库根（JML.csproj），无法定位 plugins/。");
    }
}
