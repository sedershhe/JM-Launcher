using System.Text.Json;
using JML.Bridge.Dto;

namespace JML.Bridge;

/// <summary>
/// 下载桥接：编号解析 / 开始批次 / 取消批次 / 查询进度。
///
/// 线程纪律：所有方法用 <see cref="Task.Run(Func{Task})"/> 包装，
/// UI 线程只 await，不直接阻塞等待 Python。
/// 失败一律返回 DTO 的 error_code，不向 UI 抛异常。
///
/// 业务纪律：不拼文件名、不判断成功失败、不取消前预检、不记忆批次状态；
/// 所有结论以 Python 返回值为准。
/// </summary>
public static class DownloadBridge
{
    /// <summary>解析批量编号输入（分隔符/去重/前导零全部由 Python 决定）。</summary>
    public static Task<ParseIdsResultDto> ParseBookIds(string text)
    {
        return Task.Run(() =>
        {
            try
            {
                return PythonGateway.CallJson<ParseIdsResultDto>(
                    backend => (string)backend.parse_book_ids(text ?? string.Empty));
            }
            catch (Exception ex)
            {
                BridgeFailures.Log("解析下载编号", ex);
                return BridgeFailures.Create<ParseIdsResultDto>(ex);
            }
        });
    }

    /// <summary>
    /// 登记下载批次。Python 立即返回，进度全部走事件；
    /// <paramref name="workDir"/> 由调用方传入（通常为 <c>AppPaths.TempDirectory</c>）。
    /// </summary>
    public static Task<StartDownloadResultDto> StartDownload(
        IReadOnlyList<string> ids,
        string targetDir,
        string workDir)
    {
        return Task.Run(() =>
        {
            try
            {
                // 只做参数序列化，不校验编号内容（非法 token 由 Python 整批拒绝）。
                string idsJson = JsonSerializer.Serialize(ids ?? Array.Empty<string>());
                return PythonGateway.CallJson<StartDownloadResultDto>(
                    backend => (string)backend.start_download(
                        idsJson,
                        targetDir ?? string.Empty,
                        workDir ?? string.Empty));
            }
            catch (Exception ex)
            {
                BridgeFailures.Log("开始下载批次", ex);
                return BridgeFailures.Create<StartDownloadResultDto>(ex);
            }
        });
    }

    /// <summary>取消批次：只置取消标志并立即返回，不等下载线程退出。</summary>
    public static Task<CancelDownloadResultDto> CancelDownload(string batchId)
    {
        return Task.Run(() =>
        {
            try
            {
                return PythonGateway.CallJson<CancelDownloadResultDto>(
                    backend => (string)backend.cancel_download(batchId ?? string.Empty));
            }
            catch (Exception ex)
            {
                BridgeFailures.Log("取消下载批次", ex);
                return BridgeFailures.Create<CancelDownloadResultDto>(ex);
            }
        });
    }

    /// <summary>查询批次状态；批次结束后 result 才非空（由 Python 判定）。</summary>
    public static Task<BatchStatusDto> GetProgress(string batchId)
    {
        return Task.Run(() =>
        {
            try
            {
                return PythonGateway.CallJson<BatchStatusDto>(
                    backend => (string)backend.get_progress(batchId ?? string.Empty));
            }
            catch (Exception ex)
            {
                BridgeFailures.Log("查询下载进度", ex);
                return BridgeFailures.Create<BatchStatusDto>(ex);
            }
        });
    }
}
