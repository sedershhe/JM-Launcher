#requires -Version 5.1
<#
.SYNOPSIS
    构建 JML 便携版（框架依赖 / 可选自包含）并打包为 dist\JML-portable-<版本>-<日期>.zip。

.DESCRIPTION
    步骤：
      1. dotnet publish（默认 -r win-x64 --self-contained false）；
      2. 复制 Python\ 源码（排除测试/缓存）与 plugins\ 四个插件；
      3. 生成 README.txt（运行要求 / 数据目录 / SmartScreen / 自检命令）；
      4. 校验包内必需文件，并确认没有把用户数据（settings/账号/日志/Draw/Delete）打进去；
      5. Compress-Archive 打包。

    安全边界：脚本只清理 dist\ 下形如 JML-portable-* 的自身产物，
    绝不删除源码、plugins\、Python\ 或用户数据目录。

.PARAMETER Configuration
    Release（默认）或 Debug。

.PARAMETER Version
    版本号；省略时读 JML.csproj 的 <Version>（当前 1.0.0）。

.PARAMETER DistRoot
    产物目录；默认 <仓库根>\dist。

.PARAMETER SelfContained
    生成自包含包（体积显著增大，但用户无需安装 .NET 10 桌面运行时）。
    默认框架依赖版：体积小，运行需 .NET 10 桌面运行时。

.EXAMPLE
    pwsh -File scripts\publish-portable.ps1

.EXAMPLE
    pwsh -File scripts\publish-portable.ps1 -SelfContained
#>
[CmdletBinding()]
param(
    [ValidateSet('Debug', 'Release')]
    [string]$Configuration = 'Release',

    [string]$Version = '',

    [string]$DistRoot = '',

    [switch]$SelfContained
)

$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# 路径与参数
# ---------------------------------------------------------------------------

$repoRoot = Split-Path -Parent $PSScriptRoot
$projectPath = Join-Path $repoRoot 'JML.csproj'
if (-not (Test-Path -LiteralPath $projectPath)) {
    throw "找不到项目文件：$projectPath"
}

