using System.Windows.Controls;

namespace JML.Pages;

/// <summary>
/// 插件页主体（阶段 1）：左侧栏（插件管理/插件功能）+ 右侧内容视图切换。
/// 功能页的双击进入通过 FeatureRequested 事件转发给 MainWindow（由它 PushInner）。
/// </summary>
public partial class PluginHubPage : UserControl
{
    /// <summary>用户请求进入某个插件功能页（由 MainWindow 订阅并 PushInner）。</summary>
    public event EventHandler<PluginFeaturesView.FeatureItem>? FeatureRequested;

    private readonly PluginListView _manageView;
    private readonly PluginFeaturesView _featuresView;

    public PluginHubPage()
    {
        InitializeComponent();

        _manageView = new PluginListView();
        _featuresView = new PluginFeaturesView();
        _featuresView.FeatureRequested += (_, item) => FeatureRequested?.Invoke(this, item);

        ContentArea.Content = _manageView;
    }

    /// <summary>左侧栏选中切换（两个 RadioButton 共用，按发送者区分）。</summary>
    private void Tab_Checked(object sender, System.Windows.RoutedEventArgs e)
    {
        if (!IsLoaded)
        {
            return;
        }

        ContentArea.Content = ReferenceEquals(sender, TabFeatures) ? _featuresView : _manageView;
    }
}
