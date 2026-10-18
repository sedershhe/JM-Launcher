using System.Windows;
using System.Windows.Media;

namespace JML.Infrastructure;

/// <summary>
/// PCL2 主题色算法的 C# 复刻（纯函数，便于单测锁定色值）。
///
/// 来源：PCL2 仓库 “Plain Craft Launcher 2\Modules\Base\ModBase.vb:365-389” 的
/// <c>MyColor.FromHSL2</c>，以及它内部调用的 ModBase.vb:340-364（<c>Hue</c> / <c>FromHSL</c>）。
///
/// FromHSL2 与普通 HSL 的区别：先按“色相所在片区”做一次亮度校正，再走普通 HSL 转 RGB。
/// 公式（h 单位度，s / l 单位百分比）：
/// <code>
///   cent = { +0.1, -0.06, -0.3, -0.19, -0.15, -0.24, -0.32, -0.09, +0.18, +0.05, -0.12, -0.02, +0.1, -0.06 }
///           //  索引 i 对应色相 30i；末尾两位与开头一致，保证 330°~360° 插值不越界
///   centerIdx = H / 30;   i = floor(centerIdx);   f = centerIdx - i
///   corr   = (1 - f) * cent[i] + f * cent[i + 1]
///   center = 50 - corr * S
///   L'     = if (L &lt; center) L / center * 50 else (1 + (L - center) / (100 - center)) * 50
/// </code>
/// 最后按普通 HSL(H, S, L') 转 RGB（PCL 的 Hue/FromHSL 与标准 HSL 等价，见下方 <see cref="Hue"/>）。
///
/// 默认主题 H=210、S=85（ModSecret.vb:64 的 ColorHue=210 / ColorSat=85）：
///   L=48 → L'≈41.6305 → #106AC4
///   L=54 → L'≈46.8343 → #1277DD
/// 这正是 PCL2 标题栏运行时三停靠渐变（ModSecret.vb:114-120，Offset 0 / 0.5 / 1，
/// 色为 HSL(...,48) / HSL(...,54) / HSL(...,48)）的真实颜色；
/// 普通 HSL(210,85,48/54) 会得到 #127AE2 / #268AED，那不是 PCL2 的实际观感。
/// </summary>
public static class PclThemeColor
{
    /// <summary>PCL2 默认主题色相（ModSecret.vb:64 ColorHue）。</summary>
    public const double DefaultHue = 210;

    /// <summary>PCL2 默认主题饱和度百分比（ModSecret.vb:64 ColorSat）。</summary>
    public const double DefaultSaturation = 85;

    /// <summary>标题栏渐变两端的亮度（ModSecret.vb:114-120 的 HSL(...,48)）。</summary>
    public const double TitleBarLightEdge = 48;

    /// <summary>标题栏渐变中点的亮度（ModSecret.vb:114-120 的 HSL(...,54)）。</summary>
    public const double TitleBarLightCenter = 54;

    /// <summary>
    /// 逐色相的亮度校正表（ModBase.vb:374-379 原值，共 14 项）。
    /// 索引 i 对应色相 30i；最后两项与开头一致，供 330°~360° 插值使用。
    /// </summary>
    private static readonly double[] CenterOffsets =
    {
        +0.10, -0.06, -0.30, //   0°,  30°,  60°
        -0.19, -0.15, -0.24, //  90°, 120°, 150°
        -0.32, -0.09, +0.18, // 180°, 210°, 240°
        +0.05, -0.12, -0.02, // 270°, 300°, 330°
        +0.10, -0.06,        // 360°/0°、390°/30°
    };

