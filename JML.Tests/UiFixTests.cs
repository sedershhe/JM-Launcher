using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using JML.Pages;
using Xunit;

namespace JML.Tests;

/// <summary>
/// 2026-10-18 用户实测三项 UI 修复的回归（ui-fix-agent）：
/// 1) 标题栏从静态 ColorBrush4 #4890F5 改成 PCL2 运行时主题蓝渐变（ModSecret.vb:114-120）；
/// 2) 内容区外层 Border 裁剪 + 标题栏 Panel.ZIndex=10，切页入场 Y=-16 时不压到标题栏；
/// 3) 按钮统一 PCL2 规格（主/次均 35/13，主按钮只是次按钮的别名样式），禁用文字 Gray3，
///    且没有写 Style 的按钮（PythonSetupPage）也落到次按钮样式。
/// 同时把关键“文字/背景”配对的 WCAG 对比度锁成下限，防止以后又调回会糊的组合。
/// </summary>
public class UiFixTests
{
    // ---- PCL2 默认主题色（ModSecret.vb:64，hue=210、sat=85%、lightAdjust=0） ----
    // 标题栏色走 PCL2 的 MyColor.FromHSL2（带按色相的亮度校正，ModBase.vb:365-389）：
    //   HSL2(210,85,48) → #106AC4，HSL2(210,85,54) → #1277DD
    // 普通 HSL 的 #127AE2 / #268AED 是上一版近似值，已弃用。
    private static readonly Color TitleStopDark = Color.FromRgb(0x10, 0x6A, 0xC4);  // HSL2(210,85,48)
    private static readonly Color TitleStopMid = Color.FromRgb(0x12, 0x77, 0xDD);   // HSL2(210,85,54)
    private static readonly Color SecondaryFill = Color.FromArgb(0x55, 0xFF, 0xFF, 0xFF);
    private static readonly Color CardSurface = Color.FromRgb(0xF5, 0xF5, 0xF5);    // #F5FFFFFF 叠白卡近似

    /// <summary>
    /// 标题栏背景：横向三停靠渐变、停靠 0/0.5/1、两端 #106AC4、中间 #1277DD
    /// （PCL2 MyColor.FromHSL2 的校正结果，由 Infrastructure\PclThemeColor.cs 生成）；
    /// ColorBrush4 资源本身必须仍是 #4890F5（还留给悬停/输入框用，不能顺手改掉）。
    /// </summary>
    [Fact]
    public void TitleBar_Background_IsPclRuntimeGradient()
    {
        UiSmokeTests.RunOnUiThread(() =>
        {
            EnsureLightTheme();
            var window = new MainWindow();
            try
            {
                var titleBar = Assert.IsType<Grid>(window.FindName("PanTitle"));
                var brush = Assert.IsType<LinearGradientBrush>(titleBar.Background);

                Assert.Equal(new Point(0, 0), brush.StartPoint);
                Assert.Equal(new Point(1, 0), brush.EndPoint);
                Assert.Equal(3, brush.GradientStops.Count);
                Assert.Equal(0.0, brush.GradientStops[0].Offset);
                Assert.Equal(0.5, brush.GradientStops[1].Offset);
                Assert.Equal(1.0, brush.GradientStops[2].Offset);
                Assert.Equal(TitleStopDark, brush.GradientStops[0].Color);
                Assert.Equal(TitleStopMid, brush.GradientStops[1].Color);
                Assert.Equal(TitleStopDark, brush.GradientStops[2].Color);

                // ColorBrush4 是 PCL2 XAML 的静态默认值，不能被这次改动顺手改掉。
                Assert.Equal(
                    Color.FromRgb(0x48, 0x90, 0xF5),
                    Assert.IsType<SolidColorBrush>(Application.Current.FindResource("ColorBrush4")).Color);
            }
            finally
            {
                window.Close();
            }
        });
    }

