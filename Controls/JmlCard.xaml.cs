using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Media.Animation;

namespace JML.Controls;

/// <summary>
/// PCL2 风格卡片（无边框、圆角 5、紧阴影、40 高标题行）。
///
/// 参数来源：docs/20261017_PCL2界面视觉规格_v1.0.md C 节 / E 节；视觉在 Controls\JmlCard.xaml 的
/// 隐式样式模板里。
///
/// 悬停动效（docs/20261017_PCL2动效规格_v1.0.md 7.1、13.3-7）：
///   PCL2 MyCard.vb:181-185 悬停 90ms 线性：标题 Foreground ColorBrush1→ColorBrush2，
///   自绘阴影 MainChrome.Opacity 0.07→0.4。这里：
///   1. 标题色：动画标题自己的 SolidColorBrush.Color（不碰共享主题画刷）；
///   2. 阴影：动画独立阴影图层 CardShadowHover 的元素 Opacity（模板里 DropShadowEffect
///      的 Opacity 是静态的 0.4，绝不在动画里改），常态 0.07 由 CardShadow 的紧阴影提供，
///      本层补 0.33 使两层合计 ≈0.40，静态观感与验收时的 0.07 一致。
/// 本类不含任何业务判断。
///
/// 说明：不用 UserControl 自带 XAML，是因为 UserControl 的 XAML 会建立独立名称作用域，
/// 页面里写在卡片内的 x:Name 会在编译期报 MC3093；ContentControl + 隐式样式模板
/// 既保留 Header/Content 两段结构，也不影响页面控件的 x:Name。
/// </summary>
public class JmlCard : ContentControl
{
    /// <summary>PCL2 阴影常态不透明度（MyCard.vb:175-176）；由模板里 CardShadow 的紧阴影提供。</summary>
    public const double ShadowIdleOpacity = 0.07;

    /// <summary>PCL2 阴影悬停不透明度（MyCard.vb:185）。</summary>
    public const double ShadowHoverOpacity = 0.4;

    /// <summary>卡片悬停动效时长（MyCard.vb:181-185，90ms 线性）。</summary>
    public const int HoverDurationMs = 90;

    private TextBlock? _headerText;
    private Border? _shadowHover;
    private SolidColorBrush? _headerBrush;

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

    /// <summary>拿到模板部件：标题文字与悬停阴影层，并把标题前景换成可动画的本地画刷。</summary>
    public override void OnApplyTemplate()
    {
        base.OnApplyTemplate();

        _headerText = GetTemplateChild("HeaderText") as TextBlock;
        _shadowHover = GetTemplateChild("CardShadowHover") as Border;

        if (_headerText is not null)
        {
            // 主题画刷是共享资源（可能已冻结），不能直接做 Color 动画；换成同色本地画刷。
            _headerBrush = new SolidColorBrush(ThemeColor("ColorBrush1", Colors.Black));
            _headerText.Foreground = _headerBrush;
        }

        ApplyHover(false, animate: false);
    }

    protected override void OnMouseEnter(MouseEventArgs e)
    {
        base.OnMouseEnter(e);
        ApplyHover(true, animate: true);
    }

    protected override void OnMouseLeave(MouseEventArgs e)
    {
        base.OnMouseLeave(e);
        ApplyHover(false, animate: true);
    }

    /// <summary>
    /// 应用/撤销悬停状态。可重入：每次先读当前动画值、清掉旧动画、从当前值起步，
    /// 快速进出不会残留半透明或错误颜色（规格 13.2-10）。
    /// </summary>
    internal void ApplyHover(bool hover, bool animate)
    {
        if (_headerBrush is not null)
        {
            Color target = ThemeColor(hover ? "ColorBrush2" : "ColorBrush1", Colors.Black);
            Color current = _headerBrush.Color;
            _headerBrush.BeginAnimation(SolidColorBrush.ColorProperty, null);
            _headerBrush.Color = current;

            if (animate)
            {
                _headerBrush.BeginAnimation(
                    SolidColorBrush.ColorProperty,
                    new ColorAnimation
                    {
                        To = target,
                        Duration = TimeSpan.FromMilliseconds(HoverDurationMs),
                    });
            }
            else
            {
                _headerBrush.Color = target;
            }
        }

        if (_shadowHover is not null)
        {
            // 0.07 的基础阴影已由 CardShadow 提供，本层只补差值 → 两层合计 0.40。
            double target = hover ? ShadowHoverOpacity - ShadowIdleOpacity : 0d;
            double current = _shadowHover.Opacity;
            _shadowHover.BeginAnimation(UIElement.OpacityProperty, null);
            _shadowHover.Opacity = 0d;

            if (animate)
            {
                _shadowHover.BeginAnimation(
                    UIElement.OpacityProperty,
                    new DoubleAnimation
                    {
                        From = current,
                        To = target,
                        Duration = TimeSpan.FromMilliseconds(HoverDurationMs),
                    });
            }
            else
            {
                _shadowHover.Opacity = target;
            }
        }
    }

    private Color ThemeColor(string key, Color fallback) =>
        TryFindResource(key) is SolidColorBrush brush ? brush.Color : fallback;
}
