using System.IO;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace JML.Infrastructure;

/// <summary>
/// 凭据实际落点。只用于界面/诊断展示，不包含任何凭据内容。
/// </summary>
public enum CredentialStorageBackend
{
    /// <summary>当前没有可用的凭据落点，或尚未执行读写。</summary>
    None,

    /// <summary>Windows 凭据管理器（首选）。</summary>
    CredentialManager,

    /// <summary>DPAPI 加密文件 <c>%APPDATA%\JML\account.dat</c>（回退）。</summary>
    DpapiFile,
}

/// <summary>
/// 一组内存中的账号凭据。
///
/// <see cref="ToString"/> 故意不输出任何字段，避免调试器/日志顺手把凭据带出去。
/// </summary>
public sealed class CredentialRecord
{
    internal CredentialRecord(string username, string password)
    {
        Username = username;
        Password = password;
    }

    /// <summary>真实用户名；只允许用于登录和打码展示，不得写入配置。</summary>
    public string Username { get; }

    /// <summary>真实密码；只在本次内存对象中存在，不得写入配置或日志。</summary>
    public string Password { get; }

    /// <inheritdoc />
    public override string ToString() => "CredentialRecord(凭据已隐藏)";
}

/// <summary>
/// 凭据安全存储（功能计划书 4.8 / 11.2，技术方案第六章，桥接契约 §十）。
///
/// 存储顺序：
/// 1. **Windows 凭据管理器**（首选）：P/Invoke <c>CredWriteW / CredReadW / CredDeleteW</c>，
///    类型 <c>CRED_TYPE_GENERIC</c>，目标名 <see cref="DefaultTargetName"/>；
/// 2. **DPAPI 加密文件**（回退）：<c>ProtectedData.Protect(..., CurrentUser)</c> 加密
///    <c>{"username":..,"password":..}</c> 后写入 <see cref="AppPaths.AccountFilePath"/>；
/// 3. 两者都失败时只返回失败与固定中文提示，**绝不明文落盘**。
///
/// 纪律：
/// - 本类不写日志；<see cref="LastError"/> 在返回前过 <see cref="LogSanitizer"/>；
/// - 本类不持有 <see cref="AppSettings"/>，凭据不会进入 settings.json；
/// - <see cref="ToString"/> 不输出字段。
/// </summary>
public sealed class CredentialStore
{
    /// <summary>凭据管理器目标名；固定值，修改会导致旧凭据读不到。</summary>
    public const string DefaultTargetName = "JML/jm-account";

    /// <summary>两者都不可用时的固定中文提示（对 UI 展示，不拼接底层异常）。</summary>
    public const string BothBackendsUnavailableMessage = "凭据管理器与加密文件均不可用，账号功能暂不可用";

    /// <summary>Windows <c>CRED_TYPE_GENERIC</c>。</summary>
    private const uint CredTypeGeneric = 1;

    /// <summary>
    /// <c>CRED_PERSIST_LOCAL_MACHINE</c>：仅当前 Windows 用户在本机可读，重启后仍保留，
    /// 不随域账户漫游。它与 DPAPI 的 <c>CurrentUser</c> 作用域一致，所以选它而不是
    /// <c>CRED_PERSIST_SESSION</c>（后者注销/重启即丢，无法做到“记住账号”）。
    /// </summary>
    private const uint CredPersistLocalMachine = 2;

    /// <summary>ERROR_NOT_FOUND；删除/读取不存在的通用凭据按“没有”处理。</summary>
    private const int ErrorNotFound = 1168;

    /// <summary>CRED_MAX_CREDENTIAL_BLOB_SIZE（5 * 512）；超过则回退 DPAPI。</summary>
    private const int MaxCredentialBlobSize = 5 * 512;

    /// <summary>DPAPI 的附加熵，用于把本项目凭据与其它 DPAPI 数据隔离。</summary>
    private static readonly byte[] DpapiEntropy = Encoding.UTF8.GetBytes("JML/account/v1");

    private static readonly JsonSerializerOptions PayloadJsonOptions = new()
    {
        PropertyNameCaseInsensitive = true,
    };

