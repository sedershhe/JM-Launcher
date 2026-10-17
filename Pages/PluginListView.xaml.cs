using System.Windows;
using System.Windows.Controls;
using JML.Bridge;
using JML.Bridge.Dto;
using JML.Infrastructure;

namespace JML.Pages;

/// <summary>
/// 插件管理视图（阶段 1）。
///
/// 数据来源：PluginBridge.Bootstrap（首次进入自动加载插件宿主），
/// 结果渲染为三类行：已加载 / 未启用（AppSettings.DisabledPlugins）/ 加载失败。
///
/// 约束：
/// 1. 本类不做任何业务判断，插件可用性结论全部来自 Python 返回值；
/// 2. 所有桥接调用用 Task.Run 包装，结果经 TaskScheduler 回 UI 线程；
/// 3. 启用/停用按钮为阶段 3 占位（需 Python 侧配置文件服务支持），当前禁用。
/// </summary>
public partial class PluginListView : UserControl
{
    /// <summary>管理视图的一行（合并已加载/失败/禁用三类的统一行模型）。</summary>
    private sealed record PluginRow(string Name, string Version, string Status, string Description);

    private bool _loadedOnce;

    public PluginListView()
    {
        InitializeComponent();
        Loaded += (_, _) =>
        {
            if (!_loadedOnce)
            {
                _loadedOnce = true;
                RefreshPlugins(useBootstrap: true);
            }
        };
    }

    /// <summary>重新扫描按钮：走 list_plugins（只扫描不加载）。</summary>
    private void BtnRescan_Click(object sender, RoutedEventArgs e)
    {
        RefreshPlugins(useBootstrap: false);
    }

    /// <summary>
    /// 刷新插件列表。
    /// useBootstrap=true：调用 Bootstrap（首次加载插件宿主）；
    /// useBootstrap=false：调用 ListPlugins（重新扫描目录）。
    /// </summary>
    private void RefreshPlugins(bool useBootstrap)
    {
        string? pluginsDir = PluginBridge.LocatePluginsDirectory();
        if (pluginsDir is null)
        {
            GridPlugins.ItemsSource = Array.Empty<PluginRow>();
            TextStatus.Text = "未找到插件目录（plugins/）。请将插件放入程序目录下的 plugins 文件夹后点击「重新扫描」。";
            return;
        }

        BtnRescan.IsEnabled = false;
        TextStatus.Text = useBootstrap ? "正在加载插件宿主…" : "正在重新扫描插件…";

        // 禁用列表快照（后台线程只读副本，避免跨线程共享可变设置对象）
        List<string> disabled = AppSettings.Load().DisabledPlugins.ToList();

        Task.Run(() => useBootstrap
                ? PluginBridge.Bootstrap(pluginsDir)
                : PluginBridge.ListPlugins(pluginsDir))
            .ContinueWith(t =>
            {
                if (t.IsFaulted)
                {
                    string reason = t.Exception?.GetBaseException().Message ?? "未知错误";
                    GridPlugins.ItemsSource = Array.Empty<PluginRow>();
                    TextStatus.Text = "插件操作失败：" + LogSanitizer.Sanitize(reason);
                }
                else
                {
                    Render(t.Result, disabled);
                }
                BtnRescan.IsEnabled = true;
            }, TaskScheduler.FromCurrentSynchronizationContext());
    }

    /// <summary>把后端结果渲染为表格行（已加载 / 未启用 / 加载失败）。</summary>
    private void Render(BootstrapResultDto result, List<string> disabled)
    {
        var rows = new List<PluginRow>();

        foreach (PluginInfoDto p in result.Loaded)
        {
            bool isDisabled = disabled.Contains(p.Id);
            string status = isDisabled
                ? "未启用"
                : (p.IsDevelopmentSample ? "已加载（示例）" : "已加载");
            rows.Add(new PluginRow(p.Name, p.Version, status, p.Description));
        }

        foreach (LoadErrorDto f in result.Failed)
        {
            rows.Add(new PluginRow(f.Id, "-", "加载失败", f.Reason));
        }

        GridPlugins.ItemsSource = rows;
        TextStatus.Text = $"已加载 {result.Loaded.Count} 个插件，失败 {result.Failed.Count} 个。";
    }
}
