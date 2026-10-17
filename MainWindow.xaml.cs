using System.Runtime.CompilerServices;
using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Media.Animation;
using JML.Navigation;
using JML.Pages;

namespace JML;

// =====================================================================
// 一、PCL2 缓动函数精确复刻（规格 12.3）
//     PCL2 的 AniEaseOutBack/InBack 与 WPF BackEase 形状不同，想 1:1 只能用自定义曲线。
//     注意：EasingMode 必须是 EaseIn —— WPF 的 EaseIn 模式会直接调用 EaseInCore，
//     而这里把完整的 PCL2 曲线写进 EaseInCore（规格 12.3 的用法说明）。
// =====================================================================

/// <summary>PCL2 AniEaseOutBack 的精确复刻：1-(1-t)^p*cos(1.5πt)。</summary>
public sealed class PclEaseOutBack : EasingFunctionBase
{
    /// <summary>2=Weak、1.5=Middle、1=Strong、0.5=ExtraStrong。</summary>
    public static readonly DependencyProperty PowerProperty = DependencyProperty.Register(
        nameof(Power), typeof(double), typeof(PclEaseOutBack), new PropertyMetadata(1.5));

    public double Power
    {
        get => (double)GetValue(PowerProperty);
        set => SetValue(PowerProperty, value);
    }

    protected override double EaseInCore(double t)
    {
        t = Math.Clamp(t, 0, 1);
        return 1 - Math.Pow(1 - t, Power) * Math.Cos(1.5 * Math.PI * t);
    }

    protected override Freezable CreateInstanceCore() => new PclEaseOutBack();
}

/// <summary>PCL2 AniEaseInBack 的精确复刻：t^p*cos(1.5π(1-t))。</summary>
public sealed class PclEaseInBack : EasingFunctionBase
{
    public static readonly DependencyProperty PowerProperty = DependencyProperty.Register(
        nameof(Power), typeof(double), typeof(PclEaseInBack), new PropertyMetadata(1.5));

    public double Power
    {
        get => (double)GetValue(PowerProperty);
        set => SetValue(PowerProperty, value);
    }

    protected override double EaseInCore(double t)
    {
        t = Math.Clamp(t, 0, 1);
        return Math.Pow(t, Power) * Math.Cos(1.5 * Math.PI * (1 - t));
    }

    protected override Freezable CreateInstanceCore() => new PclEaseInBack();
}

/// <summary>
/// PCL2 AniEaseOutFluentWithInitial 的精确复刻：alpha = v0*T/D-1，alpha≤0 时退化为线性。
/// 本轮卡片高度补间未做，保留该曲线供后续使用（规格 12.3）。
/// </summary>
public sealed class PclEaseOutFluentWithInitial : EasingFunctionBase
{
    public static readonly DependencyProperty AlphaProperty = DependencyProperty.Register(
        nameof(Alpha), typeof(double), typeof(PclEaseOutFluentWithInitial), new PropertyMetadata(0d));

    public double Alpha
    {
        get => (double)GetValue(AlphaProperty);
        set => SetValue(AlphaProperty, value);
    }

    protected override double EaseInCore(double t)
    {
        t = Math.Clamp(t, 0, 1);
        return Alpha <= 0 ? t : (Alpha + 1) * t / (1 + Alpha * t);
    }

    protected override Freezable CreateInstanceCore() => new PclEaseOutFluentWithInitial();
}

// =====================================================================
// 二、动效参数 + 同属性多段动画的 10ms 关键帧合成器（规格 12.4）
//     PCL2 是逐帧增量（同属性多条动画相加），WPF Storyboard 是覆盖；
//     所以“+5/250ms Fluent(3) 与 +11/350ms Back(1.5) 叠加”必须采样成一条关键帧动画。
// =====================================================================

/// <summary>合成分解出的一段增量动画：delta 在 durationMs 内按 ease 曲线加到起始值上。</summary>
public readonly record struct MotionPart(double Delta, int DurationMs, int DelayMs, IEasingFunction Ease);

/// <summary>
/// PCL2 动效参数与 WPF 时间轴构造（纯函数，便于单元测试直接断言时长/关键帧）。
/// 参数唯一来源：docs/20261017_PCL2动效规格_v1.0.md。
/// </summary>
public static class JmlMotion
{
    /// <summary>规格 12.4 推荐的采样间隔。</summary>
    public const int SampleIntervalMs = 10;

    // ---- 页面内容切换（规格 3.2 / 13.3-3） ----
    public const int ContentEnterOpacityMs = 100;
    public const int ContentEnterTranslateMs = 350;
    public const int ContentExitMs = 70;
    public const double ContentEnterOffset = -16;

    // ---- 内页标题栏切换（规格 4.1 / 4.2） ----
    public const int TitleOpacityMs = 150;
    public const int TitleDelayMs = 200;
    public const int TitleInnerXMs = 350;
    public const int TitleTotalMs = 550;

    // ---- 窗口入场（规格 1.1） ----
    public const int WindowEntryDelayMs = 100;
    public const int WindowEntryTranslateMs = 600;
    public const int WindowEntryRotateMs = 500;
    public const int WindowEntryOpacityMs = 250;

    /// <summary>TranslateTransform.Y 的 Storyboard 属性路径。</summary>
    public static readonly PropertyPath TranslateYPath =
        new("(UIElement.RenderTransform).(TranslateTransform.Y)");

    /// <summary>TranslateTransform.X 的 Storyboard 属性路径。</summary>
    public static readonly PropertyPath TranslateXPath =
        new("(UIElement.RenderTransform).(TranslateTransform.X)");

    /// <summary>PCL2 AniEaseOutBack(Power=…)，EasingMode 必须是 EaseIn（见类注释）。</summary>
    public static PclEaseOutBack EaseOutBack(double power) =>
        new() { Power = power, EasingMode = EasingMode.EaseIn };

    /// <summary>PCL2 AniEaseInBack(Power=…)。</summary>
    public static PclEaseInBack EaseInBack(double power) =>
        new() { Power = power, EasingMode = EasingMode.EaseIn };

    /// <summary>PCL2 AniEaseOutFluent(p) = 1-(1-t)^p。</summary>
    public static PowerEase EaseOutFluent(double power) =>
        new() { Power = power, EasingMode = EasingMode.EaseOut };