    private readonly object _gate = new();
    private readonly string _dpapiPath;
    private readonly string _targetName;
    private readonly bool _useCredentialManager;

    /// <summary>默认实例：凭据管理器优先，失败回退 <see cref="AppPaths.AccountFilePath"/>。</summary>
    public static CredentialStore Default { get; } = new();

    /// <summary>
    /// 创建凭据存储。
    /// </summary>
    /// <param name="dpapiPath">
    /// DPAPI 密文路径；测试可传临时文件。为空时使用 <see cref="AppPaths.AccountFilePath"/>。
    /// </param>
    /// <param name="useCredentialManager">
    /// 是否尝试 Windows 凭据管理器；测试关闭它，避免污染真实凭据库。
    /// </param>
    /// <param name="targetName">凭据管理器目标名；测试可传独立值。</param>
    public CredentialStore(
        string? dpapiPath = null,
        bool useCredentialManager = true,
        string? targetName = null)
    {
        _dpapiPath = string.IsNullOrWhiteSpace(dpapiPath) ? AppPaths.AccountFilePath : dpapiPath;
        _useCredentialManager = useCredentialManager;
        _targetName = string.IsNullOrWhiteSpace(targetName) ? DefaultTargetName : targetName;
    }

    /// <summary>最近一次操作的脱敏错误文案；成功或无凭据时为空串。</summary>
    public string LastError { get; private set; } = "";

    /// <summary>最近一次成功读/写使用的落点；失败/无凭据为 <see cref="CredentialStorageBackend.None"/>。</summary>
    public CredentialStorageBackend LastBackend { get; private set; }

    /// <summary>
    /// 保存凭据：先尝试 Windows 凭据管理器，失败回退 DPAPI 加密文件。
    /// 两者都失败时返回 false，并给出固定中文提示；绝不写明文。
    /// </summary>
    public bool Save(string username, string password)
    {
        lock (_gate)
        {
            ClearError();

            string safeUsername = (username ?? string.Empty).Trim();
            string safePassword = password ?? string.Empty;
            if (safeUsername.Length == 0 || safePassword.Length == 0)
            {
                LastBackend = CredentialStorageBackend.None;
                SetError("账号和密码不能为空");
                return false;
            }

            if (_useCredentialManager &&
                TrySaveCredentialManager(safeUsername, safePassword, out _))
            {
                LastBackend = CredentialStorageBackend.CredentialManager;
                ClearError();

                // 凭据管理器是首选落点；清掉可能残留的旧 DPAPI 回退文件，避免下次读取到旧值。
                TryDeleteDpapiQuiet();
                return true;
            }

            if (TrySaveDpapi(safeUsername, safePassword, out _))
            {
                LastBackend = CredentialStorageBackend.DpapiFile;
                ClearError();

                // 同上：回退文件成为最新值后，尽力清掉凭据管理器里的旧值。
                TryDeleteCredentialManagerQuiet();
                return true;
            }

            LastBackend = CredentialStorageBackend.None;
            SetError(BothBackendsUnavailableMessage);
            return false;
        }
    }

    /// <summary>
    /// 读取凭据：先凭据管理器，再 DPAPI 回退。
    /// 没有任何已保存凭据时返回 null 且 <see cref="LastError"/> 为空；
    /// 两个落点都不可用时返回 null 且 <see cref="LastError"/> 为明确中文提示。
    /// </summary>
    public (string Username, string Password)? Load()
    {
        lock (_gate)
        {
            ClearError();
            LastBackend = CredentialStorageBackend.None;

            bool cmNotFound = !_useCredentialManager;
            string cmError = "";
            if (_useCredentialManager &&
                TryReadCredentialManager(out CredentialRecord? cmRecord, out cmNotFound, out cmError) &&
                cmRecord is not null)
            {
                LastBackend = CredentialStorageBackend.CredentialManager;
                return (cmRecord.Username, cmRecord.Password);
            }

            bool dpMissing = false;
            string dpError = "";
            if (TryReadDpapi(out CredentialRecord? dpRecord, out dpMissing, out dpError) &&
                dpRecord is not null)
            {
                LastBackend = CredentialStorageBackend.DpapiFile;
                return (dpRecord.Username, dpRecord.Password);
            }

            // 两个落点都明确没有数据：属于“未保存账号”，不是错误。
            if (cmNotFound && dpMissing)
            {
                return null;
            }

            // 凭据管理器只是没有条目、DPAPI 文件存在但损坏：给出文件层错误更准确。
            if (cmNotFound)
            {
                SetError(string.IsNullOrEmpty(dpError)
                    ? "读取已保存的加密凭据失败，请重新保存账号"
                    : dpError);
                return null;
            }

            // 凭据管理器不可用（非“没有记录”）且 DPAPI 也没有可用内容：
            // 账号持久化整体不可用，给固定提示，不拼底层异常。
            SetError(BothBackendsUnavailableMessage);
            return null;
        }
    }

