using System.Threading;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Media.Effects;
using System.Windows.Shapes;
using System.Windows.Threading;
using JML.Bridge.Dto;
using JML.Controls;
using JML.Pages;
using Xunit;
using Xunit.Sdk;

namespace JML.Tests;

/// <summary>
/// 界面自检（阶段 2-C）：
/// 1) 在 STA 线程构造下载页 / 搜索页 / 详情窗，证明 XAML 能真正加载（资源字典解析、控件树生成、事件绑定都不抛异常）；
/// 2) 用 Measure/Arrange 量化验证"卡片内容铺满可用宽度"这类只有真机才暴露的布局缺陷
///    （PlainCard 的 ContentPresenter 跟随 HorizontalContentAlignment 默认值 Left，UI-1/2/3 同一根因）。
///
/// 不引入 <c>StaFact</c> 等新包：手动开 STA 线程即可。
/// 一个 AppDomain 只能有一个 Application，因此所有用例共用同一条 UI 线程与同一个 Application。
/// 构造过程不触发任何 Python 调用（详情窗只在 Loaded 后才拉数据，测试不 Show）。
/// </summary>
public class UiSmokeTests
{
    /// <summary>布局验收用的逻辑尺寸（DIP；真机 1620×1080 物理像素 / 150% 缩放 ≈ 1080×720 窗口）。</summary>
    private const double LayoutWidth = 1080;

    /// <summary>布局验收用的可用高度（主窗口内容区高度量级）。</summary>
    private const double LayoutHeight = 600;

    /// <summary>
    /// 共用的 UI 线程：Application 只能创建一次，所以整条线程常驻并跑 Dispatcher。
    /// </summary>
    private static readonly Lazy<Dispatcher> UiDispatcher =
        new(CreateUiThread, LazyThreadSafetyMode.ExecutionAndPublication);

    /// <summary>
    /// 在 UI 线程上执行一段断言；异常（含断言失败）原样抛回调用方。
    /// </summary>
    private static void RunOnUiThread(Action action)
    {
        Exception? failure = null;
        using var done = new ManualResetEventSlim(false);

        UiDispatcher.Value.BeginInvoke(new Action(() =>
        {
            try
            {
                action();
            }
            catch (Exception ex)
            {
                failure = ex;
            }
            finally
            {
                done.Set();
            }
        }));

        Assert.True(done.Wait(TimeSpan.FromSeconds(60)), "界面线程执行超时（可能卡在资源加载）。");

        if (failure is not null)
        {
            throw failure;
        }
    }

    /// <summary>
    /// 启动 STA 线程、创建唯一的 Application 并加载主题资源，然后把 Dispatcher 交回测试线程。
    /// </summary>
    private static Dispatcher CreateUiThread()
    {
        var ready = new TaskCompletionSource<Dispatcher>(TaskCreationOptions.RunContinuationsAsynchronously);
        var thread = new Thread(() =>
        {
            try
            {
                var app = new Application { ShutdownMode = ShutdownMode.OnExplicitShutdown };
                app.Resources.MergedDictionaries.Add(new ResourceDictionary
                {
                    Source = new Uri(
                        "pack://application:,,,/PlainToolkit.UI;component/Themes/Generic.xaml"),
                });
                app.Resources.MergedDictionaries.Add(new ResourceDictionary
                {
                    Source = new Uri(
                        "pack://application:,,,/JML;component/Controls/JmlControls.xaml"),
                });

                ready.SetResult(Dispatcher.CurrentDispatcher);
            }
            catch (Exception ex)
            {
                ready.SetException(ex);
                return;
            }

            Dispatcher.Run();
        });

        thread.SetApartmentState(ApartmentState.STA);
        thread.IsBackground = true;
        thread.Start();

        Assert.True(ready.Task.Wait(TimeSpan.FromSeconds(30)), "界面线程启动超时（主题资源加载失败？）。");
        return ready.Task.Result;
    }

    /// <summary>
    /// 对独立元素跑一轮完整的 Measure → Arrange → UpdateLayout，模拟真机上被放进内容区后的布局。
    /// </summary>
    private static void Layout(FrameworkElement root, double width = LayoutWidth, double height = LayoutHeight)
    {
        root.Measure(new Size(width, height));
        root.Arrange(new Rect(0, 0, width, height));
        root.UpdateLayout();
    }

    /// <summary>按名字取控件；取不到直接失败，并说明是哪个根元素。</summary>
    private static T Find<T>(FrameworkElement root, string name) where T : class
    {
        object? found = root.FindName(name);
        if (found is not T typed)
        {
            throw new XunitException($"{root.GetType().Name} 中找不到 {name}（期望类型 {typeof(T).Name}）。");
        }

        return typed;
    }

