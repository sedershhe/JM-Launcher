using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Input;
using System.Windows.Media;
using JML.Navigation;
using JML.Pages;

namespace JML;

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

    /// <summary>详情窗单实例（MainWindow 统一持有）。</summary>
    private DetailWindow? _detailWindow;

    private bool _setupPassed;

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
            ShowSetup();
        };
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
    /// </summary>
    private void UpdateContent()
    {
        if (!_setupPassed)
        {
            return;
        }

        var inner = _nav.CurrentInner;
        if (inner is not null)
        {
            MainHeader.Visibility = Visibility.Collapsed;
            InnerHeader.Visibility = Visibility.Visible;
            TextInnerTitle.Text = inner.Value.Title;

            var placeholder = new PluginPagePlaceholder();
            placeholder.SetContent(inner.Value.Title);
            MainContent.Content = placeholder;
            return;
        }

        MainHeader.Visibility = Visibility.Visible;
        InnerHeader.Visibility = Visibility.Collapsed;

        MainContent.Content = _nav.Current switch
        {
            MainPage.Download => _downloadPage,
            MainPage.Search => _searchPage,
            MainPage.Settings => _settingsPage,
            _ => _pluginHubPage,
        };
    }
}