    /// <summary>PCL2 AniEaseInFluent(p) = t^p。</summary>
    public static PowerEase EaseInFluent(double power) =>
        new() { Power = power, EasingMode = EasingMode.EaseIn };

    /// <summary>
    /// 把同属性上的多段增量动画按 10ms 采样合并成一条关键帧动画（规格 12.4）。
    /// from 是起始值，每段 delta 从 delay 开始累加；总时长 = max(delay+duration)。
    /// </summary>
    public static DoubleAnimationUsingKeyFrames Combine(double from, params MotionPart[] parts)
    {
        int total = 0;
        foreach (MotionPart part in parts)
        {
            total = Math.Max(total, part.DelayMs + part.DurationMs);
        }

        var animation = new DoubleAnimationUsingKeyFrames
        {
            Duration = TimeSpan.FromMilliseconds(total),
            FillBehavior = FillBehavior.HoldEnd,
        };

        for (int t = 0; t < total; t += SampleIntervalMs)
        {
            animation.KeyFrames.Add(new LinearDoubleKeyFrame(
                ValueAt(from, parts, t),
                KeyTime.FromTimeSpan(TimeSpan.FromMilliseconds(t))));
        }

        // 总时长不一定是采样间隔的整数倍，补一个精确终点关键帧。
        animation.KeyFrames.Add(new LinearDoubleKeyFrame(
            ValueAt(from, parts, total),
            KeyTime.FromTimeSpan(TimeSpan.FromMilliseconds(total))));

        return animation;
    }

    private static double ValueAt(double from, MotionPart[] parts, int t)
    {
        double value = from;
        foreach (MotionPart part in parts)
        {
            double x = Math.Clamp((t - part.DelayMs) / (double)part.DurationMs, 0, 1);
            value += part.Delta * part.Ease.Ease(x);
        }

        return value;
    }

    // ---------------- 页面内容：新页入场 ----------------

    /// <summary>
    /// 新内容 TranslateY -16→0：+5/250ms EaseOutFluent(3) 与 +11/350ms EaseOutBack(1.5) 叠加，
    /// 采样合并（t≈150ms 过冲 +1.7、t≈200ms 过冲 +2.7，与规格 12.4 表一致）。
    /// </summary>
    public static DoubleAnimationUsingKeyFrames CreateContentEnterTranslate() =>
        Combine(
            ContentEnterOffset,
            new MotionPart(5, 250, 0, EaseOutFluent(3)),
            new MotionPart(11, 350, 0, EaseOutBack(1.5)));

    /// <summary>新内容 Opacity 0→1 / 100ms 线性。</summary>
    public static DoubleAnimation CreateContentEnterOpacity() =>
        new(0, 1, TimeSpan.FromMilliseconds(ContentEnterOpacityMs));

    /// <summary>旧内容整层 Opacity 1→0 / 70ms 线性（规格 3.2 的简化：不做逐控件退出）。</summary>
    public static DoubleAnimation CreateContentExitOpacity() =>
        new(1, 0, TimeSpan.FromMilliseconds(ContentExitMs));

    /// <summary>新内容入场 Storyboard（目标元素需已是 TranslateTransform）。</summary>
    public static Storyboard CreateContentEnterStoryboard()
    {
        var storyboard = new Storyboard();
        DoubleAnimation opacity = CreateContentEnterOpacity();
        Storyboard.SetTargetProperty(opacity, new PropertyPath(UIElement.OpacityProperty));

        DoubleAnimationUsingKeyFrames translate = CreateContentEnterTranslate();
        Storyboard.SetTargetProperty(translate, TranslateYPath);

        storyboard.Children.Add(opacity);
        storyboard.Children.Add(translate);
        return storyboard;
    }

    /// <summary>旧内容退场 Storyboard（70ms 淡出后由调用方换内容）。</summary>
    public static Storyboard CreateContentExitStoryboard()
    {
        var storyboard = new Storyboard();
        DoubleAnimation opacity = CreateContentExitOpacity();
        Storyboard.SetTargetProperty(opacity, new PropertyPath(UIElement.OpacityProperty));
        storyboard.Children.Add(opacity);
        return storyboard;
    }

    // ---------------- 内页标题栏：主层 ↔ 内层 ----------------

    /// <summary>
    /// 一级页 → 内页（规格 4.1）：
    ///   主层 1→0/150 线性 + X 0→12/150 EaseInFluent(2)；
    ///   内层 0→1/150（延迟 200）+ X -16→0/350（延迟 200）EaseOutBack(1.5)。
    /// 总 550ms。
    /// </summary>
    public static Storyboard CreateHeaderEnterStoryboard()
    {
        var storyboard = new Storyboard();
        storyboard.Children.Add(HeaderOpacity("MainHeader", 1, 0, TitleOpacityMs, 0, null));
        storyboard.Children.Add(HeaderTranslateX("MainHeader", 0, 12, TitleOpacityMs, 0, EaseInFluent(2)));
        storyboard.Children.Add(HeaderOpacity("InnerHeader", 0, 1, TitleOpacityMs, TitleDelayMs, null));
        storyboard.Children.Add(HeaderTranslateX(
            "InnerHeader", -16, 0, TitleInnerXMs, TitleDelayMs, EaseOutBack(1.5)));
        return storyboard;
    }

    /// <summary>
    /// 内页 → 一级页（规格 4.2，镜像）：
    ///   内层 1→0/150 线性 + X 0→-18/150 EaseInFluent(3)；
    ///   主层 0→1/150（延迟 200）+ X +12→0/350（延迟 200）EaseOutBack(2)。
    /// 总 550ms。
    /// </summary>
    public static Storyboard CreateHeaderReturnStoryboard()
    {
        var storyboard = new Storyboard();
        storyboard.Children.Add(HeaderOpacity("InnerHeader", 1, 0, TitleOpacityMs, 0, null));
        storyboard.Children.Add(HeaderTranslateX("InnerHeader", 0, -18, TitleOpacityMs, 0, EaseInFluent(3)));
        storyboard.Children.Add(HeaderOpacity("MainHeader", 0, 1, TitleOpacityMs, TitleDelayMs, null));
        storyboard.Children.Add(HeaderTranslateX(
            "MainHeader", 12, 0, TitleInnerXMs, TitleDelayMs, EaseOutBack(2)));
        return storyboard;
    }

    // ---------------- 窗口入场（规格 1.1） ----------------

