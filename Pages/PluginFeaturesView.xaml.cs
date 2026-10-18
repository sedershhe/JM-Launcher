using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using JML.Bridge;
using JML.Bridge.Dto;
using JML.Infrastructure;

namespace JML.Pages;

/// <summary>
/// 插件功能视图（阶段 4）：列出已加载且未禁用插件；双击进入该插件的功能页。
///
/// 多页面处理（契约《插件结构化页面协议》§12.4）：
/// - 插件只声明 1 个页面 → 直接触发 <see cref="FeatureRequested"/>（pages[0]）；
/// - 声明多个页面 → 先在本视图内显示页面选择列表，选中后再触发；
/// - 未声明页面 → 状态行提示，不进入内页。
///
/// 本视图不做业务判断：页面清单与模板类型全部来自 Python bootstrap 返回值。
/// </summary>
public partial class PluginFeaturesView : UserControl
{
    /// <summary>一个可进入的插件功能页（列表项模型，MainWindow 用它 PushInner）。</summary>
    public sealed record FeatureItem(
        string PluginId, string PageId, string PageTitle, string PluginName);

    /// <summary>用户选中某个功能页时触发（由宿主负责 PushInner）。</summary>
    public event EventHandler<FeatureItem>? FeatureRequested;

    /// <summary>插件列表项：一个已启用插件 + 它的全部页面。</summary>
    private sealed record PluginEntry(
        string PluginId,
        string PluginName,
        IReadOnlyList<PluginPageDto> Pages)
    {
        public string Summary =>
            Pages.Count == 0
                ? "未声明功能页面"
                : $"{Pages.Count} 个功能页：{string.Join("、", Pages.Select(page => page.Title))}";
    }

    private readonly List<PluginEntry> _entries = [];
    private PluginEntry? _pageChooserEntry;
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

    /// <summary>当前插件列表快照（测试/诊断用）。</summary>
    internal IReadOnlyList<string> PluginIds =>
        _entries.Select(entry => entry.PluginId).ToList();

    /// <summary>加载已启用插件（每个插件一行）。</summary>
    private void LoadFeatures()
    {
        string? pluginsDir = PluginBridge.LocatePluginsDirectory();
        if (pluginsDir is null)
        {
            _entries.Clear();
            FeatureList.ItemsSource = Array.Empty<PluginEntry>();
            TextStatus.Text = "未找到插件目录（plugins/）。";
            return;
        }

        TextStatus.Text = "正在读取插件…";
        List<string> disabled = AppSettings.Load().DisabledPlugins.ToList();

        Task.Run(() => PluginBridge.Bootstrap(pluginsDir))
            .ContinueWith(t =>
            {
                if (t.IsFaulted)
                {
                    string reason = t.Exception?.GetBaseException().Message ?? "未知错误";
                    _entries.Clear();
                    FeatureList.ItemsSource = Array.Empty<PluginEntry>();
                    TextStatus.Text = "读取插件失败：" + LogSanitizer.Sanitize(reason);
                    return;
                }

                _entries.Clear();
                foreach (PluginInfoDto plugin in t.Result.Loaded)
                {
                    if (disabled.Contains(plugin.Id))
                    {
                        continue;
                    }

                    _entries.Add(new PluginEntry(plugin.Id, plugin.Name, plugin.Pages));
                }

                FeatureList.ItemsSource = _entries.ToList();
                TextStatus.Text = _entries.Count > 0
                    ? $"共 {_entries.Count} 个已启用插件（双击进入功能页）。"
                    : "暂无可用的插件功能页。";
            }, TaskScheduler.FromCurrentSynchronizationContext());
    }

    private void FeatureList_MouseDoubleClick(object sender, MouseButtonEventArgs e)
    {
        if (FeatureList.SelectedItem is not PluginEntry entry)
        {
            return;
        }

        if (entry.Pages.Count == 0)
        {
            TextStatus.Text = $"插件「{entry.PluginName}」未声明功能页面。";
            return;
        }

        if (entry.Pages.Count == 1)
        {
            EnterPage(entry, entry.Pages[0]);
            return;
        }

        ShowPageChooser(entry);
    }

    /// <summary>多页面插件：先显示页面选择列表（不进入内页）。</summary>
    private void ShowPageChooser(PluginEntry entry)
    {
        _pageChooserEntry = entry;
        PageList.ItemsSource = entry.Pages.ToList();
        TextPagesTitle.Text = $"选择「{entry.PluginName}」的功能页（共 {entry.Pages.Count} 个）";
        PanelPlugins.Visibility = Visibility.Collapsed;
        PanelPages.Visibility = Visibility.Visible;
        TextStatus.Text = "双击列表中的页面进入。";
    }

    private void PageList_MouseDoubleClick(object sender, MouseButtonEventArgs e)
    {
        if (_pageChooserEntry is null || PageList.SelectedItem is not PluginPageDto page)
        {
            return;
        }

        EnterPage(_pageChooserEntry, page);
    }

    private void BtnPagesBack_Click(object sender, RoutedEventArgs e)
    {
        _pageChooserEntry = null;
        PageList.ItemsSource = null;
        PanelPages.Visibility = Visibility.Collapsed;
        PanelPlugins.Visibility = Visibility.Visible;
        TextStatus.Text = _entries.Count > 0
            ? $"共 {_entries.Count} 个已启用插件（双击进入功能页）。"
            : "暂无可用的插件功能页。";
    }

    private void EnterPage(PluginEntry entry, PluginPageDto page)
    {
        FeatureRequested?.Invoke(
            this,
            new FeatureItem(entry.PluginId, page.Id, page.Title, entry.PluginName));
    }
}
