using System.Windows.Controls;

namespace JML.Pages;

/// <summary>
/// 插件功能页占位（阶段 1）。
/// 通过 SetContent 传入插件页标题；阶段 4 由真正的模板渲染页替换。
/// </summary>
public partial class PluginPagePlaceholder : UserControl
{
    public PluginPagePlaceholder()
    {
        InitializeComponent();
    }

    /// <summary>设置占位页展示的插件页标题。</summary>
    public void SetContent(string title)
    {
        TextTitle.Text = title;
    }
}
