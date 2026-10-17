using System.ComponentModel;
using System.IO;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using JML.Bridge;
using JML.Bridge.Dto;
using JML.Infrastructure;

namespace JML.Pages;

/// <summary>
/// 搜索页（阶段 2-C）。
///
/// 数据来源：<see cref="SearchBridge.Search"/> 的返回值（本页不使用 search.page_loaded 事件，
/// 二选一以避免双渲染）；封面走 <see cref="SearchBridge.FetchCover"/> 只取字节。
///
/// 纪律：
/// 1. 关键词快照、总页数、页码修正全部由 Python 决定；翻页只传页码，关键词由 Python 快照决定，
///    C# 不保存"上一次关键词"；
/// 2. 封面 BitmapImage 在 UI 线程创建并设置 <see cref="BitmapCacheOption.OnLoad"/>，
///    失败显示暗红底 X（计划书 11.6）；
/// 3. 搜索/翻页失败只展示后端 error_code / error_message，不做文案分类。
/// </summary>
public partial class SearchPage : UserControl
{
    private bool _searching;
    private int _searchGeneration;
    private int _currentPage = 1;
    private int _totalPages = 1;
    private List<SearchRow> _rows = [];

    /// <summary>搜索调用（测试可替换；生产默认走 <see cref="SearchBridge.Search"/>）。</summary>
    internal Func<string, int, Task<SearchResultPageDto>> SearchInvoker { get; set; } =
        (keyword, page) => SearchBridge.Search(keyword, page, "main");

    /// <summary>
    /// 失败弹框（测试可注入以记录"弹框那一刻按钮是否已恢复"，避免真的弹 MessageBox）；
    /// 为 null 时走默认 MessageBox。参数：错误文本、标题。
    /// </summary>
    internal Action<string, string>? ErrorDialog { get; set; }

    /// <summary>用户点某行「下载」：把 JM 号交给 MainWindow 协调（切下载页并开始）。</summary>
    public event EventHandler<string>? DownloadRequested;

    /// <summary>用户点某行「详情」：把 JM 号交给 MainWindow 打开详情窗。</summary>
    public event EventHandler<string>? DetailRequested;

    public SearchPage()
    {
        InitializeComponent();
    }

    // ------------------------------------------------------------------
    // 搜索 / 翻页
    // ------------------------------------------------------------------

    private async void BtnSearch_Click(object sender, RoutedEventArgs e)
    {
        await RunSearchAsync(KeywordBox.Text, 1);
    }

    private async void KeywordBox_KeyDown(object sender, KeyEventArgs e)
    {
        if (e.Key != Key.Enter)
        {
            return;
        }

        e.Handled = true;
        await RunSearchAsync(KeywordBox.Text, 1);
    }

    private async void BtnPrev_Click(object sender, RoutedEventArgs e)
    {
        if (_currentPage <= 1)
        {
            return;
        }

        int targetPage = _currentPage - 1;

        // page > 1 时关键词由 Python 会话快照决定；回到第 1 页按契约是"发起新搜索"，
        // 必须带上关键词（取当前输入框，不额外记忆）。
        string keyword = targetPage <= 1 ? KeywordBox.Text : string.Empty;
        await RunSearchAsync(keyword, targetPage);
    }

    private async void BtnNext_Click(object sender, RoutedEventArgs e)
    {
        if (_currentPage >= _totalPages)
        {
            return;
        }

        await RunSearchAsync(string.Empty, _currentPage + 1);
    }

