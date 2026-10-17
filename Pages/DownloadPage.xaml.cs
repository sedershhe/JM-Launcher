using System.Windows;
using System.Windows.Controls;
using JML.Bridge;
using JML.Bridge.Dto;
using JML.Infrastructure;
using Microsoft.Win32;

namespace JML.Pages;

/// <summary>
/// 下载页（阶段 2-C）。
///
/// 数据来源：<see cref="DownloadBridge"/> 的四个入口 + <see cref="RuntimeBootstrap.Events"/> 的下载事件。
/// 纪律：
/// 1. 编号分隔符/去重/合法性、目录校验、产物命名、成功失败计数全部由 Python 决定，
///    本类只转发和展示；唯一的前端检查是"输入为空"；
/// 2. 所有桥接调用 await，不阻塞 UI 线程；
/// 3. 日志只显示本批次（空 batch_id 的全局日志也显示），自动滚到底；
/// 4. 批次结束文案逐字为「完成，成功 x / 共 y，失败 z」（有取消时追加「，取消 N」）。
/// </summary>
public partial class DownloadPage : UserControl
{
    /// <summary>等待 batch_id 期间最多缓存的事件数，防止无界增长。</summary>
    private const int PendingEventLimit = 500;

    /// <summary>契约 §1.1：下载被取消时 Python 在 item_finished 里回的错误码。</summary>
    private const string CancelledErrorCode = "CANCELLED";

    private EventRelay? _subscribedRelay;
    private readonly List<object> _pendingEvents = [];

    /// <summary>当前批次 id；为空表示没有正在跟踪的批次。</summary>
    private string? _currentBatchId;

    /// <summary>已调用 start_download、但 batch_id 尚未返回（此间事件先缓存，拿到 id 后回放）。</summary>
    private bool _awaitingBatch;

    /// <summary>start_download 调用进行中（防重复点击）。</summary>
    private bool _starting;

    /// <summary>应用设置：只用于记忆下载目录，不存任何凭据。</summary>
    private readonly AppSettings _settings = AppSettings.Load();

    public DownloadPage()
    {
        InitializeComponent();

        // 计划书 11.6：默认下载路径为桌面；阶段 3 起优先使用上次选择且仍存在的目录。
        InputDir.Text = AppSettings.ResolveDownloadDirectory(_settings.DownloadDir);

        // 主窗口在 App.OnStartup 之后才创建，此时事件中继通常已就绪；
        // 同时也在 Loaded 再补一次，覆盖启动失败后重试等情况。
        EnsureSubscribed();
        Loaded += (_, _) =>
        {
            EnsureSubscribed();

            // 切回本页时日志自动停在末尾（自动滚到底）。
            LogBox.ScrollToEnd();
        };
    }

    /// <summary>
    /// 供搜索页「下载」/详情窗「下载本子」协调使用：把编号填入输入框并立即开始。
    /// 返回 Python 给的 batch_id（失败返回 null），供详情窗按批次过滤日志。
    /// </summary>
    public async Task<string?> StartWithIds(IReadOnlyList<string> ids)
    {
        if (ids is null || ids.Count == 0)
        {
            return null;
        }

        // 仅回填界面文本，编号内容仍原样交给 Python。
        InputIds.Text = string.Join(", ", ids);
        return await StartBatchAsync(ids);
    }

    // ------------------------------------------------------------------
    // 事件订阅（按 Relay 实例幂等；页面生命周期内常驻，切页不丢事件）
    // ------------------------------------------------------------------

    private void EnsureSubscribed()
    {
        EventRelay? relay = RuntimeBootstrap.Events;
        if (relay is null || ReferenceEquals(relay, _subscribedRelay))
        {
            return;
        }

        Unsubscribe();
        relay.DownloadItemStartedReceived += OnItemStarted;
        relay.DownloadItemFinishedReceived += OnItemFinished;
        relay.DownloadBatchFinishedReceived += OnBatchFinished;
        relay.DownloadLogReceived += OnLog;
        _subscribedRelay = relay;
    }

