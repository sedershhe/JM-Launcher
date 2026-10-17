using JML.Bridge.Dto;

namespace JML.Bridge;

/// <summary>
/// 搜索桥接：搜索/翻页 + 封面原始字节。
///
/// 线程纪律：
/// - <see cref="Search"/> 与 <see cref="FetchCover"/> 都是同步网络操作，
///   一律 <see cref="Task.Run(Func{Task})"/> 包装，禁止占用 UI 线程；
/// - 封面只返回原始 bytes，不在这里创建 BitmapImage（旧缺陷 #7）。
///
/// 业务纪律：不记忆关键词、不修正页码、不判断成功失败；翻页快照在 Python。
/// </summary>
public static class SearchBridge
{
    /// <summary>
    /// 搜索/翻页。<paramref name="sessionKey"/> 默认 <c>main</c>；
    /// <paramref name="page"/> &gt; 1 时关键词由 Python 会话快照决定。
    /// </summary>
    public static Task<SearchResultPageDto> Search(
        string keyword,
        int page,
        string sessionKey = "main")
    {
        return Task.Run(() =>
        {
            try
            {
                return PythonGateway.CallJson<SearchResultPageDto>(
                    backend => (string)backend.search(
                        keyword ?? string.Empty,
                        page,
                        sessionKey ?? "main"));
            }
            catch (Exception ex)
            {
                BridgeFailures.Log("搜索", ex);
                return BridgeFailures.Create<SearchResultPageDto>(ex);
            }
        });
    }

    /// <summary>
    /// 取封面原图字节；空编号或任何失败返回 null（契约 §3.12）。
    /// 调用方必须回 UI 线程后再创建 BitmapImage。
    /// </summary>
    public static Task<byte[]?> FetchCover(string bookId)
    {
        return Task.Run<byte[]?>(() =>
        {
            try
            {
                return PythonGateway.CallRaw<byte[]>(
                    backend => backend.fetch_cover(bookId ?? string.Empty));
            }
            catch (Exception ex)
            {
                // 契约规定失败即 None；不让网络/解码异常打穿 UI。
                BridgeFailures.Log("获取封面", ex);
                return null;
            }
        });
    }
}