    /// <summary>删除两个落点中的凭据；不存在的条目按删除成功处理。</summary>
    public bool Delete()
    {
        lock (_gate)
        {
            ClearError();
            LastBackend = CredentialStorageBackend.None;

            bool cmOk = true;
            bool dpOk = true;
            string cmError = "";
            string dpError = "";

            if (_useCredentialManager &&
                !TryDeleteCredentialManager(out cmError))
            {
                cmOk = false;
            }

            if (!TryDeleteDpapi(out dpError))
            {
                dpOk = false;
            }

            if (cmOk && dpOk)
            {
                ClearError();
                return true;
            }

            if (!cmOk && !dpOk)
            {
                SetError(BothBackendsUnavailableMessage);
            }
            else if (!cmOk)
            {
                SetError("无法删除 Windows 凭据管理器中的账号凭据");
            }
            else
            {
                SetError("无法删除加密凭据文件，请检查文件权限");
            }

            return false;
        }
    }

    /// <summary>
    /// 是否存在已保存凭据。DPAPI 侧只判断文件存在，不解密；
    /// 凭据管理器侧用一次 <c>CredReadW</c> 判断条目是否存在。
    /// </summary>
    public bool Exists()
    {
        lock (_gate)
        {
            if (_useCredentialManager &&
                TryReadCredentialManager(out CredentialRecord? record, out _, out _) &&
                record is not null)
            {
                return true;
            }

            try
            {
                return File.Exists(_dpapiPath);
            }
            catch
            {
                return false;
            }
        }
    }

    /// <inheritdoc />
    public override string ToString() => "CredentialStore(凭据已隐藏)";

    // ------------------------------------------------------------------
    // Windows 凭据管理器（advapi32）
    // ------------------------------------------------------------------

    private bool TrySaveCredentialManager(string username, string password, out string error)
    {
        error = "";
        IntPtr blob = IntPtr.Zero;
        byte[]? passwordBytes = null;

        try
        {
            passwordBytes = Encoding.Unicode.GetBytes(password);
            if (passwordBytes.Length > MaxCredentialBlobSize)
            {
                error = "密码过长，Windows 凭据管理器无法保存";
                return false;
            }

            blob = Marshal.AllocHGlobal(passwordBytes.Length);
            Marshal.Copy(passwordBytes, 0, blob, passwordBytes.Length);

            var credential = new CREDENTIAL
            {
                Flags = 0,
                Type = CredTypeGeneric,
                TargetName = _targetName,
                Comment = "JML account credential",
                CredentialBlobSize = (uint)passwordBytes.Length,
                CredentialBlob = blob,
                Persist = CredPersistLocalMachine,
                AttributeCount = 0,
                Attributes = IntPtr.Zero,
                TargetAlias = null,
                UserName = username,
            };

            if (!CredWriteW(ref credential, 0))
            {
                int code = Marshal.GetLastWin32Error();
                error = $"CredWriteW 失败，Win32 错误码 {code}";
                return false;
            }

            return true;
        }
        catch (Exception ex)
        {
            // 异常消息可能来自系统层；只保留类型名，凭据值不进 LastError。
            error = $"Windows 凭据管理器写入异常：{ex.GetType().Name}";
            return false;
        }
        finally
        {
            if (blob != IntPtr.Zero)
            {
                if (passwordBytes is not null && passwordBytes.Length > 0)
                {
                    // 擦掉非托管明文，再释放。
                    Marshal.Copy(new byte[passwordBytes.Length], 0, blob, passwordBytes.Length);
                }

                Marshal.FreeHGlobal(blob);
            }

            if (passwordBytes is not null)
            {
                CryptographicOperations.ZeroMemory(passwordBytes);
            }
        }
    }