    /// <summary>整体位移 Y 60→0 / 600ms / 延迟 100 / EaseOutBack(2)。</summary>
    public static DoubleAnimation CreateWindowEntryTranslate() =>
        new(60, 0, TimeSpan.FromMilliseconds(WindowEntryTranslateMs))
        {
            BeginTime = TimeSpan.FromMilliseconds(WindowEntryDelayMs),
            EasingFunction = EaseOutBack(2),
        };

    /// <summary>整体旋转 -4°→0° / 500ms / 延迟 100 / EaseOutBack(2)。</summary>
    public static DoubleAnimation CreateWindowEntryRotate() =>
        new(-4, 0, TimeSpan.FromMilliseconds(WindowEntryRotateMs))
        {
            BeginTime = TimeSpan.FromMilliseconds(WindowEntryDelayMs),
            EasingFunction = EaseOutBack(2),
        };

    /// <summary>Window.Opacity 0→1.0 / 250ms / 延迟 100 / 线性（不传 Ease）。</summary>
    public static DoubleAnimation CreateWindowEntryOpacity() =>
        new(0, 1, TimeSpan.FromMilliseconds(WindowEntryOpacityMs))
        {
            BeginTime = TimeSpan.FromMilliseconds(WindowEntryDelayMs),
        };

    // ---------------- 工具 ----------------

    /// <summary>时间轴的实际总时长（BeginTime + Duration；Storyboard 取所有子项最大值）。</summary>
    public static TimeSpan TotalDuration(Timeline timeline)
    {
        if (timeline is Storyboard storyboard)
        {
            double max = 0;
            foreach (Timeline child in storyboard.Children)
            {
                max = Math.Max(max, EndMilliseconds(child));
            }

            return TimeSpan.FromMilliseconds(max);
        }

        return TimeSpan.FromMilliseconds(EndMilliseconds(timeline));
    }

    private static double EndMilliseconds(Timeline timeline)
    {
        double begin = timeline.BeginTime?.TotalMilliseconds ?? 0;
        Duration duration = timeline.Duration;
        return begin + (duration.HasTimeSpan ? duration.TimeSpan.TotalMilliseconds : 0);
    }

    private static DoubleAnimation HeaderOpacity(
        string targetName, double from, double to, int durationMs, int delayMs, IEasingFunction? ease)
    {
        var animation = new DoubleAnimation(from, to, TimeSpan.FromMilliseconds(durationMs))
        {
            BeginTime = TimeSpan.FromMilliseconds(delayMs),
            EasingFunction = ease,
        };
        Storyboard.SetTargetName(animation, targetName);
        Storyboard.SetTargetProperty(animation, new PropertyPath(UIElement.OpacityProperty));
        return animation;
    }

    private static DoubleAnimation HeaderTranslateX(
        string targetName, double from, double to, int durationMs, int delayMs, IEasingFunction? ease)
    {
        var animation = new DoubleAnimation(from, to, TimeSpan.FromMilliseconds(durationMs))
        {
            BeginTime = TimeSpan.FromMilliseconds(delayMs),
            EasingFunction = ease,
        };
        Storyboard.SetTargetName(animation, targetName);
        Storyboard.SetTargetProperty(animation, TranslateXPath);
        return animation;
    }
}

// =====================================================================
// 三、页面内容切换：可停止 / 可重入的 Storyboard 封装（任务 2 与硬约束 2）
// =====================================================================

/// <summary>
/// 页面内容切换 helper：一级页面切换与插件内页进入/返回共用。
/// 新内容：Opacity 0→1/100ms + TranslateY 采样关键帧 -16→0/350ms；
/// 旧内容：整层 Opacity 1→0/70ms 后卸载（对 PCL2 逐控件退出的简化，见报告）。
/// 每次切换先清掉该元素上的既有动画并归位（Opacity=1、位移 0），快速连续切页不会叠加。
/// </summary>
public sealed class PageTransition
{
    private readonly ContentControl _host;
    private Storyboard? _active;
    private int _generation;

    public PageTransition(ContentControl host)
    {
        _host = host;
        EnsureTransform(_host);
    }

    /// <summary>当前活动的 Storyboard（测试用：证明第二次切换换了新对象）。</summary>
    public Storyboard? ActiveStoryboard => _active;

    /// <summary>切换序号（每次 Begin* 自增）。</summary>
    public int Generation => _generation;

    /// <summary>确保元素有可动画的 TranslateTransform（页面本身不依赖别的 RenderTransform）。</summary>
    public static void EnsureTransform(FrameworkElement element)
    {
        if (element.RenderTransform is not TranslateTransform)
        {
            element.RenderTransform = new TranslateTransform();
        }
    }

    /// <summary>清掉元素上的透明度/位移动画并归位（动画结束态：Opacity=1、位移 0）。</summary>
    public static void ClearElement(FrameworkElement element)
    {
        element.BeginAnimation(UIElement.OpacityProperty, null);
        element.Opacity = 1.0;

        if (element.RenderTransform is TranslateTransform translate)
        {
            translate.BeginAnimation(TranslateTransform.XProperty, null);
            translate.BeginAnimation(TranslateTransform.YProperty, null);
            translate.X = 0;
            translate.Y = 0;
        }
    }

    /// <summary>播放新内容入场（会先停掉并清理上一轮动画）。</summary>
    public Storyboard BeginEnter()
    {
        _ = ++_generation;
        StopActive();
        EnsureTransform(_host);
        ClearElement(_host);

        Storyboard storyboard = JmlMotion.CreateContentEnterStoryboard();
        foreach (Timeline child in storyboard.Children)
        {
            Storyboard.SetTarget(child, _host);
        }

        _active = storyboard;
        storyboard.Begin(_host, isControllable: true);
        return storyboard;
    }

    /// <summary>
    /// 旧内容 70ms 淡出 → 回调换内容 → 新内容入场。
    /// 再次调用会停掉旧 Storyboard（其 Completed 不再触发），避免延迟回调覆盖新状态。
    /// </summary>
    public Storyboard BeginExitThenSwap(Action swap)
    {
        int generation = ++_generation;
        StopActive();
        EnsureTransform(_host);
        ClearElement(_host);

        Storyboard storyboard = JmlMotion.CreateContentExitStoryboard();
        foreach (Timeline child in storyboard.Children)
        {
            Storyboard.SetTarget(child, _host);
        }

        storyboard.Completed += (_, _) =>
        {
            if (generation != _generation)
            {
                return;
            }

            _active = null;
            swap();
            BeginEnter();
        };

        _active = storyboard;
        storyboard.Begin(_host, isControllable: true);
        return storyboard;
    }

