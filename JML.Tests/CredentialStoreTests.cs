using System.IO;
using System.Text;
using JML.Infrastructure;
using Xunit;

namespace JML.Tests;

/// <summary>
/// 凭据存储离线测试（不碰真实 Windows 凭据管理器）。
///
/// 全部使用 <c>useCredentialManager: false</c> + 临时 DPAPI 文件路径，
/// 只验证回退分支的保存/读取/删除往返与“文件中无明文”。
/// </summary>
public class CredentialStoreTests
{
    [Fact]
    public void Dpapi_RoundTrips_AndFileContainsNoPlaintext()
    {
        string directory = CreateTempDirectory();
        string file = Path.Combine(directory, "account.dat");
        const string username = "test-account-user";
        const string password = "P@ssw0rd-test-987";

        try
        {
            var store = new CredentialStore(
                dpapiPath: file,
                useCredentialManager: false,
                targetName: "JML/test-account");

            Assert.True(store.Save(username, password), store.LastError);
            Assert.True(File.Exists(file), "DPAPI 回退文件没有生成。");
            Assert.True(store.Exists());
            Assert.Equal(CredentialStorageBackend.DpapiFile, store.LastBackend);

            var loaded = store.Load();
            Assert.NotNull(loaded);
            Assert.Equal(username, loaded.Value.Username);
            Assert.Equal(password, loaded.Value.Password);
            Assert.Equal(CredentialStorageBackend.DpapiFile, store.LastBackend);

            // 密文文件里不能出现明文账号/密码（UTF-8 / UTF-16 / Latin1 三种视角都查）。
            byte[] cipher = File.ReadAllBytes(file);
            foreach (string view in new[]
                     {
                         Encoding.UTF8.GetString(cipher),
                         Encoding.Unicode.GetString(cipher),
                         Encoding.Latin1.GetString(cipher),
                     })
            {
                Assert.DoesNotContain(username, view);
                Assert.DoesNotContain(password, view);
            }

            Assert.DoesNotContain(username, store.ToString());
            Assert.DoesNotContain(password, store.ToString());

            Assert.True(store.Delete(), store.LastError);
            Assert.False(store.Exists());
            Assert.Null(store.Load());
            Assert.Equal("", store.LastError);
            Assert.Equal(CredentialStorageBackend.None, store.LastBackend);
        }
        finally
        {
            DeleteTempDirectory(directory);
        }
    }

    [Fact]
    public void CredentialManager_RoundTrips_WithIsolatedTarget_AndDeletesInFinally()
    {
        string directory = CreateTempDirectory();
        string file = Path.Combine(directory, "account.dat");
        string target = "JML/test-account-" + Guid.NewGuid().ToString("N");
        const string username = "cm-test-user";
        const string password = "cm-test-password-741";

        var store = new CredentialStore(
            dpapiPath: file,
            useCredentialManager: true,
            targetName: target);

        try
        {
            Assert.True(store.Save(username, password), store.LastError);
            Assert.Equal(CredentialStorageBackend.CredentialManager, store.LastBackend);
            Assert.Equal("", store.LastError);
            Assert.False(File.Exists(file), "凭据管理器成功后不应留下 DPAPI 回退文件。");

            // 新实例重新读，证明是真的落在凭据管理器而不是内存缓存。
            var reread = new CredentialStore(
                dpapiPath: file,
                useCredentialManager: true,
                targetName: target);
            var loaded = reread.Load();

            Assert.NotNull(loaded);
            Assert.Equal(username, loaded.Value.Username);
            Assert.Equal(password, loaded.Value.Password);
            Assert.Equal(CredentialStorageBackend.CredentialManager, reread.LastBackend);
            Assert.Equal("", reread.LastError);
            Assert.DoesNotContain(password, reread.LastError);
            Assert.DoesNotContain(password, reread.ToString());
        }
        finally
        {
            // 独立目标名：无论断言是否失败都清掉测试凭据，不碰 JML/jm-account。
            store.Delete();
            DeleteTempDirectory(directory);
        }
    }

    [Fact]
    public void Save_WhenBothBackendsUnavailable_ReturnsChineseFailureAndNoPlaintext()
    {
        string directory = CreateTempDirectory();
        const string password = "DoNotLeakThisPassword";

        try
        {
            // 把目录路径当作文件路径使用：DPAPI 写入必然失败；凭据管理器显式关闭。
            var store = new CredentialStore(
                dpapiPath: directory,
                useCredentialManager: false,
                targetName: "JML/test-account");

            Assert.False(store.Save("some-user", password));
            Assert.Contains(CredentialStore.BothBackendsUnavailableMessage, store.LastError);
            Assert.DoesNotContain(password, store.LastError);
            Assert.Equal(CredentialStorageBackend.None, store.LastBackend);
        }
        finally
        {
            DeleteTempDirectory(directory);
        }
    }

    [Fact]
    public void Save_RejectsEmptyCredentials()
    {
        string directory = CreateTempDirectory();
        string file = Path.Combine(directory, "account.dat");

        try
        {
            var store = new CredentialStore(
                dpapiPath: file,
                useCredentialManager: false,
                targetName: "JML/test-account");

            Assert.False(store.Save("", "x"));
            Assert.Contains("不能为空", store.LastError);
            Assert.False(store.Save("user", ""));
            Assert.Contains("不能为空", store.LastError);
            Assert.False(File.Exists(file));
        }
        finally
        {
            DeleteTempDirectory(directory);
        }
    }

    [Fact]
    public void AppSettings_HasNoCredentialField()
    {
        string[] names = typeof(AppSettings).GetProperties()
            .Select(p => p.Name)
            .ToArray();

        Assert.DoesNotContain(names, name =>
            name.Contains("Password", StringComparison.OrdinalIgnoreCase) ||
            name.Contains("Credential", StringComparison.OrdinalIgnoreCase) ||
            name.Contains("Account", StringComparison.OrdinalIgnoreCase) ||
            name.Contains("Token", StringComparison.OrdinalIgnoreCase));
    }

    [Fact]
    public void DownloadDirectory_UsesExistingPath_ElseDesktop()
    {
        string directory = CreateTempDirectory();
        try
        {
            Assert.Equal(directory, AppSettings.ResolveDownloadDirectory(directory));

            string desktop = Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory);
            Assert.Equal(desktop, AppSettings.ResolveDownloadDirectory(""));
            Assert.Equal(desktop, AppSettings.ResolveDownloadDirectory(Path.Combine(directory, "missing")));
        }
        finally
        {
            DeleteTempDirectory(directory);
        }
    }

    private static string CreateTempDirectory()
    {
        // 测试进程所在目录位于工作区内（不受系统 Temp 的 ACL 限制），
        // 用独立随机子目录并在 finally 中删除，不写真实凭据管理器。
        string directory = Path.Combine(
            AppContext.BaseDirectory,
            "jml-credential-test-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(directory);
        return directory;
    }

    private static void DeleteTempDirectory(string directory)
    {
        try
        {
            if (Directory.Exists(directory))
            {
                Directory.Delete(directory, recursive: true);
            }
        }
        catch
        {
            // 清理失败不掩盖真正的断言失败；临时目录位于测试输出目录下。
        }
    }
}