    private bool TryReadCredentialManager(
        out CredentialRecord? record,
        out bool notFound,
        out string error)
    {
        record = null;
        notFound = false;
        error = "";
        IntPtr raw = IntPtr.Zero;

        try
        {
            if (!CredReadW(_targetName, CredTypeGeneric, 0, out raw))
            {
                int code = Marshal.GetLastWin32Error();
                if (code == ErrorNotFound)
                {
                    notFound = true;
                    return false;
                }

                error = $"CredReadW 失败，Win32 错误码 {code}";
                return false;
            }

            var credential = Marshal.PtrToStructure<CREDENTIAL>(raw);
            if (credential.CredentialBlob == IntPtr.Zero ||
                credential.CredentialBlobSize == 0 ||
                credential.CredentialBlobSize > int.MaxValue ||
                credential.CredentialBlobSize % 2 != 0)
            {
                error = "Windows 凭据管理器中的账号数据格式无效";
                return false;
            }

            string username = credential.UserName ?? "";
            string password = Marshal.PtrToStringUni(
                                  credential.CredentialBlob,
                                  (int)(credential.CredentialBlobSize / 2))
                              ?? "";
            if (username.Length == 0 || password.Length == 0)
            {
                error = "Windows 凭据管理器中的账号数据为空";
                return false;
            }

            record = new CredentialRecord(username, password);
            return true;
        }
        catch (Exception ex)
        {
            error = $"Windows 凭据管理器读取异常：{ex.GetType().Name}";
            return false;
        }
        finally
        {
            if (raw != IntPtr.Zero)
            {
                CredFree(raw);
            }
        }
    }

    private bool TryDeleteCredentialManager(out string error)
    {
        error = "";

        try
        {
            if (CredDeleteW(_targetName, CredTypeGeneric, 0))
            {
                return true;
            }

            int code = Marshal.GetLastWin32Error();
            if (code == ErrorNotFound)
            {
                return true;
            }

            error = $"CredDeleteW 失败，Win32 错误码 {code}";
            return false;
        }
        catch (Exception ex)
        {
            error = $"Windows 凭据管理器删除异常：{ex.GetType().Name}";
            return false;
        }
    }

    private void TryDeleteCredentialManagerQuiet()
    {
        if (!_useCredentialManager)
        {
            return;
        }

        try
        {
            _ = TryDeleteCredentialManager(out _);
        }
        catch
        {
            // 回退存储已经成功；清旧值失败不影响本次保存。
        }
    }

    // ------------------------------------------------------------------
    // DPAPI 加密文件（回退）
    // ------------------------------------------------------------------

    private bool TrySaveDpapi(string username, string password, out string error)
    {
        error = "";
        string? tempPath = null;

        try
        {
            var payload = new DpapiPayload
            {
                Username = username,
                Password = password,
            };

            byte[] plain = JsonSerializer.SerializeToUtf8Bytes(payload, PayloadJsonOptions);
            try
            {
                byte[] cipher = ProtectedData.Protect(
                    plain,
                    DpapiEntropy,
                    DataProtectionScope.CurrentUser);

                string? directory = Path.GetDirectoryName(_dpapiPath);
                if (!string.IsNullOrEmpty(directory))
                {
                    Directory.CreateDirectory(directory);
                }

                // 先写临时文件再原子替换，避免中途崩溃留下半截密文。
                tempPath = _dpapiPath + ".tmp-" + Guid.NewGuid().ToString("N");
                File.WriteAllBytes(tempPath, cipher);
                File.Move(tempPath, _dpapiPath, overwrite: true);
                tempPath = null;
                return true;
            }
            finally
            {
                CryptographicOperations.ZeroMemory(plain);
            }
        }
        catch (Exception ex)
        {
            error = $"写入加密凭据失败：{ex.GetType().Name}";
            return false;
        }
        finally
        {
            if (tempPath is not null)
            {
                try
                {
                    File.Delete(tempPath);
                }
                catch
                {
                    // 临时密文清理失败不影响结果；不把路径写进 LastError。
                }
            }
        }
    }