    /// <summary>沿逻辑树向上找最近的指定类型祖先（用于从空结果文字定位它所在的结果卡片）。</summary>
    private static T FindAncestor<T>(DependencyObject child) where T : DependencyObject
    {
        DependencyObject? current = child;
        while (current is not null)
        {
            if (current is T match)
            {
                return match;
            }

            current = (current as FrameworkElement)?.Parent;
        }

        throw new XunitException($"找不到 {typeof(T).Name} 祖先。");
    }

    /// <summary>读取 Style 的一个直接 Setter 值（外壳参数用，避免把颜色/尺寸硬编码进测试）。</summary>
    private static T GetStyleSetter<T>(Style style, DependencyProperty property)
    {
        foreach (SetterBase setterBase in style.Setters)
        {
            if (setterBase is Setter setter && setter.Property == property)
            {
                if (setter.Value is T typed)
                {
                    return typed;
                }

                throw new XunitException(
                    $"样式 Setter {property.Name} 的值类型为 {setter.Value?.GetType().Name ?? "null"}，期望 {typeof(T).Name}。");
            }
        }

        throw new XunitException($"样式里找不到 Setter：{property.Name}。");
    }

    /// <summary>按主题资源取颜色，作为“不许硬编码色值”的对照基准。</summary>
    private static Color ResourceColor(string key)
    {
        var brush = Assert.IsType<SolidColorBrush>(Application.Current.FindResource(key));
        return brush.Color;
    }

    [Fact]
    public void Pages_Construct_OnStaThread_WithAppResources()
    {
        RunOnUiThread(() =>
        {
            // 1) 下载页
            var download = new DownloadPage();
            Assert.NotNull(download);
            Assert.IsType<TextBox>(download.FindName("InputIds"));
            Assert.IsType<TextBox>(download.FindName("InputDir"));
            Assert.IsType<Button>(download.FindName("BtnStart"));
            Assert.IsType<Button>(download.FindName("BtnCancel"));
            Assert.IsType<TextBox>(download.FindName("LogBox"));
            Assert.IsType<TextBlock>(download.FindName("TextStatus"));

            // 默认下载目录必须是桌面（计划书 11.6），且不得为空。
            var dirBox = (TextBox)download.FindName("InputDir");
            Assert.False(string.IsNullOrWhiteSpace(dirBox.Text));

            // 2) 搜索页
            var search = new SearchPage();
            Assert.NotNull(search);
            Assert.IsType<TextBox>(search.FindName("KeywordBox"));
            Assert.IsType<Button>(search.FindName("BtnSearch"));
            Assert.IsType<ListBox>(search.FindName("ResultList"));
            Assert.IsType<TextBlock>(search.FindName("TextEmpty"));
            Assert.IsType<TextBlock>(search.FindName("TextPage"));
            Assert.IsType<Button>(search.FindName("BtnPrev"));
            Assert.IsType<Button>(search.FindName("BtnNext"));

            // 3) 详情窗（不 Show，避免 Loaded 触发 Python 调用）
            var detail = new DetailWindow("123456");
            Assert.NotNull(detail);
            Assert.IsType<Button>(detail.FindName("BtnDownload"));
            Assert.IsType<TextBox>(detail.FindName("LogBox"));
            Assert.IsType<TextBlock>(detail.FindName("TextError"));
            Assert.IsType<TextBlock>(detail.FindName("TextTitle"));
            detail.Close();
        });
    }

    /// <summary>
    /// UI-1：搜索页 KeywordBox 必须横向铺满可用宽度。
    /// 修复前实测（1080×600 布局）：16×36（真机 UIA：531,391,16,54 物理像素）。
    /// </summary>
    [Fact]
    public void SearchKeywordBox_FillsCardWidth()
    {
        RunOnUiThread(() =>
        {
            var search = new SearchPage();
            Layout(search);

            var keyword = Find<TextBox>(search, "KeywordBox");
            var card = FindAncestor<ContentControl>(keyword);

            Assert.True(
                keyword.ActualWidth > 400,
                $"UI-1 KeywordBox 实测 {keyword.ActualWidth:0.#}×{keyword.ActualHeight:0.#}，" +
                $"所在卡片实测 {card.ActualWidth:0.#}×{card.ActualHeight:0.#}；" +
                "期望宽度 > 400（搜索框随窗口铺满，输入文字才显现属缺陷）。");
        });
    }

    /// <summary>
    /// UI-2（下载页）：日志框必须铺满所在卡片。
    /// 修复前实测（1080×600 布局）：175×240 物理像素 ≈ 116.7×160 DIP 的窄条。
    /// </summary>
    [Fact]
    public void DownloadLogBox_FillsCard()
    {
        RunOnUiThread(() =>
        {
            var download = new DownloadPage();
            Layout(download);

            var logBox = Find<TextBox>(download, "LogBox");
            var card = FindAncestor<ContentControl>(logBox);

            Assert.True(
                logBox.ActualWidth > 400 && logBox.ActualHeight > 200,
                $"UI-2 下载页 LogBox 实测 {logBox.ActualWidth:0.#}×{logBox.ActualHeight:0.#}，" +
                $"所在卡片实测 {card.ActualWidth:0.#}×{card.ActualHeight:0.#}；" +
                "期望 > 400×> 200（铺满卡片）。");
        });
    }

