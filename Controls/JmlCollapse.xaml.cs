using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Media.Animation;
using System.Windows.Shapes;

namespace JML.Controls;

/// <summary>
/// PCL2 风格折叠面板（强化搜索插件阶段 3）。
///
/// 结构：34 高标题行（Header + 可选的右侧 Summary + 折叠箭头）+ 可折叠内容区。
/// 标题行整行可点，<see cref="IsExpanded"/> 默认 false。
///
/// 动效参数（PCL2 短促缓动，不弹跳）：
///   内容高度 150ms（QuadraticEase / EaseOut），收起动画结束后才把内容设为
///   Collapsed，避免“先消失再收高”的闪烁；
///   箭头旋转 250ms（QuadraticEase / EaseOut），收起 0°（v）/ 展开 180°（^）。
///   快速连续点击时先读当前高度/角度再清动画，从当前值续播，不跳变。
///
/// 视觉全部在 Controls\JmlCollapse.xaml 的隐式样式模板里；本类不含业务逻辑。
/// 与 JmlCard 一样使用 ContentControl + ResourceDictionary，避免 UserControl
/// 独立名称作用域导致页面内 x:Name 编译报 MC3093。
/// </summary>
public class JmlCollapse : ContentControl
{
    /// <summary>内容展开 / 收起动画时长（毫秒）。</summary>
    public const int ExpandDurationMs = 150;

    /// <summary>箭头旋转动画时长（毫秒）。</summary>
    public const int ArrowDurationMs = 250;

    private Border? _contentHost;
    private Path? _arrow;
    private RotateTransform? _arrowRotate;

    /// <summary>折叠头标题。</summary>
    public static readonly DependencyProperty HeaderProperty = DependencyProperty.Register(
        nameof(Header),
        typeof(string),
        typeof(JmlCollapse),
        new PropertyMetadata(string.Empty));

    /// <summary>折叠头右侧摘要（空串 = 不显示）。</summary>
    public static readonly DependencyProperty SummaryProperty = DependencyProperty.Register(
        nameof(Summary),
        typeof(string),
        typeof(JmlCollapse),
        new PropertyMetadata(string.Empty));

    /// <summary>是否展开；默认 false。</summary>
    public static readonly DependencyProperty IsExpandedProperty = DependencyProperty.Register(
        nameof(IsExpanded),
        typeof(bool),
        typeof(JmlCollapse),
        new FrameworkPropertyMetadata(
            false,
            FrameworkPropertyMetadataOptions.BindsTwoWayByDefault,
            OnIsExpandedChanged));

    static JmlCollapse()
    {
        // 折叠面板自身不参与页面布局之外的默认样式（模板在 JmlCollapse.xaml）。
    }

    /// <summary>折叠头标题。</summary>
    public string Header
    {
        get => (string)GetValue(HeaderProperty);
        set => SetValue(HeaderProperty, value);
    }

    /// <summary>折叠头右侧摘要。</summary>
    public string Summary
    {
        get => (string)GetValue(SummaryProperty);
        set => SetValue(SummaryProperty, value);
    }

    /// <summary>是否展开。</summary>
    public bool IsExpanded
    {
        get => (bool)GetValue(IsExpandedProperty);
        set => SetValue(IsExpandedProperty, value);
    }

    /// <summary>拿到模板部件并同步一次状态（模板应用前设置的 IsExpanded 会在这里补上）。</summary>
    public override void OnApplyTemplate()
    {
        base.OnApplyTemplate();

        _contentHost = GetTemplateChild("ContentHost") as Border;
        _arrow = GetTemplateChild("Arrow") as Path;
        _arrowRotate = _arrow?.RenderTransform as RotateTransform;

        ApplyExpanded(IsExpanded, animate: false);
    }

    private static void OnIsExpandedChanged(
        DependencyObject d,
        DependencyPropertyChangedEventArgs e)
    {
        // 模板尚未应用时由 OnApplyTemplate 补状态；这里只驱动动画。
        if (d is JmlCollapse collapse)
        {
            collapse.ApplyExpanded((bool)e.NewValue, animate: true);
        }
    }

    private void ApplyExpanded(bool expanded, bool animate)
    {
        ApplyArrow(expanded, animate);
        ApplyContent(expanded, animate);
    }

    private void ApplyArrow(bool expanded, bool animate)
    {
        if (_arrowRotate is null)
        {
            return;
        }

        double target = expanded ? 180d : 0d;
        double current = _arrowRotate.Angle;
        _arrowRotate.BeginAnimation(RotateTransform.AngleProperty, null);
        _arrowRotate.Angle = current;

        if (!animate)
        {
            _arrowRotate.Angle = target;
            return;
        }

        _arrowRotate.BeginAnimation(
            RotateTransform.AngleProperty,
            new DoubleAnimation
            {
                From = current,
                To = target,
                Duration = TimeSpan.FromMilliseconds(ArrowDurationMs),
                EasingFunction = new QuadraticEase { EasingMode = EasingMode.EaseOut },
            });
    }

    private void ApplyContent(bool expanded, bool animate)
    {
        if (_contentHost is null)
        {
            return;
        }

        if (expanded)
        {
            Expand(animate);
        }
        else
        {
            Collapse(animate);
        }
    }

    private void Expand(bool animate)
    {
        Border host = _contentHost!;

        // 先恢复可见并让内容自然测量高度，再从 0 补间到实际高度。
        host.BeginAnimation(HeightProperty, null);
        host.Visibility = Visibility.Visible;
        host.Height = double.NaN;
        host.UpdateLayout();

        double target = host.ActualHeight;
        if (!animate || target <= 0)
        {
            host.Height = double.NaN;
            return;
        }

        host.Height = 0d;
        var animation = new DoubleAnimation
        {
            From = 0d,
            To = target,
            Duration = TimeSpan.FromMilliseconds(ExpandDurationMs),
            EasingFunction = new QuadraticEase { EasingMode = EasingMode.EaseOut },
        };
        animation.Completed += (_, _) =>
        {
            // 动画结束后交还给布局，内容变化（例如筛选行增减）仍能自适应。
            host.BeginAnimation(HeightProperty, null);
            host.Height = double.NaN;
        };
        host.BeginAnimation(HeightProperty, animation);
    }

    private void Collapse(bool animate)
    {
        Border host = _contentHost!;

        double from = host.ActualHeight;
        host.BeginAnimation(HeightProperty, null);

        if (!animate || from <= 0d)
        {
            host.Height = 0d;
            host.Visibility = Visibility.Collapsed;
            return;
        }

        host.Height = from;
        var animation = new DoubleAnimation
        {
            From = from,
            To = 0d,
            Duration = TimeSpan.FromMilliseconds(ExpandDurationMs),
            EasingFunction = new QuadraticEase { EasingMode = EasingMode.EaseOut },
        };
        animation.Completed += (_, _) =>
        {
            // 收起动画结束才隐藏：过程中内容一直被父 Border 的 ClipToBounds 裁切。
            host.BeginAnimation(HeightProperty, null);
            host.Height = 0d;
            host.Visibility = Visibility.Collapsed;
        };
        host.BeginAnimation(HeightProperty, animation);
    }
}