    private void StopActive()
    {
        if (_active is null)
        {
            return;
        }

        _active.Stop(_host);
        _active = null;
    }
}

// =====================================================================
// 四、导航胶囊：悬停 / 按下 / 选中状态机（规格 5，C# 驱动，避免 Style Trigger 打架）
// =====================================================================

/// <summary>
/// PCL2 MyRadioButton 的颜色动效：悬停 90ms → #32EAF2FE、移出 150ms、按下 60ms → #78EAF2FE、
/// 选中 120ms 白色（白色由模板 IsChecked 触发器保证，回归测试锁死，见报告简化项）。
/// 悬停/按下各用一层底色，动画的是元素的 Opacity，不碰共享主题画刷。
/// </summary>
public static class NavMotion
{
    public const int HoverEnterMs = 90;
    public const int HoverLeaveMs = 150;
    public const int CheckedMs = 120;
    public const int PressedMs = 60;

    /// <summary>悬停底色（PCL2 MyRadioButton.xaml.vb:191 MyColor(50, Color8)）。</summary>
    public static readonly Color HoverColor = Color.FromArgb(0x32, 0xEA, 0xF2, 0xFE);

    /// <summary>按下底色（MyRadioButton.xaml.vb:184 MyColor(120, Color8)）。</summary>
    public static readonly Color PressColor = Color.FromArgb(0x78, 0xEA, 0xF2, 0xFE);

    private sealed class State
    {
        public bool Hover;
        public bool Pressed;
        public Border? HoverLayer;
        public Border? PressLayer;
    }

    private static readonly ConditionalWeakTable<RadioButton, State> States = new();

    /// <summary>给导航胶囊挂上颜色动效（幂等，只能挂一次）。</summary>
    public static void Attach(RadioButton nav)
    {
        State state = States.GetOrCreateValue(nav);

        nav.MouseEnter += (_, _) =>
        {
            state.Hover = true;
            Refresh(nav, state);
        };

        nav.MouseLeave += (_, _) =>
        {
            state.Hover = false;
            state.Pressed = false;
            Refresh(nav, state);
        };

        nav.PreviewMouseLeftButtonDown += (_, _) =>
        {
            state.Pressed = true;
            Refresh(nav, state);
        };

        nav.PreviewMouseLeftButtonUp += (_, _) =>
        {
            state.Pressed = false;
            Refresh(nav, state);
        };

        nav.Checked += (_, _) => Refresh(nav, state);
        nav.Unchecked += (_, _) => Refresh(nav, state);
    }

    /// <summary>按当前状态刷新两层底色（测试也可直接调用）。</summary>
    internal static void Refresh(RadioButton nav, object? stateObject = null)
    {
        State state = stateObject as State ?? States.GetOrCreateValue(nav);
        EnsureParts(nav, state);

        bool isChecked = nav.IsChecked == true;
        int durationMs = isChecked
            ? CheckedMs
            : state.Pressed
                ? PressedMs
                : state.Hover
                    ? HoverEnterMs
                    : HoverLeaveMs;

        double hoverTarget = !isChecked && !state.Pressed && state.Hover ? 1 : 0;
        double pressTarget = !isChecked && state.Pressed ? 1 : 0;

        AnimateLayer(state.HoverLayer, hoverTarget, durationMs);
        AnimateLayer(state.PressLayer, pressTarget, durationMs);
    }

    private static void EnsureParts(RadioButton nav, State state)
    {
        if (state.HoverLayer is not null && state.PressLayer is not null)
        {
            return;
        }

        nav.ApplyTemplate();
        state.HoverLayer ??= nav.Template?.FindName("HoverLayer", nav) as Border;
        state.PressLayer ??= nav.Template?.FindName("PressLayer", nav) as Border;
    }

    private static void AnimateLayer(Border? layer, double target, int durationMs)
    {
        if (layer is null)
        {
            return;
        }

        double current = layer.Opacity;
        layer.BeginAnimation(
            UIElement.OpacityProperty,
            new DoubleAnimation
            {
                From = current,
                To = target,
                Duration = TimeSpan.FromMilliseconds(durationMs),
                FillBehavior = FillBehavior.HoldEnd,
            });
    }
}

// =====================================================================
// 五、标题栏图标按钮：悬停底色 + 按下 0.8 / 抬起回弹（规格 6）
// =====================================================================

/// <summary>
/// PCL2 MyIconButton：悬停底色 120ms、常态 150ms；按下内容缩放 1→0.8/400ms
/// PowerEase(4, EaseOut)；抬起 250ms 复合（+0.25/250 Back(2) 与 -0.05/250 Fluent(4) 叠加），
/// 中途约 1.04 再落回 1.0 —— 用采样关键帧合成（规格 12.4）。
/// </summary>
public static class IconButtonMotion
{
    public const int HoverMs = 120;
    public const int NormalMs = 150;
    public const int PressMs = 400;
    public const int ReleaseMs = 250;
    public const int LeaveRestoreMs = 250;
    public const double PressScale = 0.8;

    private sealed class State
    {
        public bool Hover;
        public bool Pressed;
        public ScaleTransform? Scale;
        public Border? HoverLayer;
    }

    private static readonly ConditionalWeakTable<Button, State> States = new();

    /// <summary>按下：1→0.8 / 400ms / PowerEase(4, EaseOut)（关键帧形式，便于测试断言）。</summary>
    public static DoubleAnimationUsingKeyFrames CreatePressScale(double from = 1.0)
    {
        var animation = new DoubleAnimationUsingKeyFrames
        {
            Duration = TimeSpan.FromMilliseconds(PressMs),
            FillBehavior = FillBehavior.HoldEnd,
        };

        animation.KeyFrames.Add(new LinearDoubleKeyFrame(
            from, KeyTime.FromTimeSpan(TimeSpan.Zero)));
        animation.KeyFrames.Add(new EasingDoubleKeyFrame(
            PressScale,
            KeyTime.FromTimeSpan(TimeSpan.FromMilliseconds(PressMs)),
            JmlMotion.EaseOutFluent(4)));

        return animation;
    }

