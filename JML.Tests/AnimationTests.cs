using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Media.Animation;
using System.Windows.Media.Effects;
using JML;
using JML.Bridge.Dto;
using JML.Controls;
using JML.Pages;
using Xunit;

namespace JML.Tests;

/// <summary>
/// 动效回归（2026-10-17，参数来源 docs/20261017_PCL2动效规格_v1.0.md）：
/// 1) 页面切换 helper 的时间轴：总时长、关键帧数量、采样值（规格 12.4 的采样表）；
/// 2) 连续两次切换不会叠加（第二次调用前旧动画被停掉/清理）；
/// 3) 导航胶囊悬停底色 = #32EAF2FE（PCL2 值），四档时长 90/150/120/60；
/// 4) 图标按钮按下/抬起缩放关键帧存在（按下 1→0.8/400，抬起 250ms 复合过冲 ≈1.04）；
/// 5) 窗口入场三条动画、内页标题栏镜像时间轴、搜索加载指示的显示/清理。
/// 只断言动效参数与状态，不绑定业务文案。
/// </summary>
public class AnimationTests
{
    // ------------------------------------------------------------------
    // 页面切换 helper（任务 2 / 规格 12.4）
    // ------------------------------------------------------------------

    /// <summary>
    /// 新内容位移必须是“采样合并”的一条关键帧动画：
    /// 350ms、36 帧（0/10/…/340 + 精确 350）、起点 -16、终点 0，
    /// 中途按规格 12.4 表过冲（t=150 → +1.7、t=200 → +2.7）。
    /// </summary>
    [Fact]
    public void ContentEnterTranslate_IsSampledKeyFrameAnimation()
    {
        DoubleAnimationUsingKeyFrames animation = JmlMotion.CreateContentEnterTranslate();

        Assert.Equal(TimeSpan.FromMilliseconds(350), animation.Duration.TimeSpan);
        Assert.Equal(36, animation.KeyFrames.Count);

        var frames = animation.KeyFrames.Cast<DoubleKeyFrame>().ToList();
        Assert.Equal(TimeSpan.Zero, frames[0].KeyTime.TimeSpan);
        Assert.Equal(TimeSpan.FromMilliseconds(340), frames[^2].KeyTime.TimeSpan);
        Assert.Equal(TimeSpan.FromMilliseconds(350), frames[^1].KeyTime.TimeSpan);
        Assert.Equal(-16, frames[0].Value, 3);
        Assert.Equal(0, frames[^1].Value, 3);

        DoubleKeyFrame at150 = frames.Single(f => f.KeyTime.TimeSpan == TimeSpan.FromMilliseconds(150));
        DoubleKeyFrame at200 = frames.Single(f => f.KeyTime.TimeSpan == TimeSpan.FromMilliseconds(200));
        Assert.InRange(at150.Value, 1.6, 1.9);
        Assert.InRange(at200.Value, 2.6, 2.9);

        // 每一帧都是线性关键帧（曲线已经采样进数值里，不能再挂缓动）。
        Assert.All(frames, frame => Assert.IsType<LinearDoubleKeyFrame>(frame));
    }

    /// <summary>新内容 Storyboard：Opacity 100ms + 位移 350ms，总时长 350ms。</summary>
    [Fact]
    public void ContentEnterStoryboard_HasPclDurations()
    {
        Storyboard storyboard = JmlMotion.CreateContentEnterStoryboard();

        Assert.Equal(2, storyboard.Children.Count);
        Assert.Equal(TimeSpan.FromMilliseconds(100), storyboard.Children[0].Duration.TimeSpan);
        Assert.Equal(TimeSpan.FromMilliseconds(350), storyboard.Children[1].Duration.TimeSpan);
        Assert.Equal(TimeSpan.FromMilliseconds(350), JmlMotion.TotalDuration(storyboard));

        var opacity = Assert.IsType<DoubleAnimation>(storyboard.Children[0]);
        Assert.Equal(0, opacity.From);
        Assert.Equal(1, opacity.To);

        // 旧内容退场：70ms（规格 3.2 的简化项）。
        Assert.Equal(
            TimeSpan.FromMilliseconds(70),
            JmlMotion.TotalDuration(JmlMotion.CreateContentExitStoryboard()));
    }