    /// <summary>
    /// UI-2（详情窗）：日志框必须横向铺满卡片（该行固定 190，扣掉卡片内边距后可用高约 114）。
    /// </summary>
    [Fact]
    public void DetailLogBox_FillsCard()
    {
        RunOnUiThread(() =>
        {
            var detail = new DetailWindow("123456");
            try
            {
                // Window 在 Show 之前的布局由内容根决定，直接量内容根。
                Layout((FrameworkElement)detail.Content);

                var logBox = Find<TextBox>(detail, "LogBox");
                var card = FindAncestor<ContentControl>(logBox);

                Assert.True(logBox.ActualWidth > 400,
                    $"UI-2 详情窗 LogBox 实测 {logBox.ActualWidth:0.#}×{logBox.ActualHeight:0.#}，" +
                    $"所在卡片实测 {card.ActualWidth:0.#}×{card.ActualHeight:0.#}；期望宽度 > 400。");
                Assert.True(logBox.ActualHeight > 100,
                    $"UI-2 详情窗 LogBox 高度实测 {logBox.ActualHeight:0.#}（日志区行高 190，可用高约 114）；期望 > 100。");
            }
            finally
            {
                detail.Close();
            }
        });
    }

    /// <summary>
    /// UI-3：空结果文字必须在结果区内水平、垂直居中。
    /// 修复前：结果卡片收缩成内容宽度并靠左靠上，"未找到相关结果"贴左上角。
    /// </summary>
    [Fact]
    public void EmptyResultText_IsCenteredInResultCard()
    {
        RunOnUiThread(() =>
        {
            var search = new SearchPage();
            Layout(search);

            var empty = Find<TextBlock>(search, "TextEmpty");
            var card = FindAncestor<ContentControl>(empty);

            // 空结果状态是运行时才切出来的，这里手动切可见后重新布局。
            empty.Visibility = Visibility.Visible;
            Layout(search);

            double cardWidth = card.ActualWidth;
            double cardHeight = card.ActualHeight;
            Point offsetInCard = empty.TranslatePoint(new Point(0, 0), card);
            double textCenterX = offsetInCard.X + empty.ActualWidth / 2;
            double textCenterY = offsetInCard.Y + empty.ActualHeight / 2;

            string diagnostic =
                $"结果卡片实测 {cardWidth:0.#}×{cardHeight:0.#}；" +
                $"文字实测 {empty.ActualWidth:0.#}×{empty.ActualHeight:0.#}；" +
                $"文字中心卡片坐标 ({textCenterX:0.#},{textCenterY:0.#})，" +
                $"卡片中心 ({cardWidth / 2:0.#},{cardHeight / 2:0.#})；" +
                $"HorizontalAlignment={empty.HorizontalAlignment}，VerticalAlignment={empty.VerticalAlignment}。";

            empty.Visibility = Visibility.Collapsed;

            Assert.Equal(HorizontalAlignment.Center, empty.HorizontalAlignment);
            Assert.Equal(VerticalAlignment.Center, empty.VerticalAlignment);
            Assert.True(cardWidth > 400, $"UI-3 结果卡片没有铺满：{diagnostic}");
            Assert.True(
                Math.Abs(textCenterX - (cardWidth / 2)) <= 2,
                $"UI-3 空结果文字水平方向没有居中：{diagnostic}");
            Assert.True(
                Math.Abs(textCenterY - (cardHeight / 2)) <= 2,
                $"UI-3 空结果文字垂直方向没有居中：{diagnostic}");
        });
    }

    /// <summary>
    /// 输入区域可见性：基线 PlainTextBox 的 Background 是 ColorBrushTransparent、
    /// BorderBrush 是页面底色，空输入框未悬停/未聚焦时几乎不可见。
    /// JmlInputBox 必须把 Background 换成主题里已有的淡色（ColorBrushInputHoverBg）。
    /// </summary>
    [Fact]
    public void InputTextBoxes_HaveVisibleBackground()
    {
        RunOnUiThread(() =>
        {
            var download = new DownloadPage();
            var search = new SearchPage();

            var inputs = new (string Name, TextBox Box)[]
            {
                ("InputIds", Find<TextBox>(download, "InputIds")),
                ("InputDir", Find<TextBox>(download, "InputDir")),
                ("KeywordBox", Find<TextBox>(search, "KeywordBox")),
            };

            foreach ((string name, TextBox box) in inputs)
            {
                Brush? background = box.Background;
                Assert.True(background is not null, $"{name} 的 Background 为 null，JmlInputBox 样式没有生效。");

                var solid = Assert.IsType<SolidColorBrush>(background);
                Assert.True(
                    solid.Color.A > 0,
                    $"{name} 的 Background 仍透明（#{solid.Color.A:X2}{solid.Color.R:X2}" +
                    $"{solid.Color.G:X2}{solid.Color.B:X2}）；期望 JmlInputBox 的淡色底（ColorBrushInputHoverBg），" +
                    "否则空输入框在未悬停/未聚焦时不可见。");
            }
        });
    }

