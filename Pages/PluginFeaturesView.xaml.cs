using System.Windows.Controls;
using System.Windows.Input;
using JML.Bridge;
using JML.Bridge.Dto;
using JML.Infrastructure;

namespace JML.Pages;

/// <summary>
/// 插件功能视图（阶段 1）。
/// 列出已加载且未禁用插件声明的页面；选中后由宿主（PluginHubPage）PushInner 进占位页。
/// 所有桥接调用用 Task.Run 包装，结果回 UI 线程。
/// </summary>
public partial class PluginFeaturesView : UserControl
{
    /// <summary>一个可进入的插件功能页（列表项模型）。</summary>
    public sealed record FeatureItem(
        string PluginId, string PageId, string PageTitle, string PluginName);

    /// <summary>用户双击/确认进入某个功能页时触发（由宿主负责 PushInner）。</summary>
    public event EventHandler<FeatureItem>? FeatureRequested;

    private bool _loadedOnce;

    public PluginFeaturesView()
    {
        InitializeComponent();
        Loaded += (_, _) =>
        {
            if (!_loadedOnce)
            {
                _loadedOnce = true;
                LoadFeatures();
            }
        };
    }

    /// <summary>加载已启用插件的功能页列表。</summary>
    private void LoadFeatures()
    {
        string? pluginsDir = PluginBridge.LocatePluginsDirectory();
        if (pluginsDir is null)
        {
            FeatureList.ItemsSource = Array.Empty<FeatureItem>();
            TextStatus.Text = "未找到插件目录（plugins/）。";
            return;
        }

        TextStatus.Text = "正在读取插件功能…";
        List<string> disabled = AppSettings.Load().DisabledPlugins.ToList();

        Task.Run(() => PluginBridge.Bootstrap(pluginsDir))
            .ContinueWith(t =>
            {
                if (t.IsFaulted)
                {
                    string reason = t.Exception?.GetBaseException().Message ?? "未知错误";
                    FeatureList.ItemsSource = Array.Empty<FeatureItem>();
                    TextStatus.Text = "读取插件失败：" + LogSanitizer.Sanitize(reason);
                    return;
                }

                var items = new List<FeatureItem>();
                foreach (PluginInfoDto plugin in t.Result.Loaded)
                {
                    if (disabled.Contains(plugin.Id))
                    {
                        continue;
                    }

                    foreach (PluginPageDto page in plugin.Pages)
                    {
                        items.Add(new FeatureItem(plugin.Id, page.Id, page.Title, plugin.Name));
                    }
                }

                FeatureList.ItemsSource = items;
                TextStatus.Text = items.Count > 0
                    ? $"共 {items.Count} 个功能入口（双击进入）。"
                    : "暂无已启用插件提供的功能页面。";
            }, TaskScheduler.FromCurrentSynchronizationContext());
    }

    private void FeatureList_MouseDoubleClick(object sender, MouseButtonEventArgs e)
    {
        if (FeatureList.SelectedItem is FeatureItem item)
        {
            FeatureRequested?.Invoke(this, item);
        }
    }
}