    /// <summary>
    /// 连续两次 BeginEnter：第二次必须停掉并清理上一轮动画（硬约束 2），
    /// 旧 Storyboard 不再处于 Active，元素上只剩一条活动动画。
    /// </summary>
    [Fact]
    public void ContentSwitch_SecondBegin_ClearsPreviousAnimation()
    {
        UiSmokeTests.RunOnUiThread(() =>
        {
            var host = new ContentControl();
            var transition = new PageTransition(host);

            Storyboard first = transition.BeginEnter();
            Assert.True(host.HasAnimatedProperties, "第一次切换没有在内容层上挂动画。");
            Assert.Same(first, transition.ActiveStoryboard);

            Storyboard second = transition.BeginEnter();
            Assert.NotSame(first, second);
            Assert.Same(second, transition.ActiveStoryboard);
            Assert.True(host.HasAnimatedProperties, "第二次切换后内容层应当仍有（新的）活动动画。");

            // 旧 Storyboard 已被 Stop 并从该对象上移除：WPF 不允许再对它查状态。
            Assert.Throws<InvalidOperationException>(() => first.GetCurrentState());

            // 第二轮切换后从 0 重新淡入、位移仍从 -16 起步（不能叠加成 -32）。
            var keyFrames = Assert.IsType<DoubleAnimationUsingKeyFrames>(second.Children[1]);
            Assert.Equal(-16, keyFrames.KeyFrames.Cast<DoubleKeyFrame>().First().Value, 3);
        });
    }

    /// <summary>ClearElement 是“动画结束态”的唯一出口：Opacity=1、位移归零、无动画残留。</summary>
    [Fact]
    public void ClearElement_ResetsOpacityTransformAndAnimations()
    {
        UiSmokeTests.RunOnUiThread(() =>
        {
            var host = new ContentControl
            {
                RenderTransform = new TranslateTransform(4, -16),
                Opacity = 0.3,
            };
            var transition = new PageTransition(host);
            transition.BeginEnter();
            Assert.True(host.HasAnimatedProperties);

            PageTransition.ClearElement(host);

            Assert.Equal(1.0, host.Opacity);
            var translate = Assert.IsType<TranslateTransform>(host.RenderTransform);
            Assert.Equal(0, translate.X);
            Assert.Equal(0, translate.Y);
            Assert.False(host.HasAnimatedProperties);
        });
    }

    // ------------------------------------------------------------------
    // 内页标题栏（任务 2）
    // ------------------------------------------------------------------

    /// <summary>
    /// 一级页 → 内页：主层 1→0/150 + X 0→12/150（EaseInFluent2）；
    /// 内层 0→1/150 延迟 200 + X -16→0/350 延迟 200（EaseOutBack1.5）；总 550ms。
    /// </summary>
    [Fact]
    public void HeaderEnter_MatchesPclMirror()
    {
        Storyboard storyboard = JmlMotion.CreateHeaderEnterStoryboard();

        Assert.Equal(4, storyboard.Children.Count);
        Assert.Equal(TimeSpan.FromMilliseconds(550), JmlMotion.TotalDuration(storyboard));

        var animations = storyboard.Children.Cast<DoubleAnimation>().ToList();

        Assert.Equal("MainHeader", Storyboard.GetTargetName(animations[0]));
        Assert.Equal(1, animations[0].From);
        Assert.Equal(0, animations[0].To);
        Assert.Equal(TimeSpan.FromMilliseconds(150), animations[0].Duration.TimeSpan);
        Assert.Null(animations[0].EasingFunction);

        Assert.Contains("TranslateTransform.X", Storyboard.GetTargetProperty(animations[1]).Path);
        Assert.Equal(0, animations[1].From);
        Assert.Equal(12, animations[1].To);
        var mainEase = Assert.IsType<PowerEase>(animations[1].EasingFunction);
        Assert.Equal(2, mainEase.Power);
        Assert.Equal(EasingMode.EaseIn, mainEase.EasingMode);

        Assert.Equal("InnerHeader", Storyboard.GetTargetName(animations[2]));
        Assert.Equal(0, animations[2].From);
        Assert.Equal(1, animations[2].To);
        Assert.Equal(TimeSpan.FromMilliseconds(200), animations[2].BeginTime!.Value);
        Assert.Equal(TimeSpan.FromMilliseconds(150), animations[2].Duration.TimeSpan);

        Assert.Contains("TranslateTransform.X", Storyboard.GetTargetProperty(animations[3]).Path);
        Assert.Equal(-16, animations[3].From);
        Assert.Equal(0, animations[3].To);
        Assert.Equal(TimeSpan.FromMilliseconds(200), animations[3].BeginTime!.Value);
        Assert.Equal(TimeSpan.FromMilliseconds(350), animations[3].Duration.TimeSpan);
        var innerEase = Assert.IsType<PclEaseOutBack>(animations[3].EasingFunction);
        Assert.Equal(1.5, innerEase.Power, 3);
    }