    /// <summary>
    /// 执行一次搜索/翻页并渲染返回值。参数校验（空关键词等）交给 Python。
    ///
    /// 失败时序：先由 finally 恢复按钮（文字回「搜索」、重新可用）并写好状态栏，
    /// 之后才弹框。旧实现把 MessageBox 放在 try 里、恢复放在 finally 里，
    /// 模态弹框期间按钮仍显示「搜索中…」且禁用。
    /// </summary>
    internal async Task RunSearchAsync(string keyword, int page)
    {
        if (_searching)
        {
            return;
        }

        _searching = true;
        int generation = ++_searchGeneration;
        SetBusy(true);
        TextStatus.Text = "搜索中…";

        SearchResultPageDto? failed = null;
        try
        {
            SearchResultPageDto dto = await SearchInvoker(keyword, page);
            if (generation != _searchGeneration)
            {
                return;
            }

            if (!dto.Ok)
            {
                failed = dto;
                TextStatus.Text = $"[{dto.ErrorCode}] {dto.ErrorMessage}";
            }
            else
            {
                Render(dto);
                _ = LoadCoversAsync(generation);
            }
        }
        finally
        {
            // 先恢复状态，再（在 finally 之外）弹框。
            _searching = false;
            SetBusy(false);
        }

        if (failed is not null)
        {
            ShowSearchFailure(failed.ErrorMessage, "搜索失败");
        }
    }

    /// <summary>搜索失败弹框。调用前必须已由 <see cref="SetBusy"/> 恢复按钮状态。</summary>
    private void ShowSearchFailure(string text, string caption)
    {
        if (ErrorDialog is { } dialog)
        {
            dialog(text, caption);
            return;
        }

        Window? owner = Window.GetWindow(this);
        if (owner is not null)
        {
            MessageBox.Show(owner, text, caption, MessageBoxButton.OK, MessageBoxImage.Warning);
        }
        else
        {
            MessageBox.Show(text, caption, MessageBoxButton.OK, MessageBoxImage.Warning);
        }
    }

    private void Render(SearchResultPageDto dto)
    {
        _currentPage = dto.Page;
        _totalPages = dto.TotalPages;

        // 实际使用的关键词（可能来自 Python 快照），以 DTO 为准。
        if (!string.IsNullOrEmpty(dto.Keyword))
        {
            KeywordBox.Text = dto.Keyword;
        }

        TextPage.Text = $"第 {dto.Page} 页 / 共 {dto.TotalPages} 页";

        var rows = new List<SearchRow>();
        foreach (SearchResultItemDto item in dto.Results)
        {
            rows.Add(new SearchRow(item));
        }

        _rows = rows;
        ResultList.ItemsSource = rows;
        TextEmpty.Visibility = rows.Count == 0 ? Visibility.Visible : Visibility.Collapsed;
        TextStatus.Text = rows.Count == 0 ? string.Empty : $"第 {dto.Page} 页，共 {rows.Count} 条结果。";
        UpdatePagingState();
    }

    private void SetBusy(bool busy)
    {
        BtnSearch.IsEnabled = !busy;
        BtnSearch.Content = busy ? "搜索中…" : "搜索";

        if (busy)
        {
            BtnPrev.IsEnabled = false;
            BtnNext.IsEnabled = false;
        }
        else
        {
            UpdatePagingState();
        }
    }

    private void UpdatePagingState()
    {
        BtnPrev.IsEnabled = _currentPage > 1;
        BtnNext.IsEnabled = _currentPage < _totalPages;
    }

    // ------------------------------------------------------------------
    // 封面（后台取字节，UI 线程建 BitmapImage）
    // ------------------------------------------------------------------

    private async Task LoadCoversAsync(int generation)
    {
        try
        {
            foreach (SearchRow row in _rows)
            {
                if (generation != _searchGeneration)
                {
                    return;
                }

                byte[]? bytes = await SearchBridge.FetchCover(row.Item.BookId);
                if (generation != _searchGeneration)
                {
                    return;
                }

                if (bytes is null || bytes.Length == 0)
                {
                    row.SetCoverFailed();
                    continue;
                }

                BitmapImage? image = CreateImage(bytes);
                if (image is null)
                {
                    row.SetCoverFailed();
                    continue;
                }

                row.SetCover(image);
            }
        }
        catch (Exception ex)
        {
            // 窗口关闭/Dispatcher 停止时可能中断；封面失败不影响列表本身。
            LogSanitizer.Log($"封面加载中断：{ex.GetType().Name}: {ex.Message}");
        }
    }