    private bool TryReadDpapi(
        out CredentialRecord? record,
        out bool missing,
        out string error)
    {
        record = null;
        missing = false;
        error = "";

        try
        {
            if (!File.Exists(_dpapiPath))
            {
                missing = true;
                return false;
            }

            byte[] cipher = File.ReadAllBytes(_dpapiPath);
            byte[] plain = ProtectedData.Unprotect(
                cipher,
                DpapiEntropy,
                DataProtectionScope.CurrentUser);

            try
            {
                DpapiPayload? payload = JsonSerializer.Deserialize<DpapiPayload>(
                    plain,
                    PayloadJsonOptions);

                if (payload is null ||
                    string.IsNullOrWhiteSpace(payload.Username) ||
                    string.IsNullOrEmpty(payload.Password))
                {
                    error = "已保存的加密凭据格式无效，请重新保存账号";
                    return false;
                }

                record = new CredentialRecord(payload.Username, payload.Password);
                return true;
            }
            finally
            {
                CryptographicOperations.ZeroMemory(plain);
            }
        }
        catch (CryptographicException)
        {
            error = "已保存的加密凭据损坏或无法解密，请重新保存账号";
            return false;
        }
        catch (Exception ex)
        {
            error = $"读取加密凭据失败：{ex.GetType().Name}";
            return false;
        }
    }

    private bool TryDeleteDpapi(out string error)
    {
        error = "";

        try
        {
            if (File.Exists(_dpapiPath))
            {
                File.Delete(_dpapiPath);
            }

            return true;
        }
        catch (Exception ex)
        {
            error = $"删除加密凭据失败：{ex.GetType().Name}";
            return false;
        }
    }

    private void TryDeleteDpapiQuiet()
    {
        try
        {
            _ = TryDeleteDpapi(out _);
        }
        catch
        {
            // 凭据管理器已经成功；清旧回退文件失败不影响本次保存。
        }
    }

    // ------------------------------------------------------------------
    // 错误文案
    // ------------------------------------------------------------------

    private void ClearError() => LastError = "";

    private void SetError(string message) =>
        LastError = LogSanitizer.Sanitize(message);

    /// <summary>
    /// DPAPI 加密文件里的明文 JSON 结构。私有的，且 ToString 不输出字段。
    /// </summary>
    private sealed class DpapiPayload
    {
        [JsonPropertyName("username")]
        public string Username { get; set; } = "";

        [JsonPropertyName("password")]
        public string Password { get; set; } = "";

        public override string ToString() => "DpapiPayload(凭据已隐藏)";
    }

    // ------------------------------------------------------------------
    // P/Invoke
    // ------------------------------------------------------------------

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct CREDENTIAL
    {
        public uint Flags;
        public uint Type;
        public string? TargetName;
        public string? Comment;
        public FILETIME LastWritten;
        public uint CredentialBlobSize;
        public IntPtr CredentialBlob;
        public uint Persist;
        public uint AttributeCount;
        public IntPtr Attributes;
        public string? TargetAlias;
        public string? UserName;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct FILETIME
    {
        public uint LowDateTime;
        public uint HighDateTime;
    }

    [DllImport("advapi32.dll", EntryPoint = "CredWriteW", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool CredWriteW(ref CREDENTIAL userCredential, uint flags);

    [DllImport("advapi32.dll", EntryPoint = "CredReadW", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool CredReadW(
        string targetName,
        uint type,
        uint flags,
        out IntPtr credential);

    [DllImport("advapi32.dll", EntryPoint = "CredDeleteW", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool CredDeleteW(string targetName, uint type, uint flags);

    [DllImport("advapi32.dll", EntryPoint = "CredFree", SetLastError = false)]
    private static extern void CredFree(IntPtr buffer);
}