    /// <summary>内页 → 一级页：镜像参数（内层 0→-18/150；主层 +12→0/350 Back弱）。</summary>
    [Fact]
    public void HeaderReturn_IsMirrored()
    {
        Storyboard storyboard = JmlMotion.CreateHeaderReturnStoryboard();

        Assert.Equal(4, storyboard.Children.Count);
        Assert.Equal(TimeSpan.FromMilliseconds(550), JmlMotion.TotalDuration(storyboard));

        var animations = storyboard.Children.Cast<DoubleAnimation>().ToList();

        Assert.Equal("InnerHeader", Storyboard.GetTargetName(animations[0]));
        Assert.Equal(1, animations[0].From);
        Assert.Equal(0, animations[0].To);
        Assert.Equal(TimeSpan.FromMilliseconds(150), animations[0].Duration.TimeSpan);

        Assert.Equal(0, animations[1].From);
        Assert.Equal(-18, animations[1].To);
        var innerEase = Assert.IsType<PowerEase>(animations[1].EasingFunction);
        Assert.Equal(3, innerEase.Power);
        Assert.Equal(EasingMode.EaseIn, innerEase.EasingMode);

        Assert.Equal("MainHeader", Storyboard.GetTargetName(animations[2]));
        Assert.Equal(0, animations[2].From);
        Assert.Equal(1, animations[2].To);
        Assert.Equal(TimeSpan.FromMilliseconds(200), animations[2].BeginTime!.Value);

        Assert.Equal(12, animations[3].From);
        Assert.Equal(0, animations[3].To);
        Assert.Equal(TimeSpan.FromMilliseconds(350), animations[3].Duration.TimeSpan);
        var mainEase = Assert.IsType<PclEaseOutBack>(animations[3].EasingFunction);
        Assert.Equal(2, mainEase.Power, 3);
    }

    // ------------------------------------------------------------------
    // 导航胶囊（任务 3 / 规格 5）
    // ------------------------------------------------------------------

    /// <summary>
    /// 悬停底色必须是 PCL2 的 #32EAF2FE（旧值 #32FFFFFF 是错的）；
    /// 四档时长 90 / 150 / 120 / 60；模板里不出现 #32FFFFFF 悬停色。
    /// </summary>
    [Fact]
    public void NavHover_UsesPclColorAndTimings()
    {
        Assert.Equal(Color.FromArgb(0x32, 0xEA, 0xF2, 0xFE), NavMotion.HoverColor);
        Assert.Equal(Color.FromArgb(0x78, 0xEA, 0xF2, 0xFE), NavMotion.PressColor);
        Assert.Equal(90, NavMotion.HoverEnterMs);
        Assert.Equal(150, NavMotion.HoverLeaveMs);
        Assert.Equal(120, NavMotion.CheckedMs);
        Assert.Equal(60, NavMotion.PressedMs);

        UiSmokeTests.RunOnUiThread(() =>
        {
            var nav = new RadioButton
            {
                Style = (Style)Application.Current.FindResource("NavRadioButton"),
                Content = "下载",
            };

            nav.Measure(new Size(200, 48));
            nav.Arrange(new Rect(0, 0, 200, 48));
            nav.UpdateLayout();
            nav.ApplyTemplate();

            var hover = Assert.IsType<Border>(nav.Template.FindName("HoverLayer", nav));
            Assert.Equal(
                NavMotion.HoverColor,
                Assert.IsType<SolidColorBrush>(hover.Background).Color);

            var press = Assert.IsType<Border>(nav.Template.FindName("PressLayer", nav));
            Assert.Equal(
                NavMotion.PressColor,
                Assert.IsType<SolidColorBrush>(press.Background).Color);

            // 未悬停时两层的元素 Opacity 都是 0（不会在常态露色）。
            Assert.Equal(0, hover.Opacity);
            Assert.Equal(0, press.Opacity);
        });
    }

    // ------------------------------------------------------------------
    // 图标按钮（任务 3 / 规格 6）
    // ------------------------------------------------------------------