    /// <summary>抬起：0.8 → 1.0（+0.25/250 Back(2) 与 -0.05/250 Fluent(4) 采样合并，约 1.04 过冲）。</summary>
    public static DoubleAnimationUsingKeyFrames CreateReleaseScale(double from = PressScale) =>
        JmlMotion.Combine(
            from,
            new MotionPart(0.25, ReleaseMs, 0, JmlMotion.EaseOutBack(2)),
            new MotionPart(-0.05, ReleaseMs, 0, JmlMotion.EaseOutFluent(4)));

    /// <summary>给按钮挂上动效（幂等）。</summary>
    public static void Attach(Button button)
    {
        State state = States.GetOrCreateValue(button);
        if (state.Scale is not null)
        {
            return;
        }

        var scale = new ScaleTransform(1, 1);
        state.Scale = scale;
        button.RenderTransformOrigin = new Point(0.5, 0.5);
        button.RenderTransform = scale;

        button.MouseEnter += (_, _) =>
        {
            state.Hover = true;
            AnimateLayer(state, button, 1, HoverMs);
        };

        button.MouseLeave += (_, _) =>
        {
            state.Hover = false;
            AnimateLayer(state, button, 0, NormalMs);

            if (state.Pressed)
            {
                // 按住时移出：250ms 回 1.0（规格 6.2 移出 250ms EaseOutFluent(3)）
                state.Pressed = false;
                AnimateScale(
                    state,
                    new DoubleAnimation(CurrentScale(state), 1.0, TimeSpan.FromMilliseconds(LeaveRestoreMs))
                    {
                        EasingFunction = JmlMotion.EaseOutFluent(3),
                        FillBehavior = FillBehavior.HoldEnd,
                    });
            }
        };

        button.PreviewMouseLeftButtonDown += (_, _) =>
        {
            state.Pressed = true;
            AnimateScale(state, CreatePressScale(CurrentScale(state)));
        };

        button.PreviewMouseLeftButtonUp += (_, _) =>
        {
            if (!state.Pressed)
            {
                return;
            }

            state.Pressed = false;
            AnimateScale(state, CreateReleaseScale(CurrentScale(state)));
        };
    }

    private static double CurrentScale(State state) => state.Scale?.ScaleX ?? 1.0;

    private static void AnimateScale(State state, DoubleAnimationUsingKeyFrames animation)
    {
        if (state.Scale is null)
        {
            return;
        }

        state.Scale.BeginAnimation(ScaleTransform.ScaleXProperty, animation);
        state.Scale.BeginAnimation(ScaleTransform.ScaleYProperty, animation.Clone());
    }

    private static void AnimateScale(State state, DoubleAnimation animation)
    {
        if (state.Scale is null)
        {
            return;
        }

        state.Scale.BeginAnimation(ScaleTransform.ScaleXProperty, animation);
        state.Scale.BeginAnimation(ScaleTransform.ScaleYProperty, animation.Clone());
    }

    private static void AnimateLayer(State state, Button button, double target, int durationMs)
    {
        if (state.HoverLayer is null)
        {
            button.ApplyTemplate();
            state.HoverLayer = button.Template?.FindName("HoverLayer", button) as Border;
        }

        if (state.HoverLayer is null)
        {
            return;
        }

        double current = state.HoverLayer.Opacity;
        state.HoverLayer.BeginAnimation(
            UIElement.OpacityProperty,
            new DoubleAnimation
            {
                From = current,
                To = target,
                Duration = TimeSpan.FromMilliseconds(durationMs),
                FillBehavior = FillBehavior.HoldEnd,
            });
    }
}

/// <summary>
/// JML 主窗口（PCL2 同款浅色无边框外壳 + 现有导航接线）。
///
/// 外壳（2026-10-17 重构，参数见 docs/20261017_PCL2界面视觉规格_v1.0.md A/B 节）：
///   - WindowStyle=None + AllowsTransparency + Background=null，窗体内容用 Border.Clip 圆角 6；
///   - 外层 10 + 内层 8 的透明边给 8 块自绘渐变阴影（PCL2 FormMain.xaml:21-88）；
///   - 8 个阴影矩形同时充当缩放手柄（PCL2 MyResizer 的 8 个命中矩形，不依赖系统边框）；
///   - 标题栏 48 高纯蓝：左侧文字 logo JML、中间四个胶囊导航、右侧最小化/关闭；
///     空白处 MouseLeftButtonDown → DragMove()，双击不最大化（PCL2 FormMain.xaml.vb:643）；
///   - 内页模式：主导航隐藏，改为「← 返回 + 页面名」。
///
/// 动效（2026-10-17 追加，参数见 docs/20261017_PCL2动效规格_v1.0.md）：
///   - 窗口入场：PanBack Y 60→0/600ms + Rotate -4→0/500ms + Window.Opacity 0→1/250ms；
///   - 页面内容切换（一级页 + 内页共用 PageTransition）：新内容淡入 + 16px 上滑，旧内容 70ms 淡出；
///   - 标题栏主层 ↔ 内层 150/350ms 镜像切换；
///   - 导航胶囊 / 标题栏图标按钮的悬停与按下反馈。
///
/// 导航（行为与换壳前完全一致）：
///   顶部四个 RadioButton → NavigationStack.Navigate（一级页面）；
///   插件功能页双击 → PushInner（内页）；顶部返回 → PopInner。
///   顶部标题栏在主模式/内页模式间的切换由 InnerChanged 事件驱动。
///
/// 阶段 2 接线（本类只做界面调度，不含业务逻辑）：
///   搜索页「下载」/ 详情窗「下载本子」→ 切到下载页 + 传编号自动开始；
///   详情窗由本窗口统一持有/打开，避免多实例互相抢下载日志。
/// </summary>
public partial class MainWindow : Window
{
    private readonly NavigationStack _nav = new();

    // 各一级页面实例（复用，避免每次切换重建丢状态）
    private readonly DownloadPage _downloadPage = new();
    private readonly SearchPage _searchPage = new();
    private readonly SettingsPage _settingsPage = new();
    private readonly PluginHubPage _pluginHubPage = new();

    /// <summary>页面内容切换 helper（可停止/可重入）。</summary>
    private readonly PageTransition _pageTransition;

    /// <summary>详情窗单实例（MainWindow 统一持有）。</summary>
    private DetailWindow? _detailWindow;