    /// <summary>
    /// 内容区裁剪与层级：
    /// MainContent 外层 Border ClipToBounds=true；标题栏 ZIndex=10 且严格大于内容区；
    /// 内页标题栏 InnerHeader 仍在 PanTitle 内部，因此同样在内容区之上。
    /// </summary>
    [Fact]
    public void ContentArea_Clipped_TitleBarAboveIt()
    {
        UiSmokeTests.RunOnUiThread(() =>
        {
            EnsureLightTheme();
            var window = new MainWindow();
            try
            {
                var clip = Assert.IsType<Border>(window.FindName("MainContentClip"));
                Assert.True(clip.ClipToBounds, "内容区外层 Border 必须 ClipToBounds=True（切页上滑不能画到标题栏）。");
                Assert.Equal(1, Grid.GetRow(clip));

                var content = Assert.IsType<ContentControl>(window.FindName("MainContent"));
                Assert.Same(clip, VisualTreeHelper.GetParent(content));

                var titleBar = Assert.IsType<Grid>(window.FindName("PanTitle"));
                Assert.Equal(10, Panel.GetZIndex(titleBar));
                Assert.True(
                    Panel.GetZIndex(titleBar) > Panel.GetZIndex(clip),
                    $"标题栏 ZIndex={Panel.GetZIndex(titleBar)} 必须大于内容区 ZIndex={Panel.GetZIndex(clip)}。");

                // 内页标题栏与主标题栏同层（都在 PanTitle 里），切内页时也不该被内容压住。
                var innerHeader = Assert.IsType<Grid>(window.FindName("InnerHeader"));
                var ancestor = VisualTreeHelper.GetParent(innerHeader);
                while (ancestor is not null && ancestor is not Grid { Name: "PanTitle" })
                {
                    ancestor = VisualTreeHelper.GetParent(ancestor);
                }

                Assert.NotNull(ancestor);
            }
            finally
            {
                window.Close();
            }
        });
    }

    /// <summary>
    /// 主/次按钮统一后的 PCL2 规格 + 禁用态文字色：
    /// 主按钮已 BasedOn 次按钮，35 高 / 13px / ColorBrush1 字与边框、#55FFFFFF 底；
    /// 次按钮同样 35 高 / 13px；禁用后 PanFore 底 Gray6、边框 Gray4，文字 Foreground=Gray3。
    /// </summary>
    [Fact]
    public void Buttons_FollowPclSpec_AndDisabledTextIsGray3()
    {
        UiSmokeTests.RunOnUiThread(() =>
        {
            EnsureLightTheme();
            var primary = new Button
            {
                Style = (Style)Application.Current.FindResource("PrimaryButton"),
                Content = "开始下载",
            };
            primary.Measure(new Size(200, 35));
            primary.Arrange(new Rect(0, 0, 200, 35));
            primary.UpdateLayout();
            Assert.Equal(35, primary.ActualHeight);
            Assert.Equal(13, primary.FontSize);
            Assert.Equal(ResourceColor("ColorBrush1"), Assert.IsType<SolidColorBrush>(primary.Foreground).Color);
            // 真正决定观感的是模板里那个 TextBlock：必须与 Foreground 同为 ColorBrush1。
            var primaryLabel = Assert.IsType<TextBlock>(primary.Template.FindName("LabText", primary));
            Assert.Equal(ResourceColor("ColorBrush1"), Assert.IsType<SolidColorBrush>(primaryLabel.Foreground).Color);

            var secondary = new Button
            {
                Style = (Style)Application.Current.FindResource("SecondaryButton"),
                Content = "浏览…",
            };
            secondary.Measure(new Size(200, 35));
            secondary.Arrange(new Rect(0, 0, 200, 35));
            secondary.UpdateLayout();
            Assert.Equal(35, secondary.ActualHeight);
            Assert.Equal(13, secondary.FontSize);
            Assert.Equal(ResourceColor("ColorBrush1"), Assert.IsType<SolidColorBrush>(secondary.Foreground).Color);

            var panFore = Assert.IsType<Border>(secondary.Template.FindName("PanFore", secondary));
            Assert.Equal(ResourceColor("ColorBrush1"), Assert.IsType<SolidColorBrush>(panFore.BorderBrush).Color);
            Assert.Equal(SecondaryFill, Assert.IsType<SolidColorBrush>(panFore.Background).Color);

            // 悬停底：浅色主题下 ColorBrushButtonHoverBg 与 ColorBrush7 同为 #E0EAFD。
            var hoverBg = Assert.IsType<Border>(secondary.Template.FindName("HoverBg", secondary));
            Assert.Equal(ResourceColor("ColorBrush7"), Assert.IsType<SolidColorBrush>(hoverBg.Background).Color);

            // 禁用：底 Gray6 / 边框 Gray4 / 文字 Gray3（旧模板文字跟边框一起变 Gray4，2.04:1 看不清）
            secondary.IsEnabled = false;
            secondary.UpdateLayout();
            Assert.Equal(ResourceColor("ColorBrushGray6"), Assert.IsType<SolidColorBrush>(panFore.Background).Color);
            Assert.Equal(ResourceColor("ColorBrushGray4"), Assert.IsType<SolidColorBrush>(panFore.BorderBrush).Color);
            Assert.Equal(ResourceColor("ColorBrushGray3"), Assert.IsType<SolidColorBrush>(secondary.Foreground).Color);
            var label = Assert.IsType<TextBlock>(secondary.Template.FindName("LabText", secondary));
            Assert.Equal(ResourceColor("ColorBrushGray3"), Assert.IsType<SolidColorBrush>(label.Foreground).Color);
        });
    }

