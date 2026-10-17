using System.IO;
using System.Windows;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using JML.Bridge;
using JML.Bridge.Dto;
using JML.Infrastructure;

namespace JML.Pages;

/// <summary>
/// 详情窗（阶段 2-C）。
///
/// 数据来源：<see cref="DetailBridge.GetDetail"/> + <see cref="SearchBridge.FetchCover"/>；
/// 简介截断、标签内容全部以 Python 返回值为准，C# 只展示。
///
/// 线程纪律：
/// 1. 不在构造里调 Python；<c>Loaded</c> 之后才 await 拉取；
/// 2. 封面字节在后台线程取，<see cref="BitmapImage"/> 在 UI 线程创建（CacheOption=OnLoad）；
/// 3. 下载日志只订阅全局事件并按 batch_id 过滤（旧缺陷 #6：不和主窗口抢同一个处理器）。
/// </summary>
public partial class DetailWindow : Window
{
    /// <summary>等待 batch_id 期间最多缓存的事件数。</summary>
    private const int PendingEventLimit = 500;

    /// <summary>契约 §1.1：下载被取消时 Python 在 item_finished 里回的错误码。</summary>
    private const string CancelledErrorCode = "CANCELLED";

    private readonly List<object> _pendingEvents = [];

    private EventRelay? _subscribedRelay;
    private string _bookId;
    private int _generation;
    private string? _batchId;

    /// <summary>用户点「下载本子」：把 JM 号交给 MainWindow（切下载页并开始）。</summary>
    public event EventHandler<string>? DownloadRequested;

    public DetailWindow(string bookId)
    {
        InitializeComponent();

        _bookId = bookId ?? string.Empty;
        Title = $"本子详情 - JM{_bookId}";
        TextStatus.Text = "正在加载详情…";

        EnsureSubscribed();
        Loaded += OnWindowLoaded;
        Closed += OnWindowClosed;
    }

    /// <summary>
    /// 复用同一个详情窗打开另一本（MainWindow 统一持有，避免多实例抢日志）。
    /// </summary>
    public void SetBook(string bookId)
    {
        if (string.IsNullOrWhiteSpace(bookId))
        {
            return;
        }

        _bookId = bookId;
        _batchId = null;
        _pendingEvents.Clear();
        Title = $"本子详情 - JM{bookId}";
        ResetUi();

        if (IsLoaded)
        {
            _ = LoadAsync(++_generation);
        }
    }

    /// <summary>
    /// 由 MainWindow 在下载页成功登记批次后调用：本窗日志区只显示该批次。
    /// 先缓存了等待 batch_id 期间的事件，这里回放属于该批次的那些。
    /// </summary>
    public void TrackBatch(string batchId)
    {
        if (string.IsNullOrWhiteSpace(batchId))
        {
            return;
        }

        _batchId = batchId;
        LogBox.Clear();
        TextStatus.Text = "下载中…";

        List<object> pending = _pendingEvents.ToList();
        _pendingEvents.Clear();
        foreach (object evt in pending)
        {
            if (string.Equals(BatchIdOf(evt), batchId, StringComparison.Ordinal))
            {
                Apply(evt);
            }
        }
    }

    /// <summary>MainWindow 告知「下载没有开始」（详情窗不显示业务原因，原因在下载页）。</summary>
    public void NotifyDownloadNotStarted()
    {
        TextStatus.Text = "下载未开始，请到下载页查看提示。";
    }

    // ------------------------------------------------------------------
    // 生命周期
    // ------------------------------------------------------------------

    private void OnWindowLoaded(object sender, RoutedEventArgs e)
    {
        EnsureSubscribed();
        _ = LoadAsync(++_generation);
    }

    private void OnWindowClosed(object? sender, EventArgs e)
    {
        // 关闭后到达的异步结果不再回填界面。
        _generation++;
        Unsubscribe();
    }

    // ------------------------------------------------------------------
    // 详情加载
    // ------------------------------------------------------------------