    private bool _setupPassed;

    // ---- 导航状态（动效用）----
    private bool _isInnerMode;
    private object? _currentContent;
    private string? _currentInnerPageId;

    // ---- 自绘缩放状态（PCL2 MyResizer 的等价实现）----
    private ResizeEdge _resizeEdge = ResizeEdge.None;
    private Rect _resizeStartRect;
    private Point _resizeStartMouseDip;
    private bool _isResizing;

    private enum ResizeEdge
    {
        None,
        Top,
        Bottom,
        Left,
        Right,
        TopLeft,
        TopRight,
        BottomLeft,
        BottomRight,
    }

    public MainWindow()
    {
        InitializeComponent();

        _pageTransition = new PageTransition(MainContent);
        SizeChanged += (_, _) => UpdateFormClip();

        _nav.MainChanged += OnMainChanged;
        _nav.InnerChanged += OnInnerChanged;
        _pluginHubPage.FeatureRequested += OnFeatureRequested;

        // 搜索页的行内「详情」「下载」由主窗口协调。
        _searchPage.DownloadRequested += OnSearchDownloadRequested;
        _searchPage.DetailRequested += OnSearchDetailRequested;

        Loaded += (_, _) =>
        {
            UpdateFormClip();
            AttachMotionBehaviors();
            StartWindowEntrance();
            ShowSetup();
        };
    }

    // ------------------------------------------------------------------
    // 窗口入场（PCL2 FormMain.xaml.vb:380-388）
    // ------------------------------------------------------------------

    /// <summary>标题栏按钮与导航胶囊的悬停/按下动效挂点。</summary>
    private void AttachMotionBehaviors()
    {
        foreach (RadioButton nav in new[] { NavDownload, NavSearch, NavPlugin, NavSettings })
        {
            NavMotion.Attach(nav);
        }

        foreach (Button button in new[] { BtnTitleMin, BtnTitleClose, BtnBack })
        {
            IconButtonMotion.Attach(button);
        }
    }

    /// <summary>
    /// 窗口入场：PanBack 位移 60→0 / 600ms、旋转 -4→0 / 500ms、Window.Opacity 0→1 / 250ms，
    /// 三条同时（各自延迟 100ms）；位移结束时收尾：RenderTransform 归零、Opacity=1、不留 Effect。
    /// </summary>
    private void StartWindowEntrance()
    {
        try
        {
            DoubleAnimation translate = JmlMotion.CreateWindowEntryTranslate();
            WindowEntryTranslate.BeginAnimation(TranslateTransform.YProperty, translate);
            WindowEntryRotate.BeginAnimation(RotateTransform.AngleProperty, JmlMotion.CreateWindowEntryRotate());
            BeginAnimation(OpacityProperty, JmlMotion.CreateWindowEntryOpacity());

            translate.Completed += (_, _) =>
            {
                WindowEntryTranslate.BeginAnimation(TranslateTransform.YProperty, null);
                WindowEntryRotate.BeginAnimation(RotateTransform.AngleProperty, null);
                WindowEntryTranslate.Y = 0;
                WindowEntryRotate.Angle = 0;
                PanBack.RenderTransform = null;

                BeginAnimation(OpacityProperty, null);
                Opacity = 1.0;
            };
        }
        catch (Exception)
        {
            // 动画启动失败不能把窗口留在透明/错位状态。
            PanBack.RenderTransform = null;
            Opacity = 1.0;
        }
    }

    // ------------------------------------------------------------------
    // 无边框外壳：圆角裁剪 / 拖动 / 缩放 / 最小化 / 关闭
    // ------------------------------------------------------------------

    /// <summary>
    /// 同步 BorderForm 的圆角裁剪矩形（PCL2 FormMain.xaml.vb:638 的 RectForm.Rect 同一做法）。
    /// </summary>
    private void UpdateFormClip()
    {
        RectForm.Rect = new Rect(0, 0, BorderForm.ActualWidth, BorderForm.ActualHeight);
    }

    /// <summary>标题栏空白处拖动窗口；双击不最大化（PCL2 FormMain.xaml.vb:623-626）。</summary>
    private void PanTitle_MouseLeftButtonDown(object sender, MouseButtonEventArgs e)
    {
        if (e.ChangedButton != MouseButton.Left)
        {
            return;
        }

        // 双击：不处理，避免任何最大化路径（PCL2 本身不支持最大化）。
        if (e.ClickCount > 1)
        {
            e.Handled = true;
            return;
        }

        // 只有点在标题栏本体的空白处才拖动，点中导航/按钮时交给控件自己处理。
        if (!PanTitle.IsMouseDirectlyOver)
        {
            return;
        }

        try
        {
            DragMove();
        }
        catch (InvalidOperationException)
        {
            // 鼠标已在拖动开始前抬起等边界情况：忽略即可，不影响窗口。
        }
    }

    private void BtnTitleMin_Click(object sender, RoutedEventArgs e)
    {
        WindowState = WindowState.Minimized;
    }

    private void BtnTitleClose_Click(object sender, RoutedEventArgs e)
    {
        Close();
    }

    /// <summary>最大化不支持：被系统手势最大化时立即还原（PCL2 FormMain.xaml.vb:643）。</summary>
    private void Window_StateChanged(object? sender, EventArgs e)
    {
        if (WindowState == WindowState.Maximized)
        {
            WindowState = WindowState.Normal;
        }
    }

    /// <summary>按下阴影命中区，开始自绘缩放。</summary>
    private void ResizeHandle_MouseLeftButtonDown(object sender, MouseButtonEventArgs e)
    {
        if (e.ChangedButton != MouseButton.Left
            || sender is not FrameworkElement handle
            || handle.Tag is not string tag
            || !Enum.TryParse(tag, out ResizeEdge edge)
            || edge == ResizeEdge.None)
        {
            return;
        }

        _resizeEdge = edge;
        _resizeStartRect = new Rect(Left, Top, ActualWidth, ActualHeight);
        _resizeStartMouseDip = GetCursorPositionInDip();
        _isResizing = true;

        if (!handle.CaptureMouse())
        {
            // 捕获失败（如鼠标已抬起）就不进入缩放，避免窗口跟着鼠标乱动。
            _isResizing = false;
            _resizeEdge = ResizeEdge.None;
            return;
        }

        e.Handled = true;
    }