    /// <summary>
    /// 主按钮与次按钮必须逐项一致（2026-10-18 按钮统一）：
    /// 样式层没有任何自己的 Setter（只 BasedOn 次按钮），实例上的
    /// Height / FontSize / Foreground / Background / BorderThickness / BorderBrush / Padding
    /// 与次按钮逐项相等，模板也是同一份。
    /// </summary>
    [Fact]
    public void PrimaryButton_MatchesSecondaryButton_ItemByItem()
    {
        UiSmokeTests.RunOnUiThread(() =>
        {
            EnsureLightTheme();
            var primaryStyle = Assert.IsType<Style>(Application.Current.FindResource("PrimaryButton"));
            var secondaryStyle = Assert.IsType<Style>(Application.Current.FindResource("SecondaryButton"));

            Assert.Same(secondaryStyle, primaryStyle.BasedOn);
            Assert.Empty(primaryStyle.Setters);

            var primary = MakeButton("开始下载", primaryStyle);
            var secondary = MakeButton("浏览…", secondaryStyle);

            Assert.Equal(secondary.Height, primary.Height);
            Assert.Equal(secondary.FontSize, primary.FontSize);
            Assert.Equal(secondary.BorderThickness, primary.BorderThickness);
            Assert.Equal(secondary.Padding, primary.Padding);
            Assert.Equal(ColorOf(secondary.Foreground), ColorOf(primary.Foreground));
            Assert.Equal(ColorOf(secondary.Background), ColorOf(primary.Background));
            Assert.Equal(ColorOf(secondary.BorderBrush), ColorOf(primary.BorderBrush));
            Assert.Equal(secondary.ActualHeight, primary.ActualHeight);
            Assert.Equal(35, primary.ActualHeight);

            // 模板同一份：圆角、悬停层、禁用触发器不会再分叉。
            Assert.Same(secondary.Template, primary.Template);
        });
    }

    private static Button MakeButton(string content, Style style)
    {
        var button = new Button { Content = content, Style = style };
        button.Measure(new Size(200, 35));
        button.Arrange(new Rect(0, 0, 200, 35));
        button.UpdateLayout();
        return button;
    }

    private static Color ColorOf(Brush brush) =>
        Assert.IsType<SolidColorBrush>(brush).Color;

    /// <summary>
    /// 没写 Style 的按钮（PythonSetupPage 的「重新检测」「进入主界面」）必须落到次按钮样式：
    /// 35 高、13px、ColorBrush1 前景——不再落到外部库的透明底 PlainButton 默认外观。
    /// 「进入主界面」在 XAML 里是禁用占位，文字随之变 Gray3（对比度修正）。
    /// </summary>
    [Fact]
    public void UnstyledButtons_UseSecondaryButton_ByImplicitStyle()
    {
        UiSmokeTests.RunOnUiThread(() =>
        {
            EnsureLightTheme();
            var implicitStyle = Assert.IsType<Style>(Application.Current.FindResource(typeof(Button)));
            Assert.Same(Application.Current.FindResource("SecondaryButton"), implicitStyle.BasedOn);

            var page = new PythonSetupPage();
            var retry = Assert.IsType<Button>(page.FindName("BtnRetry"));
            Assert.Same(implicitStyle, retry.Style);
            Assert.Equal(35, retry.Height);
            Assert.Equal(13, retry.FontSize);
            Assert.Equal(ResourceColor("ColorBrush1"), Assert.IsType<SolidColorBrush>(retry.Foreground).Color);

            var cont = Assert.IsType<Button>(page.FindName("BtnContinue"));
            Assert.Same(implicitStyle, cont.Style);
            Assert.Equal(35, cont.Height);
            Assert.Equal(13, cont.FontSize);
            Assert.False(cont.IsEnabled);
            Assert.Equal(ResourceColor("ColorBrushGray3"), Assert.IsType<SolidColorBrush>(cont.Foreground).Color);
        });
    }

