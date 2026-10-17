using System.Threading;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Threading;
using JML.Bridge.Dto;
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
}