    /// <summary>
    /// 缩放循环：鼠标捕获在阴影矩形上，事件冒泡到窗口；
    /// 用屏幕坐标差（换算成 DIP）计算新窗口矩形，避免窗口移动带来的坐标系漂移。
    /// </summary>
    private void Window_MouseMove(object sender, MouseEventArgs e)
    {
        if (!_isResizing)
        {
            return;
        }

        if (e.LeftButton != MouseButtonState.Pressed)
        {
            EndResize();
            return;
        }

        Point current = GetCursorPositionInDip();
        double dx = current.X - _resizeStartMouseDip.X;
        double dy = current.Y - _resizeStartMouseDip.Y;

        double left = _resizeStartRect.Left;
        double top = _resizeStartRect.Top;
        double width = _resizeStartRect.Width;
        double height = _resizeStartRect.Height;

        if (IsRightEdge(_resizeEdge))
        {
            width = Math.Max(MinWidth, _resizeStartRect.Width + dx);
        }

        if (IsLeftEdge(_resizeEdge))
        {
            width = Math.Max(MinWidth, _resizeStartRect.Width - dx);
            left = _resizeStartRect.Right - width;
        }

        if (IsBottomEdge(_resizeEdge))
        {
            height = Math.Max(MinHeight, _resizeStartRect.Height + dy);
        }

        if (IsTopEdge(_resizeEdge))
        {
            height = Math.Max(MinHeight, _resizeStartRect.Height - dy);
            top = _resizeStartRect.Bottom - height;
        }

        Left = left;
        Top = top;
        Width = width;
        Height = height;
    }

    private void Window_MouseLeftButtonUp(object sender, MouseButtonEventArgs e)
    {
        if (e.ChangedButton == MouseButton.Left)
        {
            EndResize();
        }
    }

    private void EndResize()
    {
        if (!_isResizing)
        {
            return;
        }

        _isResizing = false;
        _resizeEdge = ResizeEdge.None;

        if (Mouse.Captured is UIElement captured)
        {
            captured.ReleaseMouseCapture();
        }
    }

    private static bool IsLeftEdge(ResizeEdge edge) =>
        edge is ResizeEdge.Left or ResizeEdge.TopLeft or ResizeEdge.BottomLeft;

    private static bool IsRightEdge(ResizeEdge edge) =>
        edge is ResizeEdge.Right or ResizeEdge.TopRight or ResizeEdge.BottomRight;

    private static bool IsTopEdge(ResizeEdge edge) =>
        edge is ResizeEdge.Top or ResizeEdge.TopLeft or ResizeEdge.TopRight;

    private static bool IsBottomEdge(ResizeEdge edge) =>
        edge is ResizeEdge.Bottom or ResizeEdge.BottomLeft or ResizeEdge.BottomRight;