    /// <summary>
    /// 按下 1→0.8/400ms（PowerEase(4, EaseOut)），抬起 250ms 采样关键帧、
    /// 中途过冲约 1.04 再落回 1.0；两条时间轴都必须存在关键帧。
    /// </summary>
    [Fact]
    public void IconButton_PressAndRelease_ScaleKeyFrames()
    {
        DoubleAnimationUsingKeyFrames press = IconButtonMotion.CreatePressScale();
        Assert.True(press.KeyFrames.Count >= 2, "按下缩放没有关键帧。");
        Assert.Equal(TimeSpan.FromMilliseconds(400), press.Duration.TimeSpan);

        var pressFrames = press.KeyFrames.Cast<DoubleKeyFrame>().ToList();
        Assert.Equal(1.0, pressFrames[0].Value, 3);
        Assert.Equal(IconButtonMotion.PressScale, pressFrames[^1].Value, 3);
        Assert.Equal(0.8, pressFrames[^1].Value, 3);

        DoubleAnimationUsingKeyFrames release = IconButtonMotion.CreateReleaseScale();
        Assert.Equal(26, release.KeyFrames.Count);
        Assert.Equal(TimeSpan.FromMilliseconds(250), release.Duration.TimeSpan);

        var releaseFrames = release.KeyFrames.Cast<DoubleKeyFrame>().ToList();
        Assert.Equal(0.8, releaseFrames[0].Value, 3);
        Assert.Equal(1.0, releaseFrames[^1].Value, 3);
        Assert.InRange(releaseFrames.Max(frame => frame.Value), 1.03, 1.05);
    }

    // ------------------------------------------------------------------
    // 窗口入场（任务 1 / 规格 1.1）
    // ------------------------------------------------------------------

    /// <summary>
    /// 位移 60→0/600ms、旋转 -4→0/500ms、Window.Opacity 0→1/250ms，全部延迟 100ms；
    /// 位移与旋转用 PCL2 EaseOutBack(Weak)，透明度线性。
    /// </summary>
    [Fact]
    public void WindowEntry_Animations_MatchPcl()
    {
        DoubleAnimation translate = JmlMotion.CreateWindowEntryTranslate();
        Assert.Equal(60, translate.From);
        Assert.Equal(0, translate.To);
        Assert.Equal(TimeSpan.FromMilliseconds(600), translate.Duration.TimeSpan);
        Assert.Equal(TimeSpan.FromMilliseconds(100), translate.BeginTime!.Value);
        Assert.Equal(2, Assert.IsType<PclEaseOutBack>(translate.EasingFunction).Power, 3);

        DoubleAnimation rotate = JmlMotion.CreateWindowEntryRotate();
        Assert.Equal(-4, rotate.From);
        Assert.Equal(0, rotate.To);
        Assert.Equal(TimeSpan.FromMilliseconds(500), rotate.Duration.TimeSpan);
        Assert.Equal(TimeSpan.FromMilliseconds(100), rotate.BeginTime!.Value);
        Assert.Equal(2, Assert.IsType<PclEaseOutBack>(rotate.EasingFunction).Power, 3);

        DoubleAnimation opacity = JmlMotion.CreateWindowEntryOpacity();
        Assert.Equal(0, opacity.From);
        Assert.Equal(1, opacity.To);
        Assert.Equal(TimeSpan.FromMilliseconds(250), opacity.Duration.TimeSpan);
        Assert.Equal(TimeSpan.FromMilliseconds(100), opacity.BeginTime!.Value);
        Assert.Null(opacity.EasingFunction);
    }

    // ------------------------------------------------------------------
    // 卡片悬停（任务 3 / 规格 7.1）
    // ------------------------------------------------------------------

    /// <summary>
    /// 阴影必须动画“独立图层的元素 Opacity”，DropShadowEffect.Opacity 保持静态 0.4；
    /// 常态不叠加额外阴影；标题色 ColorBrush1→ColorBrush2。
    /// </summary>
    [Fact]
    public void CardHover_AnimatesLayerOpacity_NotEffectOpacity()
    {
        UiSmokeTests.RunOnUiThread(() =>
        {
            var card = new JmlCard
            {
                Header = "测试卡片",
                Content = new TextBlock { Text = "内容" },
            };
            card.Style = (Style)Application.Current.FindResource(typeof(JmlCard));

            card.Measure(new Size(320, 200));
            card.Arrange(new Rect(0, 0, 320, 200));
            card.UpdateLayout();

            var shadow = Assert.IsType<Border>(card.Template.FindName("CardShadowHover", card));
            var effect = Assert.IsType<DropShadowEffect>(shadow.Effect);
            Assert.Equal(JmlCard.ShadowHoverOpacity, effect.Opacity, 3);
            Assert.Equal(0, shadow.Opacity, 3);

            var header = Assert.IsType<TextBlock>(card.Template.FindName("HeaderText", card));
            var headerBrush = Assert.IsType<SolidColorBrush>(header.Foreground);
            Assert.Equal(((SolidColorBrush)Application.Current.FindResource("ColorBrush1")).Color, headerBrush.Color);

            card.ApplyHover(true, animate: false);

            // 阴影增量 = 0.4 - 0.07（0.07 由 CardShadow 的紧阴影提供）。
            Assert.Equal(
                JmlCard.ShadowHoverOpacity - JmlCard.ShadowIdleOpacity,
                shadow.Opacity,
                3);
            Assert.Equal(JmlCard.ShadowHoverOpacity, effect.Opacity, 3); // Effect 参数不动

            Assert.Equal(
                ((SolidColorBrush)Application.Current.FindResource("ColorBrush2")).Color,
                headerBrush.Color);

            card.ApplyHover(false, animate: false);
            Assert.Equal(0, shadow.Opacity, 3);
            Assert.Equal(
                ((SolidColorBrush)Application.Current.FindResource("ColorBrush1")).Color,
                headerBrush.Color);
        });
    }