    /// <summary>
    /// 搜索失败时序：必须先恢复按钮（文字回「搜索」、重新可用）并写好状态栏，再弹框。
    /// 旧实现把 MessageBox 放在 try 里、恢复放在 finally 里，模态弹框期间按钮仍是
    /// 「搜索中…」且禁用。这里注入记录快照的弹框回调，不真的弹 MessageBox。
    /// </summary>
    [Fact]
    public void SearchFailure_RestoresButtonBeforeDialog()
    {
        RunOnUiThread(() =>
        {
            var search = new SearchPage();
            search.SearchInvoker = (_, _) => Task.FromResult(new SearchResultPageDto
            {
                Ok = false,
                ErrorCode = "TEST_FAILURE",
                ErrorMessage = "模拟搜索失败",
            });

            bool dialogCalled = false;
            bool? buttonEnabledInDialog = null;
            string? buttonTextInDialog = null;
            string? statusInDialog = null;
            string? dialogText = null;
            string? dialogCaption = null;

            search.ErrorDialog = (text, caption) =>
            {
                dialogCalled = true;
                var button = (Button)search.FindName("BtnSearch")!;
                buttonEnabledInDialog = button.IsEnabled;
                buttonTextInDialog = button.Content as string;
                statusInDialog = ((TextBlock)search.FindName("TextStatus")!).Text;
                dialogText = text;
                dialogCaption = caption;
            };

            search.RunSearchAsync("关键词", 1).GetAwaiter().GetResult();

            Assert.True(dialogCalled, "失败路径没有触发弹框回调（时序测试无从断言）。");
            Assert.True(
                buttonEnabledInDialog == true,
                "弹框时 BtnSearch 仍是禁用状态：失败分支先弹框、后恢复按钮（旧缺陷顺序）。");
            Assert.Equal("搜索", buttonTextInDialog);
            Assert.Contains("[TEST_FAILURE]", statusInDialog);
            Assert.Contains("模拟搜索失败", statusInDialog);
            Assert.Equal("模拟搜索失败", dialogText);
            Assert.Equal("搜索失败", dialogCaption);

            var finalButton = (Button)search.FindName("BtnSearch")!;
            Assert.True(finalButton.IsEnabled, "搜索结束后 BtnSearch 没有恢复可用。");
        });
    }

    /// <summary>
    /// 页数显示规则：page_count ≤ 0 是"后端没补到"（3 秒封顶 + 并发 3 允许不完整），
    /// 搜索行与详情窗都必须显示 `-`，不能把"未知"显示成"0 页"。
    /// </summary>
    [Fact]
    public void PageCountText_ShowsDashWhenUnknown()
    {
        var unknown = new SearchResultItemDto { BookId = "123456", PageCount = 0 };
        var known = new SearchResultItemDto { BookId = "123456", PageCount = 42 };

        // 搜索行整句格式不变：`页数: N | JM号: id`。
        Assert.Equal(
            "页数: - | JM号: 123456",
            SearchPage.BuildPageInfoText(unknown.PageCount, unknown.BookId));
        Assert.Equal(
            "页数: 42 | JM号: 123456",
            SearchPage.BuildPageInfoText(known.PageCount, known.BookId));

        // 详情窗的页数数值同源：≤0 → "-"。
        Assert.Equal("-", PageText.BuildPageCountText(unknown.PageCount));
        Assert.Equal("-", PageText.BuildPageCountText(-1));
        Assert.Equal("42", PageText.BuildPageCountText(known.PageCount));
    }

    /// <summary>
    /// 主题切换（PCL2 只有浅色）：ThemeHelper 必须真的把 ColorsDark 排到 Colors 之前，
    /// 让 ColorBrush4 解析为 PCL2 标题栏蓝 #4890F5。这是“切浅色”这条决定的直接断言。
    /// </summary>
    [Fact]
    public void ThemeHelper_SwitchesToPclLightPalette()
    {
        RunOnUiThread(() =>
        {
            // 直接跑生产路径（App.ApplyPclLightTheme）：它修正了库自带 Initialize
            // 在 BAML 相对 Source 下识别不到调色板的问题。
            JML.App.ApplyPclLightTheme(Application.Current.Resources);

            var titleBlue = Assert.IsType<SolidColorBrush>(Application.Current.FindResource("ColorBrush4"));
            Assert.Equal(Color.FromRgb(0x48, 0x90, 0xF5), titleBlue.Color);

            var white = Assert.IsType<SolidColorBrush>(Application.Current.FindResource("ColorBrushWhite"));
            Assert.Equal(Colors.White, white.Color);
        });
    }

