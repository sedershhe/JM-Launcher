using JML.Bridge.Dto;

namespace JML.Bridge;

/// <summary>
/// 详情桥接：取本子详情。
///
/// 简介截断、标签排序等全部由 Python 完成；C# 只把 DTO 交给 UI 展示。
/// 同步网络调用 → <see cref="Task.Run(Func{Task})"/> 包装，失败返回 error_code。
/// </summary>
public static class DetailBridge
{
    /// <summary>取本子详情；失败返回 DTO 的 error_code / error_message。</summary>
    public static Task<BookDetailDto> GetDetail(string bookId)
    {
        return Task.Run(() =>
        {
            try
            {
                return PythonGateway.CallJson<BookDetailDto>(
                    backend => (string)backend.get_detail(bookId ?? string.Empty));
            }
            catch (Exception ex)
            {
                BridgeFailures.Log("获取本子详情", ex);
                return BridgeFailures.Create<BookDetailDto>(ex);
            }
        });
    }
}
