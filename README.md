# JM Launcher

JM-Launcher 是基于 python 库 jmcomic 开发的可视化桌面程序。

本项目后端采用 python 开发，核心库来自 https://github.com/hect0x7/JMComic-Crawler-Python ，前端界面采用 https://github.com/PCL-Community/PCL-CE 和 https://github.com/Meloong-Git/PCL 提供的控件和动画方案，衷心感谢社区开源大佬的贡献。

注意：本项目为黑盒开发，技术细节由 ai 辅助完成，可能有未知的边界条件未覆盖，进而导致未知的 bug。

简介：如你所见，这个程序由简单的几个功能组成，分别是下载、搜索、插件、登录。其中内置了四个插件：**收藏夹**：你可以登录后打开这个插件浏览你账号中的收藏夹；**下载历史**：你可以浏览使用本程序的下载历史；**zip 浏览**：你可以指定某个文件夹，然后浏览这个文件夹内打包为 zip 的本子（该插件已经废弃，作为前置存在）；**zip 网页浏览**：你可以指定某个文件夹，唤起本地浏览器进程畅快浏览本地打包为 zip 的本子，需要上一个插件作为前置。**(注意,此处zip指本程序自动打包的纯.webp压缩包,其余情况未测试)**

\---

## 运行环境要求

本项目是 **Windows 桌面程序**，采用 C# 前端 + Python 后端的结构，因此**两端环境都要具备**。

### 一、Windows 端

|项|要求|
|-|-|
|操作系统|Windows x64（仅 Windows，依赖 WPF）|
|.NET 运行时|**.NET 10.0（net10.0-windows）桌面运行时**|
|架构|x64（64 位）|

> 便携版不含自带运行时，需本机已安装 .NET 10 桌面运行时。
> 未安装时会提示“缺少 .NET 运行时”，前往微软官网下载 `.NET Desktop Runtime 10.0` 即可。

### 二、Python 端（必须自行安装）

前端通过 **pythonnet** 调用 Python，**不会自带 Python**，需要你先安装：

|项|要求|
|-|-|
|Python 版本|**3.11 \~ 3.15**，推荐 **3.13**|
|架构|**64 位**（必须与程序一致，32 位不可用）|
|安装选项|安装时勾选 **Add python.exe to PATH**（否则程序找不到）|

安装后需安装依赖库，在本项目目录打开命令行执行：

```bash
python -m pip install -r requirements.txt
```

依赖清单（`requirements.txt`）：

|库|版本|用途|
|-|-|-|
|jmcomic|2.7.1|核心业务库（搜索 / 详情 / 下载 / 封面）|
|pillow|12.3.0|图片解码与像素校验|
|PyYAML|6.0.3|配置文件读写|

> jmcomic 自身还会带出 `commonx`、`curl-cffi`（网络传输）、`pycryptodome`（图片解密）等依赖，pip 会自动安装。
>
> 若 Python 装在非标准位置，可设置环境变量 `JML\_PYTHON\_HOME` 指向 Python 安装目录。

\---

## 运行方式

1. 解压便携版压缩包到任意目录
2. 确认已安装 **.NET 10 桌面运行时** 与 **Python 3.11\~3.15（64 位）** 及依赖库
3. 双击 `JML.exe` 启动

首次启动若缺少 Python 环境，程序会给出中文检测报告与安装指引。

### 无界面自检

```bash
JML.exe --selfcheck
```

不启动界面，只输出环境诊断结果（Python、依赖、插件加载情况），用于排查环境问题。(为什么我不打包成傻子包呢?首先排除我懒)

\---

## 内置插件

程序自带 4 个插件（位于 `plugins/` 目录），均为协议版本 `1`：

|插件 ID|名称|说明|
|-|-|-|
|download\_history|下载历史|浏览本程序的下载历史记录|
|favorites|收藏夹|登录后浏览账号收藏夹|
|zip\_browser|ZIP 浏览|浏览文件夹内打包为 zip 的本子（已废弃，作为前置存在）|
|zip\_web|ZIP 网页浏览|唤起本地浏览器流畅浏览（依赖 zip\_browser）|

> zip_web 依赖 zip_browser 提供的服务；若停用 zip_browser，zip_web 会明确报错而非崩溃。

\---

## 数据与凭据存储

|项|位置|
|-|-|
|配置 / 状态|`%APPDATA%\JML`|
|数据 / 日志 / 缓存|`%LOCALAPPDATA%\JML`|
|账号凭据|**Windows 凭据管理器**（首选）；不可用时回退 `%APPDATA%\JML\account.dat`（DPAPI 加密）|

* 账号凭据**不写入程序目录**，便携版拷贝到其他电脑不会带走账号
* 凭据**绝不明文存储**；两者均不可用时不保存账号，仅给出提示

> 极端情况：当 `%APPDATA%` 与系统临时目录都不可写时，数据会降级到程序目录下的 `runtime\_data`。
> 此时拷贝程序目录会带走数据文件，请注意。

\---

## 开发与构建

|项|说明|
|-|-|
|目标框架|`net10.0-windows`（WPF）|
|桥接|pythonnet 3.2.1|
|UI 库|PlainToolkit.UI（PCL2 风格控件与主题）|
|测试|xUnit 2.9.3 / pytest|

```bash
# 构建
dotnet build JML.csproj

# C# 测试
dotnet test JML.Tests/JML.Tests.csproj

# Python 测试
python -m pytest Python/tests

# 打包便携版
scripts/publish-portable.ps1
```

\---

## 致谢

* [JMComic-Crawler-Python](https://github.com/hect0x7/JMComic-Crawler-Python) — 核心业务库 jmcomic
* [PCL-Community/PCL-CE](https://github.com/PCL-Community/PCL-CE) 与 [Meloong-Git/PCL](https://github.com/Meloong-Git/PCL) — 界面控件与动画方案
* [PlainToolkit.UI](https://github.com/PCL-Community) — PCL2 风格 WPF 控件库