    // ------------------------------------------------------------------
    // 主窗口接线（标题栏 Storyboard 的 TargetName 必须能在窗口命名域里解析）
    // ------------------------------------------------------------------

    /// <summary>
    /// 标题栏 Storyboard 是纯函数产出的（TargetName=MainHeader/InnerHeader），
    /// 真正 Begin 在 MainWindow 上时必须能解析到元素并挂上动画时钟。
    /// </summary>
    [Fact]
    public void HeaderStoryboard_BeginsOnMainWindow_AndTargetsHeaders()
    {
        UiSmokeTests.RunOnUiThread(() =>
        {
            var window = new MainWindow();
            try
            {
                var mainHeader = Assert.IsType<Grid>(window.FindName("MainHeader"));
                var innerHeader = Assert.IsType<Grid>(window.FindName("InnerHeader"));

                Storyboard storyboard = JmlMotion.CreateHeaderEnterStoryboard();
                storyboard.Begin(window);

                Assert.True(mainHeader.HasAnimatedProperties, "标题栏 Storyboard 没有解析到 MainHeader。");
                Assert.True(innerHeader.HasAnimatedProperties, "标题栏 Storyboard 没有解析到 InnerHeader。");

                storyboard.Stop(window);
                mainHeader.BeginAnimation(UIElement.OpacityProperty, null);
                innerHeader.BeginAnimation(UIElement.OpacityProperty, null);
            }
            finally
            {
                window.Close();
            }
        });
    }

    // ------------------------------------------------------------------
    // 搜索加载指示（任务 4 / 规格 10）
    // ------------------------------------------------------------------

    /// <summary>
    /// 搜索开始显示旋转指示、结束隐藏且角度复位；失败路径也不能残留动画/可见指示。
    /// </summary>
    [Fact]
    public void SearchLoading_ShowsWhileSearching_AndClearsAfterwards()
    {
        UiSmokeTests.RunOnUiThread(() =>
        {
            var page = new SearchPage();
            Assert.Equal(Visibility.Collapsed, page.LoadingOverlayVisibilityForTest);

            page.SetLoadingForTest(true);
            Assert.Equal(Visibility.Visible, page.LoadingOverlayVisibilityForTest);
            Assert.Equal(55, page.LoadingAngleForTest, 3);
            Assert.True(page.LoadingAnimatedForTest, "加载指示的挥镐 Storyboard 没有挂到镐子上（TargetName 未解析）。");

            page.SetLoadingForTest(false);
            Assert.Equal(Visibility.Collapsed, page.LoadingOverlayVisibilityForTest);
            Assert.Equal(55, page.LoadingAngleForTest, 3);
            Assert.False(page.LoadingAnimatedForTest, "加载指示结束后仍残留动画时钟。");

            // 真实搜索（立即失败）也必须走完 SetBusy(false)，不留可见指示。
            page.SearchInvoker = (_, _) => Task.FromResult(new SearchResultPageDto
            {
                Ok = false,
                ErrorCode = "TEST_FAILURE",
                ErrorMessage = "模拟搜索失败",
            });
            page.ErrorDialog = (_, _) => { };
            page.RunSearchAsync("关键词", 1).GetAwaiter().GetResult();

            Assert.Equal(Visibility.Collapsed, page.LoadingOverlayVisibilityForTest);
            Assert.Equal(55, page.LoadingAngleForTest, 3);
        });
    }
}