if ([string]::IsNullOrWhiteSpace($DistRoot)) {
    $DistRoot = Join-Path $repoRoot 'dist'
}
$distFull = [System.IO.Path]::GetFullPath($DistRoot)
$repoFull = [System.IO.Path]::GetFullPath($repoRoot).TrimEnd('\', '/')
if ($distFull.TrimEnd('\', '/') -eq $repoFull) {
    throw "DistRoot 不能是仓库根目录：$distFull"
}

if ([string]::IsNullOrWhiteSpace($Version)) {
    [xml]$projectXml = Get-Content -LiteralPath $projectPath -Raw -Encoding UTF8
    $versionNode = @($projectXml.Project.PropertyGroup) |
        Where-Object { $null -ne $_ -and -not [string]::IsNullOrWhiteSpace($_.Version) } |
        Select-Object -First 1
    $Version = if ($null -ne $versionNode) { [string]$versionNode.Version } else { '1.0.0' }
}
$Version = $Version.Trim()
if ([string]::IsNullOrWhiteSpace($Version)) {
    throw '版本号为空：请在 JML.csproj 设置 <Version> 或用 -Version 指定。'
}

$dateTag = Get-Date -Format 'yyyyMMdd'
$packageName = "JML-portable-$Version-$dateTag"
$stage = Join-Path $distFull $packageName
$zipPath = Join-Path $distFull ($packageName + '.zip')

Write-Host "仓库根：$repoRoot"
Write-Host "版本：$Version    日期：$dateTag"
Write-Host "产物目录：$distFull"

# ---------------------------------------------------------------------------
# 清理：只删本脚本生成的 dist\JML-portable-*（目录与 zip）
# ---------------------------------------------------------------------------

if (-not (Test-Path -LiteralPath $distFull)) {
    New-Item -ItemType Directory -Path $distFull -Force | Out-Null
}

Get-ChildItem -LiteralPath $distFull -Force | Where-Object {
    $_.Name -like 'JML-portable-*'
} | ForEach-Object {
    Write-Host "清理旧产物：$($_.FullName)"
    Remove-Item -LiteralPath $_.FullName -Recurse -Force
}

# ---------------------------------------------------------------------------
# 1. dotnet publish
# ---------------------------------------------------------------------------

$selfContainedValue = if ($SelfContained) { 'true' } else { 'false' }
Write-Host ""
Write-Host "==> dotnet publish（self-contained=$selfContainedValue，win-x64，$Configuration）"
$publishArgs = @(
    'publish', $projectPath,
    '-c', $Configuration,
    '-r', 'win-x64',
    '--self-contained', $selfContainedValue,
    '-o', $stage,
    '--nologo',
    '-v', 'minimal'
)
& dotnet @publishArgs
if ($LASTEXITCODE -ne 0) {
    throw "dotnet publish 失败（退出码 $LASTEXITCODE）"
}

# ---------------------------------------------------------------------------
# 2. 复制 Python 源码与 plugins（排除缓存/测试/编译产物）
# ---------------------------------------------------------------------------

function Copy-FilteredTree {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination,
        [string[]]$SkipDirectoryNames = @('__pycache__', '.pytest_cache'),
        [string[]]$SkipFileExtensions = @('.pyc')
    )

    $sourceFull = (Resolve-Path -LiteralPath $Source).Path.TrimEnd('\', '/')
    Get-ChildItem -LiteralPath $sourceFull -Recurse -File -Force | ForEach-Object {
        $relative = $_.FullName.Substring($sourceFull.Length).TrimStart('\', '/')
        $segments = $relative -split '[\\/]'
        foreach ($segment in $segments) {
            if ($SkipDirectoryNames -contains $segment) {
                return
            }
        }

        if ($SkipFileExtensions -contains $_.Extension.ToLowerInvariant()) {
            return
        }

        $target = Join-Path $Destination $relative
        $targetDir = Split-Path -Parent $target
        if (-not (Test-Path -LiteralPath $targetDir)) {
            New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
        }
        Copy-Item -LiteralPath $_.FullName -Destination $target -Force
    }
}

$pythonSource = Join-Path $repoRoot 'Python'
if (-not (Test-Path -LiteralPath $pythonSource)) {
    throw "找不到 Python 源码目录：$pythonSource"
}

Write-Host "==> 复制 Python 源码"
Copy-FilteredTree -Source $pythonSource -Destination (Join-Path $stage 'Python') `
    -SkipDirectoryNames @('__pycache__', '.pytest_cache', 'tests')

$pluginsSource = Join-Path $repoRoot 'plugins'
if (-not (Test-Path -LiteralPath $pluginsSource)) {
    throw "找不到插件目录：$pluginsSource"
}

$pluginDirs = @(Get-ChildItem -LiteralPath $pluginsSource -Directory | Where-Object {
    Test-Path -LiteralPath (Join-Path $_.FullName 'plugin.json')
})
if ($pluginDirs.Count -eq 0) {
    throw "plugins\ 下没有找到任何 plugin.json：$pluginsSource"
}

Write-Host "==> 复制插件（$($pluginDirs.Count) 个）"
foreach ($pluginDir in $pluginDirs) {
    Copy-FilteredTree -Source $pluginDir.FullName `
        -Destination (Join-Path (Join-Path $stage 'plugins') $pluginDir.Name)
}

$requirementsSource = Join-Path $repoRoot 'requirements.txt'
if (Test-Path -LiteralPath $requirementsSource) {
    Copy-Item -LiteralPath $requirementsSource -Destination (Join-Path $stage 'requirements.txt') -Force
}

# ---------------------------------------------------------------------------
# 3. 生成 README.txt
# ---------------------------------------------------------------------------

$runtimeSection = if ($SelfContained) {
    @'
.NET 10 桌面运行时：已随包自带（self-contained），无需单独安装。
'@
} else {
    @'
.NET 10 桌面运行时：必须安装（本包是框架依赖版）。
  下载：https://dotnet.microsoft.com/download/dotnet/10.0
  安装 x64 的 ".NET Desktop Runtime 10.0.x" 后运行 JML.exe。
'@
}

$pluginLines = foreach ($pluginDir in $pluginDirs) {
    $manifestPath = Join-Path $pluginDir.FullName 'plugin.json'
    try {
        $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
        "  - $($manifest.name)（$($manifest.id) v$($manifest.version)）：$($manifest.description)"
    }
    catch {
        "  - $($pluginDir.Name)（manifest 读取失败：$($_.Exception.Message)）"
    }
}

$readme = @"
JML — JM Launcher 便携版 v$Version（$dateTag）
============================================================

一、运行要求
------------------------------------------------------------
1. Windows 10 / 11（64 位）。
2. $runtimeSection
3. Python 3.11 ~ 3.15（64 位），安装时勾选 "Add python.exe to PATH"。
   若安装在非标准位置，可设置环境变量 JML_PYTHON_HOME 指向 Python 安装目录。
4. 安装 Python 依赖（在本目录打开命令行执行）：
       python -m pip install -r requirements.txt

二、首次启动
------------------------------------------------------------
1. 双击 JML.exe。未签名程序首次运行会被 SmartScreen 拦截，见第五节。
2. 程序启动时会检测 Python 与依赖；若提示缺失，按提示安装后重启。
3. 进入主界面前会显示 Python 环境检测结果；全部通过后进入主界面。

三、数据目录（不会写进安装目录）
------------------------------------------------------------
%APPDATA%\JML
  - settings.json               主设置
  - plugins\<id>\config.json    插件设置
  - plugins\disabled.json       已停用的插件 id（唯一真源）
  - account.dat                 仅在 Windows 凭据管理器不可用时使用的 DPAPI 密文

%LOCALAPPDATA%\JML
  - data\                       运行数据
  - logs\                       诊断与自检日志
  - cache\                      缓存
  - tmp\                        下载临时目录

卸载：直接删除本程序目录；如需彻底清理，可自行删除上述两个 JML 目录。

四、内置插件
------------------------------------------------------------
$($pluginLines -join "`r`n")

说明：插件停用/启用后需重启程序才真正生效（插件管理页会提示）。

五、SmartScreen 与代码签名
------------------------------------------------------------
本程序未做代码签名，首次运行时 Windows 可能提示：
  "Windows 已保护你的电脑"
这是 SmartScreen 对未知发布者的常规提示，并非检测到病毒。

继续运行的方法：
  1. 点击提示框的"更多信息"；
  2. 点击"仍要运行"。

如果希望彻底消除提示：使用受信任的代码签名证书对 JML.exe 签名
（例如 EV 代码签名证书），并在发布前用 signtool 签名。

六、自检命令（无界面）
------------------------------------------------------------
在 PowerShell 中执行：
    `$p = Start-Process .\JML.exe -ArgumentList '--selfcheck' -Wait -PassThru
    `$p.ExitCode

退出码：0 = 全部通过；1 = 存在失败项（失败原因见输出/日志）。
完整文本同时写入：
    %LOCALAPPDATA%\JML\logs\selfcheck-<时间戳>.log

自检项：Python 运行时、依赖库、插件扫描、插件装配、服务注册表、配置契约。
注意：JML.exe 是无控制台程序，直接在 cmd 中运行可能看不到输出，
以退出码与日志文件为准。

七、已知限制
------------------------------------------------------------
1. 搜索页页数补全最多等待 3 秒；补不到时显示"页数: -"。
2. 详情窗仍使用系统标题栏（与主界面的 PCL2 风格不完全一致）。
3. 首次运行需要本机已安装 Python 与依赖，未做 Python 运行时打包。
4. 本包不包含任何用户配置、账号、日志或下载产物。
"@

$readmePath = Join-Path $stage 'README.txt'
[System.IO.File]::WriteAllText($readmePath, $readme, (New-Object System.Text.UTF8Encoding($true)))
Write-Host "==> 已生成 README.txt"

# ---------------------------------------------------------------------------
# 4. 打包前校验
# ---------------------------------------------------------------------------

Write-Host "==> 校验包内必需文件"
$requiredFiles = @(
    'JML.exe',
    'JML.dll',
    'requirements.txt',
    'README.txt',
    'Python\jml_backend.py',
    'Python\jml\host\loader.py',
    'Python\jml\services\host_services.py'
)
foreach ($pluginDir in $pluginDirs) {
    $requiredFiles += "plugins\$($pluginDir.Name)\plugin.json"
    $requiredFiles += "plugins\$($pluginDir.Name)\main.py"
}
foreach ($relative in $requiredFiles) {
    $full = Join-Path $stage $relative
    if (-not (Test-Path -LiteralPath $full)) {
        throw "发布包缺少必需文件：$relative"
    }
}

if (-not $SelfContained) {
    $runtimeConfigPath = Join-Path $stage 'JML.runtimeconfig.json'
    if (-not (Test-Path -LiteralPath $runtimeConfigPath)) {
        throw '框架依赖版缺少 JML.runtimeconfig.json。'
    }
}

Write-Host "==> 校验没有用户数据/临时目录"
$forbiddenFileNames = @(
    'settings.json', 'account.dat', 'disabled.json', 'config.json',
    'credentials.json', 'secrets.json', 'option.yml'
)
$forbiddenDirectoryNames = @(
    '__pycache__', '.pytest_cache', 'logs', 'Draw', 'Delete', 'bin', 'obj', 'JML.Tests', '.git'
)
$violations = @()
Get-ChildItem -LiteralPath $stage -Recurse -Force | ForEach-Object {
    $relative = $_.FullName.Substring($stage.TrimEnd('\', '/').Length).TrimStart('\', '/')
    if (-not $_.PSIsContainer -and $forbiddenFileNames -contains $_.Name) {
        $violations += $relative
    }
    elseif ($_.PSIsContainer -and $forbiddenDirectoryNames -contains $_.Name) {
        $violations += $relative + '\'
    }

    if (-not $_.PSIsContainer -and $_.Extension.ToLowerInvariant() -eq '.pyc') {
        $violations += $relative
    }
}
if ($violations.Count -gt 0) {
    throw "发布包包含禁止内容：`r`n  " + ($violations -join "`r`n  ")
}

# ---------------------------------------------------------------------------
# 5. 打包
# ---------------------------------------------------------------------------

Write-Host "==> 压缩为 $packageName.zip"
Compress-Archive -Path (Join-Path $stage '*') -DestinationPath $zipPath -CompressionLevel Optimal
if (-not (Test-Path -LiteralPath $zipPath)) {
    throw "压缩失败：$zipPath"
}

$zipItem = Get-Item -LiteralPath $zipPath
$zipSizeMb = [math]::Round($zipItem.Length / 1MB, 2)
$stageFiles = @(Get-ChildItem -LiteralPath $stage -Recurse -File -Force)

Write-Host ""
Write-Host "发布完成"
Write-Host "  解压目录：$stage"
Write-Host "  压缩包：$zipPath"
Write-Host "  大小：$zipSizeMb MB"
Write-Host "  文件数：$($stageFiles.Count)"
Write-Host "  插件数：$($pluginDirs.Count)"
Write-Host "  框架：$(if ($SelfContained) { 'self-contained' } else { 'framework-dependent（需 .NET 10 桌面运行时）' })"
Write-Host ""
Write-Host "自检：在解压目录执行  .\JML.exe --selfcheck"
Write-Host "      退出码 0=通过，1=有失败项；日志在 %LOCALAPPDATA%\JML\logs"
