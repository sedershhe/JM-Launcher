using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using JML.Infrastructure;
using JML.Pages;
using Xunit;

namespace JML.Tests;

/// <summary>
/// 2026-10-18 第二轮 UI 修复回归（pcl-color-fix-agent）：
/// 1) 标题栏从普通 HSL 近似色改成 PCL2 的 MyColor.FromHSL2（按色相做亮度校正），
///    色值由 Infrastructure\PclThemeColor.cs 生成，不在 XAML 里写死；
/// 2) 侧栏项（SideListItem）文字改成显式 TextBlock + TemplateBinding Foreground，
///    选中态 ColorBrush3 才能真正渲染（外部库隐式 TextBlock 样式会把前景压成 ColorBrush1）。
/// </summary>
public class PclThemeColorTests
{
    // ---- PCL2 默认主题：hue=210 / sat=85（ModSecret.vb:64） ----
    private const double Hue = 210;
    private const double Saturation = 85;

    /// <summary>普通 HSL 的旧近似值，作为“没有走 HSL2”的反例。</summary>
    private static readonly Color OrdinaryHslDark = Color.FromRgb(0x12, 0x7A, 0xE2); // HSL(210,85,48)
    private static readonly Color OrdinaryHslMid = Color.FromRgb(0x26, 0x8A, 0xED);  // HSL(210,85,54)

    /// <summary>
    /// FromHSL2 核心算法锁定（ModBase.vb:365-389）：
    /// 默认主题 L=48 → #106AC4、L=54 → #1277DD。
    /// 允许每通道 ±1 的四舍五入误差；同时证明它与普通 HSL 的旧近似值不是同一结果。
    /// </summary>
    [Theory]
    [InlineData(48, 0x10, 0x6A, 0xC4)]
    [InlineData(54, 0x12, 0x77, 0xDD)]
    public void FromHsl2_DefaultTheme_MatchesPcl2Colors(double light, int r, int g, int b)
    {
        Color color = PclThemeColor.FromHsl2(Hue, Saturation, light);

        Assert.Equal(255, color.A);
        Assert.InRange(color.R, r - 1, r + 1);
        Assert.InRange(color.G, g - 1, g + 1);
        Assert.InRange(color.B, b - 1, b + 1);
    }

    /// <summary>
    /// 带色相亮度校正的证据：普通 HSL(210,85,48/54) 是 #127AE2 / #268AED，
    /// HSL2 校正后是 #106AC4 / #1277DD（明显更深）。若有人把算法改回普通 HSL，这条会失败。
    /// </summary>
    [Fact]
    public void FromHsl2_IsNotOrdinaryHsl()
    {
        Assert.NotEqual(OrdinaryHslDark, PclThemeColor.FromHsl2(Hue, Saturation, 48));
        Assert.NotEqual(OrdinaryHslMid, PclThemeColor.FromHsl2(Hue, Saturation, 54));

        // 色相跨 0°/360° 时也要稳定（ModBase.vb:373 的 (H + 3600000) Mod 360）。
        Assert.Equal(
            PclThemeColor.FromHsl2(Hue, Saturation, 48),
            PclThemeColor.FromHsl2(Hue + 360, Saturation, 48));
    }

    /// <summary>
    /// 标题栏实际渲染的渐变：停靠 0 / 0.5 / 1，颜色逐项等于 FromHsl2(210,85,48/54/48)，
    /// 且背景对象来自 PclThemeColor.CreateTitleBarBrush（构造时冻结，XAML 硬编码的渐变不会冻结）。
    /// </summary>
    [Fact]
    public void TitleBar_Background_StopsComeFromFromHsl2()
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

                Color edge = PclThemeColor.FromHsl2(Hue, Saturation, 48);
                Color center = PclThemeColor.FromHsl2(Hue, Saturation, 54);
                Assert.Equal(edge, brush.GradientStops[0].Color);
                Assert.Equal(center, brush.GradientStops[1].Color);
                Assert.Equal(edge, brush.GradientStops[2].Color);

                // 反例：旧普通 HSL 色值不能再出现。
                Assert.NotEqual(OrdinaryHslDark, brush.GradientStops[0].Color);
                Assert.NotEqual(OrdinaryHslMid, brush.GradientStops[1].Color);