    /// <summary>
    /// 换壳回归（PCL2 规格 A1/A2）：主窗口必须是无边框透明窗，默认 900×550 / 最小 810×470，
    /// 透明外边 10+8、圆角 6、标题栏 48 高且用主题 ColorBrush4 纯色，
    /// 主模式/内页模式两套标题栏与 28×28 最小化/关闭按钮都在。
    /// </summary>
    [Fact]
    public void MainWindow_Shell_UsesPclFramelessParameters()
    {
        RunOnUiThread(() =>
        {
            var window = new MainWindow();
            try
            {
                // A1 窗口参数
                Assert.Equal(WindowStyle.None, window.WindowStyle);
                Assert.True(window.AllowsTransparency, "无边框外壳必须开启 AllowsTransparency。");
                Assert.Null(window.Background);
                Assert.Equal(900, window.Width);
                Assert.Equal(550, window.Height);
                Assert.Equal(810, window.MinWidth);
                Assert.Equal(470, window.MinHeight);
                Assert.Equal(WindowStartupLocation.CenterScreen, window.WindowStartupLocation);
                Assert.Equal(ResizeMode.CanResize, window.ResizeMode);

                // 透明外边：外层 10（阴影区）+ 内层 8
                var panBack = Find<Grid>(window, "PanBack");
                Assert.Equal(new Thickness(10), panBack.Margin);
                var borderForm = Find<Border>(window, "BorderForm");
                Assert.Equal(new Thickness(8), borderForm.Margin);

                // 圆角 6（Clip 的 RectangleGeometry）
                Assert.Equal(new CornerRadius(6), borderForm.CornerRadius);
                var clip = Assert.IsType<RectangleGeometry>(borderForm.Clip);
                Assert.Equal(6, clip.RadiusX);
                Assert.Equal(6, clip.RadiusY);

                // 标题栏：48 高、纯色 ColorBrush4（不是渐变）
                var titleBar = Find<Grid>(window, "PanTitle");
                Assert.Equal(48, titleBar.Height);
                var titleBrush = Assert.IsType<SolidColorBrush>(titleBar.Background);
                Assert.Equal(ResourceColor("ColorBrush4"), titleBrush.Color);

                // 主模式 / 内页模式两套标题栏
                Assert.Equal(Visibility.Visible, Find<Grid>(window, "MainHeader").Visibility);
                Assert.Equal(Visibility.Collapsed, Find<Grid>(window, "InnerHeader").Visibility);
                Assert.IsType<Button>(window.FindName("BtnTitleMin"));
                Assert.IsType<Button>(window.FindName("BtnTitleClose"));
                Assert.IsType<Button>(window.FindName("BtnBack"));

                // 内页标题 15px（PCL2 FormMain.xaml:136）
                var innerTitle = Find<TextBlock>(window, "TextInnerTitle");
                Assert.Equal(15, innerTitle.FontSize);

                // A4 顶部四个胶囊导航：27 高、间距 5、内边距 2,0、带矢量图标
                foreach (string name in new[] { "NavDownload", "NavSearch", "NavPlugin", "NavSettings" })
                {
                    var nav = Find<RadioButton>(window, name);
                    Assert.Equal(27, nav.Height);
                    Assert.Equal(new Thickness(5, 0, 5, 0), nav.Margin);
                    Assert.Equal(new Thickness(2, 0, 2, 0), nav.Padding);
                    Assert.Same(Application.Current.FindResource("NavRadioButton"), nav.Style);

                    var content = Assert.IsType<StackPanel>(nav.Content);
                    Assert.Single(content.Children.OfType<Path>());
                    Assert.Single(content.Children.OfType<TextBlock>());
                }

                // 内容区仍在（换壳不能白屏）
                Assert.IsType<ContentControl>(window.FindName("MainContent"));

                // 页面底色资源存在且是渐变（PCL2 UiBackgroundColorful）
                var pageBackground = Assert.IsType<LinearGradientBrush>(
                    Application.Current.FindResource("JmlPageBackground"));
                Assert.Equal(3, pageBackground.GradientStops.Count);
            }
            finally
            {
                window.Close();
            }
        });
    }

