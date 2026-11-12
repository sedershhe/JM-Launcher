using System.Windows;
using System.Windows.Controls;
using System.Windows.Media.Animation;

namespace JML.Controls;

/// <summary>
/// PCL2 挥镐加载动画（Controls\JmlLoadingPickaxe.xaml 的代码后置）。
///
/// 几何与动效照抄 PCL2 <c>Controls\MyLoading.xaml</c> 与 <c>MyLoading.xaml.vb</c> 的
/// <c>AniLoop</c>：60×47 画布、镐子绕 (30,30) 旋转、左右碎屑、底部 25×2 进度条。
/// 时间轴完整写在 XAML Storyboard 里（见 xaml 顶部注释），本类只负责：
/// <list type="bullet">
/// <item><see cref="IsRunning"/> 为 true 时启动循环动画；</item>
/// <item>为 false 时停止动画，并把镐子角度与碎屑复位到 PCL2 起始姿态；</item>
/// <item>不控制自身 <see cref="UIElement.Visibility"/>，显隐由宿主决定。</item>
/// </list>
/// </summary>
public partial class JmlLoadingPickaxe : UserControl
{
    /// <summary>停止 / 每次挥镐起点的镐子角度（PCL2 AniLoop 第一段终点 -20°）。</summary>
    public const double StoppedAngle = -20;

    /// <summary>动画一个完整周期的时长（毫秒），与 XAML Storyboard 的 Duration 一致。</summary>
    public const int CycleDurationMs = 1600;

    public static readonly DependencyProperty IsRunningProperty = DependencyProperty.Register(
        nameof(IsRunning),
        typeof(bool),
        typeof(JmlLoadingPickaxe),
        new PropertyMetadata(false, OnIsRunningChanged));

    /// <summary>是否正在播放挥镐动画；默认 false。</summary>
    public bool IsRunning
    {
        get => (bool)GetValue(IsRunningProperty);
        set => SetValue(IsRunningProperty, value);
    }

    private Storyboard? _storyboard;

    public JmlLoadingPickaxe()
    {
        InitializeComponent();
    }

    private Storyboard LoadingStoryboard =>
        _storyboard ??= (Storyboard)Resources["PickaxeStoryboard"];

    private static void OnIsRunningChanged(DependencyObject d, DependencyPropertyChangedEventArgs e)
    {
        ((JmlLoadingPickaxe)d).ApplyRunningState((bool)e.NewValue);
    }

    /// <summary>
    /// 启动 / 停止循环动画。Begin 传 isControllable=true，保证 Stop 能移除同一个时钟，
    /// 反复切换不会叠加出多个循环。
    /// </summary>
    private void ApplyRunningState(bool running)
    {
        if (running)
        {
            LoadingStoryboard.Begin(this, true);
            return;
        }

        LoadingStoryboard.Stop(this);
        ResetVisualState();
    }

    /// <summary>把镐子与碎屑复位到 PCL2 起始姿态：镐子 -20°、碎屑隐藏且无位移。</summary>
    private void ResetVisualState()
    {
        PickaxeRotate.Angle = StoppedAngle;
        PathLeft.Opacity = 0;
        PathRight.Opacity = 0;
        LeftTranslate.X = 0;
        LeftTranslate.Y = 0;
        RightTranslate.X = 0;
        RightTranslate.Y = 0;
    }
}
