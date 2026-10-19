namespace JML.Navigation;

/// <summary>一级页面类型（对应顶部导航的四个按钮）。</summary>
public enum MainPage
{
    Download,
    Search,
    Plugin,
    Settings,
}

/// <summary>
/// 导航栈：一级页面切换 + 插件内页的进栈/出栈。
/// 对应 PCL2 FormMain 的 PageStack + PageBack() 机制（仅借鉴结构，无代码复制）。
/// </summary>
public sealed class NavigationStack
{
    private readonly object _gate = new();

    /// <summary>当前一级页面（2026-10-19 起默认进入下载页）</summary>
    public MainPage Current { get; private set; } = MainPage.Download;

    /// <summary>当前一级页内的内页栈：空 = 未进入内页；栈顶 = 当前内页</summary>
    private readonly Stack<(string PageId, string Title)> _innerStack = new();

    /// <summary>一级页面切换时触发（内页栈同时被清空）</summary>
    public event EventHandler<MainPage>? MainChanged;

    /// <summary>内页进入/返回时触发</summary>
    public event EventHandler? InnerChanged;

    /// <summary>切换一级页面（同一页重复点击无副作用）。</summary>
    public void Navigate(MainPage page)
    {
        lock (_gate)
        {
            if (Current == page && _innerStack.Count == 0)
            {
                return;
            }

            Current = page;
            _innerStack.Clear();
        }

        MainChanged?.Invoke(this, page);
        InnerChanged?.Invoke(this, EventArgs.Empty);
    }

    /// <summary>进入内页（如某个插件的功能页）。</summary>
    public void PushInner(string pageId, string title)
    {
        lock (_gate)
        {
            _innerStack.Push((pageId, title));
        }

        InnerChanged?.Invoke(this, EventArgs.Empty);
    }

    /// <summary>
    /// 返回上一层内页。返回 true 表示弹回了一个内页；
    /// false 表示已在最外层（此时由调用方决定是否忽略，不自动跳一级页）。
    /// </summary>
    public bool PopInner()
    {
        bool popped;
        lock (_gate)
        {
            popped = _innerStack.Count > 0;
            if (popped) { _innerStack.Pop(); }
        }

        if (popped)
        {
            InnerChanged?.Invoke(this, EventArgs.Empty);
        }

        return popped;
    }

    /// <summary>当前内页（未进入内页时为 null）。</summary>
    public (string PageId, string Title)? CurrentInner
    {
        get
        {
            lock (_gate)
            {
                return _innerStack.Count > 0 ? _innerStack.Peek() : null;
            }
        }
    }

    /// <summary>是否处于内页（顶部应显示内页标题栏 + 返回按钮）。</summary>
    public bool IsInner => CurrentInner is not null;
}