                // CreateTitleBarBrush() 会 Freeze；XAML 里手写的 LinearGradientBrush 不会。
                Assert.True(
                    brush.IsFrozen,
                    "标题栏背景必须由 PclThemeColor.CreateTitleBarBrush() 生成（背景对象应为冻结画刷）。");
            }
            finally
            {
                window.Close();
            }
        });
    }

    /// <summary>
    /// 侧栏项文字：模板里的 TextBlock 必须真正跟随 RadioButton.Foreground。
    /// 选中 = ColorBrush3 #1370F3，未选中 = ColorBrush1 #343D4A。
    /// 旧模板用 ContentPresenter，外部库隐式 TextBlock 样式把前景压成 ColorBrush1，选中态看不出来。
    /// </summary>
    [Fact]
    public void SideListItem_TextForeground_FollowsCheckedState()
    {
        UiSmokeTests.RunOnUiThread(() =>
        {
            EnsureLightTheme();

            var item = new RadioButton
            {
                Style = (Style)Application.Current.FindResource("SideListItem"),
                Content = "插件管理",
                IsChecked = true,
            };

            Layout(item, 160, 36);

            var label = Assert.IsType<TextBlock>(item.Template.FindName("LabText", item));
            Assert.Equal("插件管理", label.Text);
            Assert.Equal(
                ResourceColor("ColorBrush3"),
                Assert.IsType<SolidColorBrush>(label.Foreground).Color);

            // 未选中：模板文字回到样式里的 ColorBrush1。
            item.IsChecked = false;
            item.UpdateLayout();
            Assert.Equal(
                ResourceColor("ColorBrush1"),
                Assert.IsType<SolidColorBrush>(item.Foreground).Color);
            Assert.Equal(
                ResourceColor("ColorBrush1"),
                Assert.IsType<SolidColorBrush>(label.Foreground).Color);
        });
    }

    /// <summary>
    /// 用真实插件页左栏验证：默认选中的「插件管理」文字是 ColorBrush3，
    /// 未选中的「插件功能」是 ColorBrush1（不依赖手工构造的 RadioButton）。
    /// </summary>
    [Fact]
    public void PluginHubSidebar_SelectedItemText_IsColorBrush3()
    {
        UiSmokeTests.RunOnUiThread(() =>
        {
            EnsureLightTheme();

            var hub = new PluginHubPage();
            Layout(hub);

            var manage = UiSmokeTests.Find<RadioButton>(hub, "TabManage");
            var features = UiSmokeTests.Find<RadioButton>(hub, "TabFeatures");

            Assert.True(manage.IsChecked, "插件页默认应选中「插件管理」。");
            Assert.True(features.IsChecked is false or null, "「插件功能」默认不应选中。");

            var manageText = Assert.IsType<TextBlock>(manage.Template.FindName("LabText", manage));
            var featuresText = Assert.IsType<TextBlock>(features.Template.FindName("LabText", features));

            Assert.Equal("插件管理", manageText.Text);
            Assert.Equal("插件功能", featuresText.Text);
            Assert.Equal(
                ResourceColor("ColorBrush3"),
                Assert.IsType<SolidColorBrush>(manageText.Foreground).Color);
            Assert.Equal(
                ResourceColor("ColorBrush1"),
                Assert.IsType<SolidColorBrush>(featuresText.Foreground).Color);
        });
    }

    /// <summary>
    /// 分组标题（SideGroupTitle）复查：显式 Style 的 Foreground=ColorBrush1 + Opacity=0.6，
    /// 与外部库隐式 TextBlock 样式同色（ColorBrush1），只是额外半透明——属 PCL2 原设定，无需修改。
    /// </summary>
    [Fact]
    public void SideGroupTitle_ForegroundIsColorBrush1_NotSuppressed()
    {
        UiSmokeTests.RunOnUiThread(() =>
        {
            EnsureLightTheme();

            // 先证明外部库隐式 TextBlock 样式确实把普通 TextBlock 的前景压成 ColorBrush1（深色）。
            var host = new Grid();
            var plain = new TextBlock { Text = "x" };
            host.Children.Add(plain);
            Layout(host, 120, 30);
            Assert.Equal(
                ResourceColor("ColorBrush1"),
                Assert.IsType<SolidColorBrush>(plain.Foreground).Color);

            // 分组标题的显式 Style 优先于隐式样式，且显式写了 ColorBrush1 + 0.6 —— 与 PCL2 原设定一致，
            // 不存在“选中态该变蓝却仍深色”的问题（它本来就不随选中态变色）。
            var title = new TextBlock
            {
                Text = "插件",
                Style = (Style)Application.Current.FindResource("SideGroupTitle"),
            };

            Assert.Equal(
                ResourceColor("ColorBrush1"),
                Assert.IsType<SolidColorBrush>(title.Foreground).Color);
            Assert.Equal(0.6, title.Opacity, 3);
        });
    }

    // ------------------------------------------------------------------

    private static void EnsureLightTheme() =>
        JML.App.ApplyPclLightTheme(Application.Current.Resources);

    private static Color ResourceColor(string key) =>
        Assert.IsType<SolidColorBrush>(Application.Current.FindResource(key)).Color;

    private static void Layout(FrameworkElement root, double width = 1080, double height = 600)
    {
        root.Measure(new Size(width, height));
        root.Arrange(new Rect(0, 0, width, height));
        root.UpdateLayout();
    }
}