    private async Task LoadAsync(int generation)
    {
        ResetUi();
        TextStatus.Text = "正在加载详情…";

        BookDetailDto detail;
        try
        {
            detail = await DetailBridge.GetDetail(_bookId);
        }
        catch (Exception ex)
        {
            if (generation == _generation)
            {
                ShowError(LogSanitizer.Sanitize(ex.Message));
            }

            return;
        }

        if (generation != _generation)
        {
            return;
        }

        if (!detail.Ok)
        {
            // 失败信息直接展示 Python 给的 error_code / error_message，不分类、不猜测。
            ShowError($"[{detail.ErrorCode}] {detail.ErrorMessage}");
            return;
        }

        RenderDetail(detail);
        TextStatus.Text = string.Empty;

        // 封面：后台取字节，回 UI 线程解码。
        byte[]? bytes;
        try
        {
            bytes = await SearchBridge.FetchCover(_bookId);
        }
        catch (Exception ex)
        {
            bytes = null;
            LogSanitizer.Log($"详情封面获取失败：{ex.GetType().Name}: {ex.Message}");
        }

        if (generation != _generation)
        {
            return;
        }

        if (bytes is null || bytes.Length == 0)
        {
            ShowCoverFailed();
            return;
        }

        BitmapImage? image = CreateImage(bytes);
        if (image is null)
        {
            ShowCoverFailed();
            return;
        }

        ShowCover(image);
    }

    private void RenderDetail(BookDetailDto detail)
    {
        ErrorPanel.Visibility = Visibility.Collapsed;
        InfoPanel.Visibility = Visibility.Visible;

        TextTitle.Text = detail.Title;
        RunAuthor.Text = detail.Author;
        RunPageCount.Text = PageText.BuildPageCountText(detail.PageCount);
        TagItems.ItemsSource = detail.Tags;

        // 简介已由 Python 截断（200 字 + 省略号），这里直接显示，不再截断。
        TextDescription.Text = detail.Description;

        CoverImage.Source = null;
        CoverImage.Visibility = Visibility.Collapsed;
        CoverFailure.Visibility = Visibility.Collapsed;
        CoverPlaceholder.Visibility = Visibility.Visible;
    }

    private void ShowError(string text)
    {
        InfoPanel.Visibility = Visibility.Collapsed;
        ErrorPanel.Visibility = Visibility.Visible;
        TextError.Text = text;
        TextStatus.Text = "详情加载失败。";
    }

    private void ResetUi()
    {
        InfoPanel.Visibility = Visibility.Visible;
        ErrorPanel.Visibility = Visibility.Collapsed;
        TextError.Text = string.Empty;
        TextTitle.Text = string.Empty;
        RunAuthor.Text = string.Empty;
        RunPageCount.Text = string.Empty;
        TagItems.ItemsSource = null;
        TextDescription.Text = string.Empty;
        CoverImage.Source = null;
        CoverImage.Visibility = Visibility.Collapsed;
        CoverFailure.Visibility = Visibility.Collapsed;
        CoverPlaceholder.Visibility = Visibility.Visible;
        LogBox.Clear();
    }

    // ------------------------------------------------------------------
    // 封面
    // ------------------------------------------------------------------

    private void ShowCover(ImageSource image)
    {
        CoverImage.Source = image;
        CoverImage.Visibility = Visibility.Visible;
        CoverPlaceholder.Visibility = Visibility.Collapsed;
        CoverFailure.Visibility = Visibility.Collapsed;
    }

    private void ShowCoverFailed()
    {
        CoverImage.Source = null;
        CoverImage.Visibility = Visibility.Collapsed;
        CoverPlaceholder.Visibility = Visibility.Collapsed;
        CoverFailure.Visibility = Visibility.Visible;
    }

    /// <summary>UI 线程创建 BitmapImage；OnLoad 保证流释放后仍可显示。</summary>
    private static BitmapImage? CreateImage(byte[] bytes)
    {
        try
        {
            using var stream = new MemoryStream(bytes, writable: false);
            var image = new BitmapImage();
            image.BeginInit();
            image.CacheOption = BitmapCacheOption.OnLoad;
            image.StreamSource = stream;
            image.EndInit();
            image.Freeze();
            return image;
        }
        catch (Exception ex)
        {
            LogSanitizer.Log($"详情封面解码失败：{ex.GetType().Name}: {ex.Message}");
            return null;
        }
    }

    // ------------------------------------------------------------------
    // 下载
    // ------------------------------------------------------------------

