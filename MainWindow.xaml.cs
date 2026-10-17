using System.Windows;
using JML.Navigation;
using JML.Pages;

namespace JML;

/// <summary>
/// JM Launcher 主窗口（阶段 1 骨架 + 阶段 2 接线）。
///
/// 启动流程：
///   1. 先显示 PythonSetupPage（Python 环境检测，后台线程执行）；
///   2. 检测通过并点击"进入主界面"后，切换到主骨架（默认落在插件页）。
///
/// 导航：
///   顶部四个 RadioButton → NavigationStack.Navigate（一级页面）；
///   插件功能页双击 → PushInner（内页）；顶部「← 返回」→ PopInner。
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

    public MainWindow()
    {
        InitializeComponent();

        _nav.MainChanged += OnMainChanged;
        _nav.InnerChanged += OnInnerChanged;
        _pluginHubPage.FeatureRequested += OnFeatureRequested;

        // 搜索页的行内「详情」「下载」由主窗口协调。
        _searchPage.DownloadRequested += OnSearchDownloadRequested;
        _searchPage.DetailRequested += OnSearchDetailRequested;

        Loaded += (_, _) => ShowSetup();
    }

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
    /// 内页时：显示「← 返回 + 页面名」，内容区为插件功能占位页。
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
