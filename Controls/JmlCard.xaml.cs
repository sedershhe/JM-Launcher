using System.Windows;
using System.Windows.Controls;

namespace JML.Controls;

/// <summary>
/// PCL2 风格卡片（无边框、圆角 5、紧阴影、40 高标题行）。
///
/// 参数来源：docs/20261017_PCL2界面视觉规格_v1.0.md C 节 / E 节；视觉在 Controls\JmlCard.xaml 的
/// 隐式样式模板里，本类只负责：
/// 1. 暴露 <see cref="Header"/>（卡片标题）；
/// 2. 把内容区默认内边距设为 PCL2 的 20,40,18,15（PageDownloadInstall.xaml:31）；
/// 3. Content 沿用 <see cref="ContentControl"/> 的 Content，使用方直接把内容写在标签里。
/// 本类不含任何业务判断。
///
/// 说明：不用 UserControl 自带 XAML，是因为 UserControl 的 XAML 会建立独立名称作用域，
/// 页面里写在卡片内的 x:Name 会在编译期报 MC3093；ContentControl + 隐式样式模板
/// 既保留 Header/Content 两段结构，也不影响页面控件的 x:Name。
/// </summary>
public class JmlCard : ContentControl
{
    /// <summary>卡片标题（显示在 40 高标题行内）。</summary>
    public static readonly DependencyProperty HeaderProperty = DependencyProperty.Register(
        nameof(Header),
        typeof(string),
        typeof(JmlCard),
        new PropertyMetadata(string.Empty));

    static JmlCard()
    {
        // PCL2 下载页卡片内容内边距：20,40,18,15（上 40 让开标题行）。
        PaddingProperty.OverrideMetadata(
            typeof(JmlCard),
            new FrameworkPropertyMetadata(new Thickness(20, 40, 18, 15)));
    }

    /// <summary>卡片标题；为空时标题行不显示文字（内容内边距仍按使用方设置）。</summary>
    public string Header
    {
        get => (string)GetValue(HeaderProperty);
        set => SetValue(HeaderProperty, value);
    }
}