    /// <summary>
    /// 换壳回归（PCL2 规格 A4）：导航胶囊模板 = 27 高、圆角 13.5、
    /// 选中态纯白底 + ColorBrush3 字色（无下划线）。
    /// </summary>
    [Fact]
    public void NavRadioButton_Template_IsPclCapsule()
    {
        RunOnUiThread(() =>
        {
            var nav = new RadioButton
            {
                Style = (Style)Application.Current.FindResource("NavRadioButton"),
                Content = "下载",
            };

            nav.Measure(new Size(200, 48));
            nav.Arrange(new Rect(0, 0, 200, 48));
            nav.UpdateLayout();

            Assert.Equal(27, nav.ActualHeight);
            var root = Assert.IsType<Border>(nav.Template.FindName("Root", nav));
            Assert.Equal(13.5, root.CornerRadius.TopLeft);
            Assert.Equal(13.5, root.CornerRadius.BottomRight);

            nav.IsChecked = true;
            nav.UpdateLayout();

            var rootBrush = Assert.IsType<SolidColorBrush>(root.Background);
            Assert.Equal(ResourceColor("ColorBrushWhite"), rootBrush.Color);

            var foreground = Assert.IsType<SolidColorBrush>(nav.Foreground);
            Assert.Equal(ResourceColor("ColorBrush3"), foreground.Color);
        });
    }

    /// <summary>
    /// 换壳回归（PCL2 规格 B）：左侧栏列表项行高 36、选中竖条 5 ×（行高−12）、圆角 2、
    /// 颜色 ColorBrush3；选中文字 14px 且变 ColorBrush3。
    /// </summary>
    [Fact]
    public void SideListItem_Template_UsesPclSelectionBar()
    {
        RunOnUiThread(() =>
        {
            var item = new RadioButton
            {
                Style = (Style)Application.Current.FindResource("SideListItem"),
                Content = "插件管理",
                IsChecked = true,
            };

            item.Measure(new Size(160, 36));
            item.Arrange(new Rect(0, 0, 160, 36));
            item.UpdateLayout();

            Assert.Equal(36, item.ActualHeight);
            Assert.Equal(14, item.FontSize);

            var bar = Assert.IsType<Rectangle>(item.Template.FindName("SelectionBar", item));
            Assert.Equal(5, bar.Width);
            Assert.Equal(2, bar.RadiusX);
            Assert.Equal(2, bar.RadiusY);
            Assert.Equal(Visibility.Visible, bar.Visibility);
            Assert.Equal(24, bar.ActualHeight); // 36 - 12
            Assert.Equal(ResourceColor("ColorBrush3"), Assert.IsType<SolidColorBrush>(bar.Fill).Color);

            var foreground = Assert.IsType<SolidColorBrush>(item.Foreground);
            Assert.Equal(ResourceColor("ColorBrush3"), foreground.Color);

            // 未选中时竖条隐藏（选中态只靠白胶囊/竖条表达，不保留下划线之类）
            item.IsChecked = false;
            item.UpdateLayout();
            Assert.Equal(Visibility.Collapsed, bar.Visibility);
        });
    }

    /// <summary>
    /// 换壳回归（PCL2 规格 B 分组标题）：FontSize=12、Opacity=0.6、Margin=13,20,5,4。
    /// </summary>
    [Fact]
    public void SideGroupTitle_Style_MatchesPcl()
    {
        RunOnUiThread(() =>
        {
            var style = (Style)Application.Current.FindResource("SideGroupTitle");
            Assert.Equal(12, GetStyleSetter<double>(style, TextBlock.FontSizeProperty));
            Assert.Equal(0.6, GetStyleSetter<double>(style, UIElement.OpacityProperty));
            Assert.Equal(
                new Thickness(13, 20, 5, 4),
                GetStyleSetter<Thickness>(style, FrameworkElement.MarginProperty));
        });
    }

    // ------------------------------------------------------------------
    // 页面层 PCL2 参数回归（2026-10-17）
    // 断言的是计划书/规格里的硬参数，不绑定具体页面文案或业务行为。
    // ------------------------------------------------------------------