    /// <summary>
    /// 关键配对的 WCAG 对比度下限（“会糊”的组合直接锁死）：
    /// 按钮启用 10.3:1、按钮禁用 Gray3/Gray6 2.8:1（旧 Gray4/Gray6 仅 2.0:1）、
    /// 调色板参照 ColorBrush8/ColorBrush3 4.0:1、ColorBrush8/ColorBrush2 5.5:1、
    /// 导航选中 ColorBrush3/白 4.5:1。
    /// 禁用态不靠加粗、不改外部库；启用态文字全部高对比。
    /// </summary>
    [Fact]
    public void ButtonColorPairs_MeetContrastFloors()
    {
        UiSmokeTests.RunOnUiThread(() =>
        {
            EnsureLightTheme();
            Color brush1 = ResourceColor("ColorBrush1");
            Color brush3 = ResourceColor("ColorBrush3");
            Color brush8 = ResourceColor("ColorBrush8");
            Color gray3 = ResourceColor("ColorBrushGray3");
            Color gray4 = ResourceColor("ColorBrushGray4");
            Color gray6 = ResourceColor("ColorBrushGray6");
            Color brush2 = ResourceColor("ColorBrush2");
            Color white = ResourceColor("ColorBrushWhite");

            // 次按钮（现在也是主按钮）底色 #55FFFFFF 叠在白卡 #F5FFFFFF 上 ≈ #F8F8F8。
            Color secondaryEnabled = Composite(SecondaryFill, CardSurface);
            Assert.True(
                Contrast(brush1, secondaryEnabled) >= 7.0,
                $"按钮启用态 {Contrast(brush1, secondaryEnabled):0.00}:1，期望 ≥ 7:1。");
            Assert.True(
                Contrast(gray3, gray6) >= 2.7,
                $"按钮禁用态 {Contrast(gray3, gray6):0.00}:1，期望 ≥ 2.7:1（旧 Gray4 只有 2.04:1）。");
            // ColorBrush8/ColorBrush3 与 ColorBrush8/ColorBrush2 仍作为调色板配对留作参照，
            // 但主按钮已不再使用这两个组合（现与次按钮同为 ColorBrush1 on #55FFFFFF）。
            Assert.True(
                Contrast(brush8, brush3) >= 4.0,
                $"调色板参照 ColorBrush8/ColorBrush3 {Contrast(brush8, brush3):0.00}:1，期望 ≥ 4:1。");
            Assert.True(
                Contrast(brush8, brush2) >= 4.5,
                $"调色板参照 ColorBrush8/ColorBrush2 {Contrast(brush8, brush2):0.00}:1，期望 ≥ 4.5:1（旧 ColorBrush4 悬停仅 2.83:1）。");
            Assert.True(
                Contrast(brush3, white) >= 4.5,
                $"导航选中态 {Contrast(brush3, white):0.00}:1，期望 ≥ 4.5:1。");

            // 旧禁用组合确实是“糊”的，作为回归参照（防止有人把 Gray3 又改回 Gray4）；
            // 旧主按钮曾被外部库隐式 TextBlock 样式逼成 ColorBrush1 字，这里一起锁住。
            Assert.True(Contrast(gray4, gray6) < 2.5, "Gray4 on Gray6 应低于 2.5:1，证明禁用文字必须改深。");
            Assert.True(
                Contrast(brush1, brush3) < 2.5,
                "ColorBrush1 字压在 ColorBrush3 主按钮上应低于 2.5:1，证明主按钮文字不能用继承深灰。");
        });
    }

    // ------------------------------------------------------------------

    /// <summary>
    /// 测试用的 Application 只合并了 Generic.xaml（默认 ColorsDark 生效），
    /// 生产路径 App.OnStartup 会切浅色；这里先跑一遍同一条生产路径，颜色断言才稳定。
    /// </summary>
    private static void EnsureLightTheme() =>
        JML.App.ApplyPclLightTheme(Application.Current.Resources);

    private static Color ResourceColor(string key) =>
        Assert.IsType<SolidColorBrush>(Application.Current.FindResource(key)).Color;

    private static Color Composite(Color foreground, Color background)
    {
        double a = foreground.A / 255.0;
        return Color.FromRgb(
            (byte)Math.Round(foreground.R * a + background.R * (1 - a)),
            (byte)Math.Round(foreground.G * a + background.G * (1 - a)),
            (byte)Math.Round(foreground.B * a + background.B * (1 - a)));
    }

    private static double Contrast(Color a, Color b)
    {
        double la = RelativeLuminance(a);
        double lb = RelativeLuminance(b);
        double hi = Math.Max(la, lb);
        double lo = Math.Min(la, lb);
        return (hi + 0.05) / (lo + 0.05);
    }

    private static double RelativeLuminance(Color color) =>
        (0.2126 * Channel(color.R)) + (0.7152 * Channel(color.G)) + (0.0722 * Channel(color.B));

    private static double Channel(byte value)
    {
        double c = value / 255.0;
        return c <= 0.04045 ? c / 12.92 : Math.Pow((c + 0.055) / 1.055, 2.4);
    }
}