    private void Unsubscribe()
    {
        if (_subscribedRelay is not { } relay)
        {
            return;
        }

        relay.DownloadItemStartedReceived -= OnItemStarted;
        relay.DownloadItemFinishedReceived -= OnItemFinished;
        relay.DownloadBatchFinishedReceived -= OnBatchFinished;
        relay.DownloadLogReceived -= OnLog;
        _subscribedRelay = null;
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

    private void OnLog(DownloadLogEventArgs args)
    {
        // 空 batch_id = 全局日志（宿主初始化等），任何批次都显示。
        if (args.IsGlobal)
        {
            AppendLog(args.Message);
            return;
        }

        Route(args, args.BatchId);
    }

    /// <summary>只在 batch_id 已知时处理；等待 batch_id 期间先缓存，避免丢掉最早几条事件。</summary>
    private void Route(object evt, string batchId)
    {
        if (_currentBatchId is not null)
        {
            if (string.Equals(batchId, _currentBatchId, StringComparison.Ordinal))
            {
                Apply(evt);
            }

            return;
        }

        if (_awaitingBatch && _pendingEvents.Count < PendingEventLimit)
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

    // ------------------------------------------------------------------
    // 用户操作
    // ------------------------------------------------------------------

    private void BtnBrowse_Click(object sender, RoutedEventArgs e)
    {
        var dialog = new OpenFolderDialog
        {
            Title = "选择下载保存目录",
            InitialDirectory = InputDir.Text,
            Multiselect = false,
        };

        if (dialog.ShowDialog() == true && !string.IsNullOrWhiteSpace(dialog.FolderName))
        {
            InputDir.Text = dialog.FolderName;
            RememberDownloadDirectory(dialog.FolderName);
        }
    }

    private async void BtnStart_Click(object sender, RoutedEventArgs e)
    {
        // 唯一的前端检查：空输入。其余参数校验全部交给 Python。
        if (string.IsNullOrWhiteSpace(InputIds.Text))
        {
            SetStatus("请输入至少一个本子编号。");
            ShowMessage("请输入至少一个本子编号。", "开始下载", MessageBoxImage.Information);
            return;
        }

        BtnStart.IsEnabled = false;
        SetStatus("正在解析编号…");

        ParseIdsResultDto parsed;
        try
        {
            parsed = await DownloadBridge.ParseBookIds(InputIds.Text);
        }
        finally
        {
            if (_currentBatchId is null && !_starting && !_awaitingBatch)
            {
                BtnStart.IsEnabled = true;
            }
        }

        if (!parsed.Ok)
        {
            SetStatus($"编号解析失败：[{parsed.ErrorCode}] {parsed.ErrorMessage}");
            ShowMessage(parsed.ErrorMessage, "编号解析失败", MessageBoxImage.Warning);
            return;
        }

        if (parsed.BookIds.Count == 0)
        {
            SetStatus("未解析出有效编号，请检查输入。");
            ShowMessage("未解析出有效编号，请检查输入。", "编号解析", MessageBoxImage.Information);
            return;
        }

        await StartBatchAsync(parsed.BookIds);
    }

    private async void BtnCancel_Click(object sender, RoutedEventArgs e)
    {
        string? batchId = _currentBatchId;
        if (string.IsNullOrEmpty(batchId))
        {
            return;
        }

        // 取消请求立刻发出（Python 真取消）；按钮等结束事件再恢复。
        BtnCancel.IsEnabled = false;
        SetStatus("正在请求取消…");

        CancelDownloadResultDto dto = await DownloadBridge.CancelDownload(batchId);

        // 等待期间批次可能已经结束（batch_finished 会恢复按钮并给出完成文案）；
        // 此时不要再用取消状态覆盖它。
        if (string.IsNullOrEmpty(_currentBatchId))
        {
            return;
        }

        if (!dto.Ok)
        {
            AppendLog($"[{dto.ErrorCode}] {dto.ErrorMessage}");
            SetStatus($"取消失败：{dto.ErrorMessage}");
            BtnCancel.IsEnabled = true;
            return;
        }

        SetStatus("已请求取消，正在等待当前本子退出…");
    }

    // ------------------------------------------------------------------
    // 下载流程
    // ------------------------------------------------------------------

    private async Task<string?> StartBatchAsync(IReadOnlyList<string> ids)
    {
        if (_starting || _currentBatchId is not null)
        {
            SetStatus("当前有下载任务在运行，请等待完成或取消后再开始。");
            return null;
        }

        // 用户点「开始下载」时把最终目录写回设置（只写路径）。
        RememberDownloadDirectory(InputDir.Text);

        _starting = true;
        _awaitingBatch = true;
        _currentBatchId = null;
        _pendingEvents.Clear();
        ClearLog();
        BtnStart.IsEnabled = false;
        BtnCancel.IsEnabled = false;
        SetStatus("正在开始下载…");

        StartDownloadResultDto dto;
        try
        {
            // work_dir 由 C# 显式传入，避免崩溃残留落到用户下载夹（契约 §九.4）。
            dto = await DownloadBridge.StartDownload(ids, InputDir.Text, AppPaths.TempDirectory);
        }
        finally
        {
            _starting = false;
        }

        if (!dto.Ok)
        {
            _awaitingBatch = false;
            _pendingEvents.Clear();
            BtnStart.IsEnabled = true;
            BtnCancel.IsEnabled = false;
            SetStatus($"下载未能开始：[{dto.ErrorCode}] {dto.ErrorMessage}");
            ShowMessage(dto.ErrorMessage, "开始下载失败", MessageBoxImage.Warning);
            return null;
        }

        _currentBatchId = dto.BatchId;
        _awaitingBatch = false;

        // 回放等待 batch_id 期间收到的事件（只保留属于本批次的）。
        List<object> pending = _pendingEvents.ToList();
        _pendingEvents.Clear();
        foreach (object evt in pending)
        {
            if (string.Equals(BatchIdOf(evt), dto.BatchId, StringComparison.Ordinal))
            {
                Apply(evt);
            }
        }

        // 回放中可能已经收到 batch_finished（极快失败），此时按钮已由 CompleteBatch 恢复。
        if (_currentBatchId is null)
        {
            return dto.BatchId;
        }

        BtnStart.IsEnabled = false;
        BtnCancel.IsEnabled = true;
        SetStatus($"下载中，共 {dto.Total} 本。");
        return dto.BatchId;
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

        // 错误文本直接展示 Python 给的 error_code / error_message，不做二次分类。
        AppendLog($"失败 JM{payload.BookId}：[{payload.ErrorCode}] {payload.ErrorMessage}");
    }

    private void CompleteBatch(DownloadBatchFinishedPayload payload)
    {
        _currentBatchId = null;
        _awaitingBatch = false;
        BtnStart.IsEnabled = true;
        BtnCancel.IsEnabled = false;

        // 计划书 11.6 逐字文案；仅在有取消时追加取消数，前三个数不变。
        string text = $"完成，成功 {payload.Success} / 共 {payload.Total}，失败 {payload.Failed}";
        if (payload.Cancelled > 0)
        {
            text += $"，取消 {payload.Cancelled}";
        }

        SetStatus(text);
        ShowMessage(text, "下载完成", MessageBoxImage.Information);
    }

    // ------------------------------------------------------------------
    // 展示辅助
    // ------------------------------------------------------------------

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

    private void ClearLog()
    {
        LogBox.Clear();
    }

    private void SetStatus(string text)
    {
        TextStatus.Text = text;
    }

    /// <summary>
    /// 记住最终下载目录（只写路径到 AppSettings，绝不写账号/密码）。
    /// 写入失败由 AppSettings 静默回退，不影响本次下载。
    /// </summary>
    private void RememberDownloadDirectory(string path)
    {
        if (string.IsNullOrWhiteSpace(path) ||
            string.Equals(_settings.DownloadDir, path, StringComparison.Ordinal))
        {
            return;
        }

        _settings.DownloadDir = path;
        _settings.Save();
    }

    private void ShowMessage(string text, string caption, MessageBoxImage icon)
    {
        Window? owner = Window.GetWindow(this);
        if (owner is not null)
        {
            MessageBox.Show(owner, text, caption, MessageBoxButton.OK, icon);
        }
        else
        {
            MessageBox.Show(text, caption, MessageBoxButton.OK, icon);
        }
    }
}