    /// <summary>
    /// 卡片（PCL2 MyCard）：圆角 5、底色 #F5FFFFFF、无边框、紧阴影
    /// （BlurRadius=3 / ShadowDepth=0 / Opacity=0.07 / Color=ColorObject1）、
    /// 标题 Margin=15,12、13px Bold ColorBrush1、内容默认内边距 20,40,18,15。
    /// </summary>
    [Fact]
    public void JmlCard_Template_UsesPclParameters()
    {
        RunOnUiThread(() =>
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

            // 默认内容内边距（PCL2 PageDownloadInstall.xaml:31）
            Assert.Equal(new Thickness(20, 40, 18, 15), card.Padding);

            // 白底层：无边框、圆角 5、#F5FFFFFF
            var surface = Assert.IsType<Border>(card.Template.FindName("CardSurface", card));
            Assert.Equal(0, surface.BorderThickness.Left);
            Assert.Equal(5, surface.CornerRadius.TopLeft);
            var surfaceBrush = Assert.IsType<SolidColorBrush>(surface.Background);
            Assert.Equal(Color.FromArgb(0xF5, 0xFF, 0xFF, 0xFF), surfaceBrush.Color);

            // 阴影层：向外扩 3、紧阴影参数（PCL2 MyCard.vb:66-68）
            var shadow = Assert.IsType<Border>(card.Template.FindName("CardShadow", card));
            Assert.Equal(new Thickness(-3, -3, -3, -4), shadow.Margin);
            Assert.Equal(5, shadow.CornerRadius.TopLeft);
            var effect = Assert.IsType<DropShadowEffect>(shadow.Effect);
            Assert.Equal(3, effect.BlurRadius);
            Assert.Equal(0, effect.ShadowDepth);
            Assert.Equal(0.07, effect.Opacity, 3);
            Assert.Equal((Color)Application.Current.FindResource("ColorObject1"), effect.Color);

            // 标题：Margin=15,12、13px Bold、ColorBrush1
            var header = Assert.IsType<TextBlock>(card.Template.FindName("HeaderText", card));
            Assert.Equal("测试卡片", header.Text);
            Assert.Equal(new Thickness(15, 12, 0, 0), header.Margin);
            Assert.Equal(13, header.FontSize);
            Assert.Equal(FontWeights.Bold, header.FontWeight);
            var headerBrush = Assert.IsType<SolidColorBrush>(header.Foreground);
            Assert.Equal(ResourceColor("ColorBrush1"), headerBrush.Color);

            // 内容从 Padding 开始（上 40 让开标题行）
            var content = Assert.IsType<ContentPresenter>(card.Template.FindName("CardContent", card));
            Assert.Equal(card.Padding, content.Margin);
        });
    }

    /// <summary>
    /// 主行动按钮（PCL2 MyExtraTextButton）：高 52、圆角 20.8、底 ColorBrush3、
    /// 字 ColorBrush8 16px、DropShadowEffect BlurRadius=10 ShadowDepth=0 Opacity=0.2、
    /// 禁用底 ColorBrushGray4。
    /// </summary>
    [Fact]
    public void PrimaryButton_Template_IsPclCapsule()
    {
        RunOnUiThread(() =>
        {
            var button = new Button
            {
                Style = (Style)Application.Current.FindResource("PrimaryButton"),
                Content = "开始下载",
            };

            button.Measure(new Size(300, 52));
            button.Arrange(new Rect(0, 0, 300, 52));
            button.UpdateLayout();

            Assert.Equal(52, button.ActualHeight);
            Assert.Equal(16, button.FontSize);
            var foreground = Assert.IsType<SolidColorBrush>(button.Foreground);
            Assert.Equal(ResourceColor("ColorBrush8"), foreground.Color);

            var root = Assert.IsType<Border>(button.Template.FindName("Root", button));
            Assert.Equal(20.8, root.CornerRadius.TopLeft, 3);
            Assert.Equal(ResourceColor("ColorBrush3"), Assert.IsType<SolidColorBrush>(root.Background).Color);

            var effect = Assert.IsType<DropShadowEffect>(root.Effect);
            Assert.Equal(10, effect.BlurRadius);
            Assert.Equal(0, effect.ShadowDepth);
            Assert.Equal(0.2, effect.Opacity, 3);

            button.IsEnabled = false;
            button.UpdateLayout();
            Assert.Equal(
                ResourceColor("ColorBrushGray4"),
                Assert.IsType<SolidColorBrush>(root.Background).Color);
        });
    }

    /// <summary>
    /// 次按钮（PCL2 MyButton）：高 35、圆角 3、底 #55FFFFFF、边框 1px ColorBrush1、文字 13px。
    /// </summary>
    [Fact]
    public void SecondaryButton_Style_IsPcl35()
    {
        RunOnUiThread(() =>
        {
            var button = new Button
            {
                Style = (Style)Application.Current.FindResource("SecondaryButton"),
                Content = "浏览…",
            };

            button.Measure(new Size(200, 35));
            button.Arrange(new Rect(0, 0, 200, 35));
            button.UpdateLayout();

            Assert.Equal(35, button.ActualHeight);
            Assert.Equal(13, button.FontSize);
            Assert.Equal(
                Color.FromArgb(0x55, 0xFF, 0xFF, 0xFF),
                Assert.IsType<SolidColorBrush>(button.Background).Color);
            Assert.Equal(
                ResourceColor("ColorBrush1"),
                Assert.IsType<SolidColorBrush>(button.BorderBrush).Color);

            var panFore = Assert.IsType<Border>(button.Template.FindName("PanFore", button));
            Assert.Equal(3, panFore.CornerRadius.TopLeft);
            Assert.Equal(1, panFore.BorderThickness.Left);
        });
    }

    /// <summary>
    /// 页面外边距与卡片间距（PCL2）：下载页 25,10,25,25、卡片竖向间距 12；
    /// 搜索页 25、搜索卡→结果卡 15、分页卡上距 15；详情窗内边距 25。
    /// </summary>
    [Fact]
    public void Pages_ContentMargins_ArePcl25()
    {
        RunOnUiThread(() =>
        {
            var download = new DownloadPage();
            var downloadRoot = Assert.IsType<Grid>(download.Content);
            Assert.Equal(new Thickness(25, 10, 25, 25), downloadRoot.Margin);
            Assert.Equal(new Thickness(20, 40, 18, 15), Find<JmlCard>(download, "CardInput").Padding);
            Assert.Equal(new Thickness(0, 12, 0, 0), Find<JmlCard>(download, "CardLog").Margin);

            var search = new SearchPage();
            Assert.Equal(new Thickness(25), Assert.IsType<Grid>(search.Content).Margin);
            var searchCard = Find<JmlCard>(search, "CardSearch");
            Assert.Equal(new Thickness(0, 0, 0, 15), searchCard.Margin);
            Assert.Equal(new Thickness(25, 40, 25, 15), searchCard.Padding);
            Assert.Equal(new Thickness(0, 15, 0, 0), Find<JmlCard>(search, "CardPages").Margin);

            var detail = new DetailWindow("123456");
            try
            {
                Assert.Equal(new Thickness(25), Assert.IsType<Grid>(detail.Content).Margin);
                Assert.Equal(19, Find<TextBlock>(detail, "TextTitle").FontSize);
                Assert.Equal(13, Find<TextBlock>(detail, "TextDescription").FontSize);
                Assert.Same(
                    Application.Current.FindResource("PrimaryButton"),
                    Find<Button>(detail, "BtnDownload").Style);

                Layout((FrameworkElement)detail.Content);
                Assert.Equal(52, Find<Button>(detail, "BtnDownload").ActualHeight);
            }
            finally
            {
                detail.Close();
            }
        });
    }

    /// <summary>
    /// 设置页布局不拉伸修复：外层 ScrollViewer 必须禁用横向滚动（否则 * 列塌成 6px），
    /// 下载目录输入框在 1080 宽布局下必须铺满（实测修复前只有 6×36）。
    /// </summary>
    [Fact]
    public void SettingsPage_Content_Stretches()
    {
        RunOnUiThread(() =>
        {
            var settings = new SettingsPage();
            Layout(settings);

            var scroll = Assert.IsType<ScrollViewer>(settings.Content);
            Assert.Equal(ScrollBarVisibility.Disabled, scroll.HorizontalScrollBarVisibility);

            var input = Find<TextBox>(settings, "InputDownloadDir");
            Assert.True(
                input.ActualWidth > 400,
                $"设置页 InputDownloadDir 实测 {input.ActualWidth:0.#}×{input.ActualHeight:0.#}；" +
                "期望宽度 > 400（横向滚动禁用 + 卡片内容 Stretch 后铺满）。");
            Assert.True(
                input.ActualHeight >= 30,
                $"设置页 InputDownloadDir 高度实测 {input.ActualHeight:0.#}；期望 ≥ 30（MinHeight=32）。");
        });
    }

    /// <summary>
    /// 插件页 PCL2 观感：左栏底 #F1FFFFFF、右侧 4 宽渐变阴影（黑 4%→透明）；
    /// DataGrid 无粗边框、行高 36、表头高 34；操作按钮统一 35 高次按钮。
    /// </summary>
    [Fact]
    public void PluginPage_UsesPclSidebarAndDataGrid()
    {
        RunOnUiThread(() =>
        {
            var hub = new PluginHubPage();

            var left = Find<Border>(hub, "LeftColumn");
            Assert.Equal(
                Color.FromArgb(0xF1, 0xFF, 0xFF, 0xFF),
                Assert.IsType<SolidColorBrush>(left.Background).Color);

            var shadow = Find<Rectangle>(hub, "LeftShadow");
            Assert.Equal(4, shadow.Width);
            var gradient = Assert.IsType<LinearGradientBrush>(shadow.Fill);
            Assert.Equal(0.04, gradient.Opacity, 3);
            Assert.Equal(Colors.Black, gradient.GradientStops[0].Color);
            Assert.Equal(0, gradient.GradientStops[1].Color.A);

            var list = new PluginListView();
            var grid = Find<DataGrid>(list, "GridPlugins");
            Assert.Same(Application.Current.FindResource("JmlDataGrid"), grid.Style);
            Assert.Equal(0, grid.BorderThickness.Left);
            Assert.Equal(36, grid.RowHeight);
            Assert.Equal(34, grid.ColumnHeaderHeight);

            foreach (string name in new[] { "BtnEnable", "BtnDisable", "BtnRescan" })
            {
                Assert.Same(
                    Application.Current.FindResource("SecondaryButton"),
                    Find<Button>(list, name).Style);
            }
        });
    }
}