    private void BtnDownload_Click(object sender, RoutedEventArgs e)
    {
        if (string.IsNullOrWhiteSpace(_bookId))
        {
            return;
        }

        TextStatus.Text = "已请求下载，正在切换到下载页…";
        DownloadRequested?.Invoke(this, _bookId);
    }

    // ------------------------------------------------------------------
    // 批次日志（只显示本书批次）
    // ------------------------------------------------------------------

    private void EnsureSubscribed()
    {
        EventRelay? relay = RuntimeBootstrap.Events;
        if (relay is null || ReferenceEquals(relay, _subscribedRelay))
        {
            return;
        }

        Unsubscribe();
        relay.DownloadLogReceived += OnLog;
        relay.DownloadItemStartedReceived += OnItemStarted;
        relay.DownloadItemFinishedReceived += OnItemFinished;
        relay.DownloadBatchFinishedReceived += OnBatchFinished;
        _subscribedRelay = relay;
    }

    private void Unsubscribe()
    {
        if (_subscribedRelay is not { } relay)
        {
            return;
        }

        relay.DownloadLogReceived -= OnLog;
        relay.DownloadItemStartedReceived -= OnItemStarted;
        relay.DownloadItemFinishedReceived -= OnItemFinished;
        relay.DownloadBatchFinishedReceived -= OnBatchFinished;
        _subscribedRelay = null;
    }

    private void OnLog(DownloadLogEventArgs args)
    {
        // 详情窗只显示本书批次；全局日志归主下载页。
        if (args.IsGlobal)
        {
            return;
        }

        Route(args, args.BatchId);
    }

    private void OnItemStarted(DownloadItemStartedPayload payload)
    {
        Route(payload, payload.BatchId);
    }

    private void OnItemFinished(DownloadItemFinishedPayload payload)
    {
        Route(payload, payload.BatchId);
    }

    private void OnBatchFinished(DownloadBatchFinishedPayload payload)
    {
        Route(payload, payload.BatchId);
    }

    private void Route(object evt, string batchId)
    {
        if (_batchId is not null)
        {
            if (string.Equals(batchId, _batchId, StringComparison.Ordinal))
            {
                Apply(evt);
            }

            return;
        }

        if (_pendingEvents.Count < PendingEventLimit)
        {
            _pendingEvents.Add(evt);
        }
    }

    private void Apply(object evt)
    {
        switch (evt)
        {
            case DownloadLogEventArgs log:
                AppendLog(log.Message);
                break;

            case DownloadItemStartedPayload started:
                AppendLog($"开始下载 JM{started.BookId}");
                break;

            case DownloadItemFinishedPayload finished:
                AppendItemFinished(finished);
                break;

            case DownloadBatchFinishedPayload batch:
                CompleteBatch(batch);
                break;
        }
    }

    private void AppendItemFinished(DownloadItemFinishedPayload payload)
    {
        if (payload.Ok)
        {
            AppendLog($"成功 JM{payload.BookId}：{payload.ZipPath}");
            return;
        }

        // 取消按契约错误码识别（不按 error_message 文案分类）。
        if (string.Equals(payload.ErrorCode, CancelledErrorCode, StringComparison.Ordinal))
        {
            AppendLog($"取消 JM{payload.BookId}：[{payload.ErrorCode}] {payload.ErrorMessage}");
            return;
        }

        AppendLog($"失败 JM{payload.BookId}：[{payload.ErrorCode}] {payload.ErrorMessage}");
    }

    private void CompleteBatch(DownloadBatchFinishedPayload payload)
    {
        string text = $"完成，成功 {payload.Success} / 共 {payload.Total}，失败 {payload.Failed}";
        if (payload.Cancelled > 0)
        {
            text += $"，取消 {payload.Cancelled}";
        }

        AppendLog(text);
        TextStatus.Text = text;
    }

    private static string BatchIdOf(object evt) => evt switch
    {
        DownloadLogEventArgs log => log.BatchId,
        DownloadItemStartedPayload started => started.BatchId,
        DownloadItemFinishedPayload finished => finished.BatchId,
        DownloadBatchFinishedPayload batch => batch.BatchId,
        _ => string.Empty,
    };

    private void AppendLog(string message)
    {
        if (string.IsNullOrEmpty(message))
        {
            return;
        }

        LogBox.AppendText(message + Environment.NewLine);
        LogBox.ScrollToEnd();
    }
}