    /// <summary>
    /// 在 UI 线程用字节创建 BitmapImage（旧缺陷 #7）。
    /// CacheOption=OnLoad 保证流释放后图像仍可显示。
    /// </summary>
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
            LogSanitizer.Log($"封面解码失败：{ex.GetType().Name}: {ex.Message}");
            return null;
        }
    }

    // ------------------------------------------------------------------
    // 行内按钮
    // ------------------------------------------------------------------

    private void RowDownload_Click(object sender, RoutedEventArgs e)
    {
        if (sender is FrameworkElement { DataContext: SearchRow row })
        {
            DownloadRequested?.Invoke(this, row.Item.BookId);
        }
    }

    private void RowDetail_Click(object sender, RoutedEventArgs e)
    {
        if (sender is FrameworkElement { DataContext: SearchRow row })
        {
            DetailRequested?.Invoke(this, row.Item.BookId);
        }
    }

    // ------------------------------------------------------------------
    // 行模型（纯展示状态，不做业务判断）
    // ------------------------------------------------------------------

    /// <summary>
    /// 搜索结果行的页数文本（计划书 11.6 格式）：`页数: N | JM号: id`。
    /// page_count ≤ 0 表示后端没补到页数（3 秒封顶 + 并发 3，允许不完整），显示 `页数: -`，
    /// 不能把"未知"显示成"0 页"这个假数据。
    /// </summary>
    internal static string BuildPageInfoText(int pageCount, string bookId) =>
        $"页数: {PageText.BuildPageCountText(pageCount)} | JM号: {bookId}";

    private sealed class SearchRow : INotifyPropertyChanged
    {
        private ImageSource? _cover;
        private bool _failed;
        private bool _finished;

        public SearchRow(SearchResultItemDto item)
        {
            Item = item;
        }

        public SearchResultItemDto Item { get; }

        public string Title => Item.Title;

        public string Author => Item.Author;

        /// <summary>计划书 11.6 逐字：`页数: N | JM号: id`；页数未知（≤0）时显示 `页数: -`。</summary>
        public string PageInfo => BuildPageInfoText(Item.PageCount, Item.BookId);

        public ImageSource? Cover => _cover;

        public Visibility PlaceholderVisibility =>
            !_finished ? Visibility.Visible : Visibility.Collapsed;

        public Visibility FailureVisibility =>
            _failed ? Visibility.Visible : Visibility.Collapsed;

        public Visibility CoverVisibility =>
            _cover is not null ? Visibility.Visible : Visibility.Collapsed;

        public event PropertyChangedEventHandler? PropertyChanged;

        public void SetCover(ImageSource image)
        {
            _cover = image;
            _failed = false;
            _finished = true;
            Notify(nameof(Cover));
            Notify(nameof(CoverVisibility));
            Notify(nameof(PlaceholderVisibility));
            Notify(nameof(FailureVisibility));
        }

        public void SetCoverFailed()
        {
            _cover = null;
            _failed = true;
            _finished = true;
            Notify(nameof(Cover));
            Notify(nameof(CoverVisibility));
            Notify(nameof(PlaceholderVisibility));
            Notify(nameof(FailureVisibility));
        }

        private void Notify(string propertyName)
        {
            PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(propertyName));
        }
    }
}

/// <summary>
/// 页数展示文本（搜索行与详情窗共用）。
/// 页数未知（≤0）显示 `-`，避免把"未知"显示成"0 页"这个假数据。
/// </summary>
internal static class PageText
{
    internal static string BuildPageCountText(int pageCount) =>
        pageCount > 0 ? pageCount.ToString() : "-";
}