    /// <summary>
    /// PCL2 <c>MyColor.FromHSL2</c> 的等价实现：带色相亮度校正的 HSL → RGB。
    /// </summary>
    /// <param name="h">色相，0~360 度（自动规范到 [0,360)）。</param>
    /// <param name="s">饱和度百分比 0~100。</param>
    /// <param name="l">亮度百分比 0~100。</param>
    public static Color FromHsl2(double h, double s, double l)
    {
        // ModBase.vb:367-370：饱和度为 0 时直接按灰度处理（原实现 sL * 2.55）。
        if (s == 0)
        {
            byte gray = ClampToByte(l * 2.55);
            return Color.FromRgb(gray, gray, gray);
        }

        // ModBase.vb:373：sH = (sH + 3600000) Mod 360
        h = (h + 3_600_000) % 360;

        // ModBase.vb:381-385：按色相找亮度片区并插值校正量。
        double centerIndex = h / 30.0;
        int index = (int)Math.Floor(centerIndex);
        double fraction = centerIndex - index;
        double correction =
            ((1 - fraction) * CenterOffsets[index]) + (fraction * CenterOffsets[index + 1]);
        double center = 50 - (correction * s);

        // ModBase.vb:386：把 L 映射到校正后的亮度（以 50 为基准分段线性）。
        double correctedLight = l < center
            ? l / center * 50
            : (1 + ((l - center) / (100 - center))) * 50;

        return FromHsl(h, s, correctedLight);
    }

    /// <summary>
    /// PCL2 <c>MyColor.FromHSL</c>（ModBase.vb:348-364）：普通 HSL → RGB。
    /// PCL 用的是自研 <see cref="Hue"/> 三分量写法，与标准 HSL 等价；这里照抄以保持逐位一致。
    /// </summary>
    public static Color FromHsl(double h, double s, double l)
    {
        if (s == 0)
        {
            byte gray = ClampToByte(l * 2.55);
            return Color.FromRgb(gray, gray, gray);
        }

        double hue = h / 360.0;
        double saturation = s / 100.0;
        double light = l / 100.0;

        // ModBase.vb:358-359
        saturation = light < 0.5
            ? (saturation * light) + light
            : (saturation * (1.0 - light)) + light;
        light = (2 * light) - saturation;

        // ModBase.vb:360-362：R/G/B 分别取 Hue(H + 1/3, H, H - 1/3)。
        byte r = ClampToByte(255 * Hue(light, saturation, hue + (1.0 / 3)));
        byte g = ClampToByte(255 * Hue(light, saturation, hue));
        byte b = ClampToByte(255 * Hue(light, saturation, hue - (1.0 / 3)));

        return Color.FromRgb(r, g, b);
    }

    /// <summary>
    /// 标题栏运行时背景（PCL2 ModSecret.vb:114-120）：
    /// 横向 LinearGradientBrush StartPoint=(0,0) / EndPoint=(1,0)，三停靠 Offset=0 / 0.5 / 1，
    /// 色为 FromHsl2(210,85,48) / FromHsl2(210,85,54) / FromHsl2(210,85,48)。
    /// 默认主题下即 #106AC4 / #1277DD / #106AC4。
    /// </summary>
    public static LinearGradientBrush CreateTitleBarBrush()
    {
        var brush = new LinearGradientBrush
        {
            StartPoint = new Point(0, 0),
            EndPoint = new Point(1, 0),
            GradientStops =
            {
                new GradientStop(
                    FromHsl2(DefaultHue, DefaultSaturation, TitleBarLightEdge), 0),
                new GradientStop(
                    FromHsl2(DefaultHue, DefaultSaturation, TitleBarLightCenter), 0.5),
                new GradientStop(
                    FromHsl2(DefaultHue, DefaultSaturation, TitleBarLightEdge), 1),
            },
        };

        // 共享给 Grid.Background，不需要再改色；冻结可跨线程/减少渲染开销。
        brush.Freeze();
        return brush;
    }

    /// <summary>
    /// PCL2 <c>MyColor.Hue</c>（ModBase.vb:340-347）：HSL 三分量里取一个通道。
    /// 注意 PCL 用 0.16667 / 0.66667 近似 1/6、2/3，这里保持原值以得到同一位舍入结果。
    /// </summary>
    private static double Hue(double v1, double v2, double vH)
    {
        if (vH < 0)
        {
            vH += 1;
        }

        if (vH > 1)
        {
            vH -= 1;
        }

        if (vH < 0.16667)
        {
            return v1 + ((v2 - v1) * 6 * vH);
        }

        if (vH < 0.5)
        {
            return v2;
        }

        if (vH < 0.66667)
        {
            return v1 + ((v2 - v1) * (4 - (vH * 6)));
        }

        return v1;
    }

    /// <summary>PCL2 <c>MyColor.ClampToByte</c>（ModBase.vb:249-253）：四舍五入并夹到 0~255。</summary>
    private static byte ClampToByte(double value) =>
        (byte)Math.Round(Math.Clamp(value, 0, 255));
}