    /// <summary>取鼠标的屏幕坐标并换算成 WPF 的 DIP（与 Window.Left/Top/Width/Height 同一坐标系）。</summary>
    private Point GetCursorPositionInDip()
    {
        if (!GetCursorPos(out NativePoint point))
        {
            return new Point(0, 0);
        }

        Matrix fromDevice = PresentationSource.FromVisual(this)?.CompositionTarget?.TransformFromDevice
                            ?? Matrix.Identity;
        return fromDevice.Transform(new Point(point.X, point.Y));
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct NativePoint
    {
        public int X;
        public int Y;
    }

    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GetCursorPos(out NativePoint point);

    // ------------------------------------------------------------------
    // 页面导航（行为与换壳前一致）
    // ------------------------------------------------------------------

    /// <summary>显示 Python 环境检测页（订阅其完成事件）。</summary>
    private void ShowSetup()
    {
        var setup = new PythonSetupPage();
        setup.SetupCompleted += (_, _) =>
        {
            _setupPassed = true;
            ShowMain();
        };

        _currentContent = setup;
        PageTransition.ClearElement(MainContent);
        MainContent.Content = setup;
    }

    /// <summary>进入主界面：默认显示插件页，并同步导航按钮选中态。</summary>
    private void ShowMain()
    {
        NavPlugin.IsChecked = true;
        _nav.Navigate(MainPage.Plugin);
        UpdateContent();
    }

    /// <summary>一级导航按钮点击（四个按钮共用一个处理器，按发送者区分）。</summary>
    private void Nav_Checked(object sender, RoutedEventArgs e)
    {
        if (!_setupPassed)
        {
            return;
        }

        if (ReferenceEquals(sender, NavDownload)) { _nav.Navigate(MainPage.Download); }
        else if (ReferenceEquals(sender, NavSearch)) { _nav.Navigate(MainPage.Search); }
        else if (ReferenceEquals(sender, NavPlugin)) { _nav.Navigate(MainPage.Plugin); }
        else if (ReferenceEquals(sender, NavSettings)) { _nav.Navigate(MainPage.Settings); }
    }

    /// <summary>内页返回按钮。</summary>
    private void BtnBack_Click(object sender, RoutedEventArgs e)
    {
        _nav.PopInner();
    }

    /// <summary>插件功能页双击：进入内页（阶段 1 为占位页）。</summary>
    private void OnFeatureRequested(object? sender, PluginFeaturesView.FeatureItem item)
    {
        _nav.PushInner(item.PageId, item.PageTitle);
    }

    // ------------------------------------------------------------------
    // 阶段 2 接线：搜索 / 详情 → 下载页
    // ------------------------------------------------------------------

    private async void OnSearchDownloadRequested(object? sender, string bookId)
    {
        await StartDownloadFromOtherPageAsync(new[] { bookId });
    }

    private void OnSearchDetailRequested(object? sender, string bookId)
    {
        OpenDetailWindow(bookId);
    }

    private async void OnDetailDownloadRequested(object? sender, string bookId)
    {
        await StartDownloadFromOtherPageAsync(new[] { bookId });
    }

    /// <summary>
    /// 切到下载页，把编号交给下载页自动开始；成功后把 batch_id 告知详情窗，
    /// 让详情窗只过滤本书批次的日志（旧缺陷 #6：各窗口各按 batch_id 分窗）。
    /// </summary>
    private async Task StartDownloadFromOtherPageAsync(IReadOnlyList<string> ids)
    {
        _nav.Navigate(MainPage.Download);
        string? batchId = await _downloadPage.StartWithIds(ids);

        if (string.IsNullOrEmpty(batchId))
        {
            _detailWindow?.NotifyDownloadNotStarted();
            return;
        }

        _detailWindow?.TrackBatch(batchId!);
    }

    /// <summary>详情窗统一打开入口：单实例复用，关闭后置空。</summary>
    private void OpenDetailWindow(string bookId)
    {
        if (string.IsNullOrWhiteSpace(bookId))
        {
            return;
        }

        if (_detailWindow is { } existing)
        {
            existing.SetBook(bookId);
            existing.Activate();
            return;
        }

        var window = new DetailWindow(bookId) { Owner = this };
        window.DownloadRequested += OnDetailDownloadRequested;
        window.Closed += (_, _) =>
        {
            window.DownloadRequested -= OnDetailDownloadRequested;
            if (ReferenceEquals(_detailWindow, window))
            {
                _detailWindow = null;
            }
        };

        _detailWindow = window;
        window.Show();
    }

    private void OnMainChanged(object? sender, MainPage page)
    {
        SyncNavChecked(page);
        UpdateContent();
    }

    private void OnInnerChanged(object? sender, EventArgs e)
    {
        UpdateContent();
    }

    /// <summary>让导航按钮选中态与 NavigationStack 当前页保持一致。</summary>
    private void SyncNavChecked(MainPage page)
    {
        switch (page)
        {
            case MainPage.Download: NavDownload.IsChecked = true; break;
            case MainPage.Search: NavSearch.IsChecked = true; break;
            case MainPage.Plugin: NavPlugin.IsChecked = true; break;
            case MainPage.Settings: NavSettings.IsChecked = true; break;
        }
    }

    /// <summary>
    /// 根据导航状态刷新顶部标题栏与内容区。
    /// 内页时：隐藏主导航（PCL2 的 PanTitleMain），显示「← 返回 + 页面名」，
    /// 内容区为插件功能占位页。
    /// 一级页与内页共用同一套切换动画（PageTransition + 标题栏镜像）。
    /// </summary>
    private void UpdateContent()
    {
        if (!_setupPassed)
        {
            return;
        }

        var inner = _nav.CurrentInner;
        object content;
        string? title = null;

        if (inner is not null)
        {
            title = inner.Value.Title;

            // Navigate 会先触发 MainChanged 再触发 InnerChanged（两次 UpdateContent），
            // 同一内页复用同一个占位页实例，避免第二次调用把入场动画重播一遍。
            if (_currentInnerPageId == inner.Value.PageId && _currentContent is PluginPagePlaceholder reusable)
            {
                content = reusable;
            }
            else
            {
                var placeholder = new PluginPagePlaceholder();
                placeholder.SetContent(inner.Value.Title);
                content = placeholder;
            }

            _currentInnerPageId = inner.Value.PageId;
        }
        else
        {
            _currentInnerPageId = null;
            content = _nav.Current switch
            {
                MainPage.Download => _downloadPage,
                MainPage.Search => _searchPage,
                MainPage.Settings => _settingsPage,
                _ => _pluginHubPage,
            };
        }

        SwitchTo(content, inner is not null, title);
    }

    /// <summary>内容 + 标题栏模式切换的唯一入口。</summary>
    private void SwitchTo(object content, bool innerMode, string? title)
    {
        if (!string.IsNullOrEmpty(title) && TextInnerTitle.Text != title)
        {
            TextInnerTitle.Text = title;
        }

        if (innerMode != _isInnerMode)
        {
            _isInnerMode = innerMode;
            PlayHeaderTransition(innerMode);
        }

        ShowContent(content);
    }

    /// <summary>
    /// 内容区切换：旧内容整层 70ms 淡出 → 换内容 → 新内容 100ms 淡入 + 16px 上滑落位。
    /// 全程 Storyboard，不阻塞 UI 线程（任务 2 / 硬约束 3）。
    /// </summary>
    private void ShowContent(object content)
    {
        if (ReferenceEquals(content, _currentContent))
        {
            return;
        }

        _currentContent = content;

        if (!IsLoaded)
        {
            PageTransition.ClearElement(MainContent);
            MainContent.Content = content;
            return;
        }

        _pageTransition.BeginExitThenSwap(() => MainContent.Content = content);
    }

    /// <summary>
    /// 标题栏主层 ↔ 内层切换（规格 4.1 / 4.2 的镜像实现）。
    /// 动画是纯 RenderTransform + Opacity，不动 Margin/Height。
    /// </summary>
    private void PlayHeaderTransition(bool toInner)
    {
        if (!IsLoaded)
        {
            MainHeader.Visibility = toInner ? Visibility.Collapsed : Visibility.Visible;
            InnerHeader.Visibility = toInner ? Visibility.Visible : Visibility.Collapsed;
            return;
        }

        // 先清掉上一轮动画，并把基础值设成“动画开始前的当前视觉状态”
        // （延迟 200ms 的那一段靠基础值顶着，不能提前闪现）。
        MainHeader.BeginAnimation(OpacityProperty, null);
        InnerHeader.BeginAnimation(OpacityProperty, null);
        MainHeaderShift.BeginAnimation(TranslateTransform.XProperty, null);
        InnerHeaderShift.BeginAnimation(TranslateTransform.XProperty, null);

        if (toInner)
        {
            MainHeader.Opacity = 1;
            InnerHeader.Opacity = 0;
            MainHeaderShift.X = 0;
            InnerHeaderShift.X = -16;
        }
        else
        {
            MainHeader.Opacity = 0;
            InnerHeader.Opacity = 1;
            MainHeaderShift.X = 12;
            InnerHeaderShift.X = 0;
        }

        MainHeader.Visibility = Visibility.Visible;
        InnerHeader.Visibility = Visibility.Visible;

        Storyboard storyboard = toInner
            ? JmlMotion.CreateHeaderEnterStoryboard()
            : JmlMotion.CreateHeaderReturnStoryboard();

        storyboard.Completed += (_, _) =>
        {
            // 期间又切回另一模式时，由后启动的那条动画负责收尾。
            if (_isInnerMode != toInner)
            {
                return;
            }

            if (toInner)
            {
                MainHeader.Visibility = Visibility.Collapsed;
            }
            else
            {
                InnerHeader.Visibility = Visibility.Collapsed;
            }
        };

        storyboard.Begin(this);
    }
}
