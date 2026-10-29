# plugin_sdk：插件开发套件

这个目录放"写新插件要用的一切"：协议规范、空白模板、一个不用启动主程序就能跑插件的加载器。

| 文件 | 作用 |
| --- | --- |
| [`20261029_JML插件协议规范_v1.0.md`](20261029_JML插件协议规范_v1.0.md) | 协议全文（字段、页面模板、服务、事件、错误码）。改宿主协议相关代码时必须同步改它 |
| [`loader.py`](loader.py) | 独立加载器：校验清单、装配插件、试跑页面与动作 |
| [`template/`](template/) | 空白插件模板，复制出去改名就能用 |
| `template/plugin.json` / `main.py` / `config.schema.json` | 模板的三个文件（JSON 不支持注释，字段说明见 `template/README.md` 和协议【三】【四】） |
| [`template/README.md`](template/README.md) | 模板使用说明：复制后要改哪几处 |

现有的四个插件（`plugins/download_history/`、`favorites/`、`zip_browser/`、`zip_web/`）是参考实现，遇到"协议没说清"的地方直接对照它们的代码。

---

## 三步上手

**第一步：复制模板。**

```bat
xcopy /E /I plugin_sdk\template plugins\my_plugin
```

目录名建议与 `plugin.json` 里的 `id` 一致（`id` 决定数据目录、配置路径和模块名）。

**第二步：改三处身份信息。**

- `plugin.json`：`id`、`name`、`description`，以及 `pages[].id`（推荐写成 `{id}.main`）
- `main.py`：`PAGE_ID` 常量要与 `pages[].id` 完全一致
- `config.schema.json`：按需要保留或删除配置项；不需要配置就把 `plugin.json` 里的 `config` 整个删掉

**第三步：命令行自检，再进主程序。**

```bat
python plugin_sdk\loader.py check  plugins\my_plugin
python plugin_sdk\loader.py run    plugins\my_plugin
python plugin_sdk\loader.py page   plugins\my_plugin my_plugin.main --page 1
python plugin_sdk\loader.py action plugins\my_plugin my_plugin.main hello --row title=第一条
```

四条命令全过，说明清单、装配、加载钩子、动作钩子都没问题。剩下的只有在主程序里看渲染效果了。

想一次性看所有插件（含现有的四个）装配得怎么样：

```bat
python plugin_sdk\loader.py run plugins
```

---

## loader.py 说明

不启动 WPF，直接调宿主的 Python 侧（`Python/jml/host/`），所以行为和主程序一致。

| 子命令 | 做什么 | 什么时候用 |
| --- | --- | --- |
| `check` | 只读 `plugin.json` 做校验，不导入插件代码 | 改完清单先跑这个，报错信息与宿主逐字一致 |
| `run` | 完整装配：导入入口 → `setup(ctx)` → 打印成功/失败清单与服务表；也支持直接给根目录（如 `run plugins`），一次装全部 | 排查"为什么插件不出现" |
| `page` | 调页面加载钩子，打印完整信封（`template` / `title` / `data`） | 核对字段名是否被宿主认得 |
| `action` | 按宿主真实 payload 调动作钩子（`row` / `row_index` / `page`） | 核对行按钮逻辑 |

几个约定：

- **数据目录默认落系统临时目录**（`%TEMP%\jml_plugin_sdk`），不会污染 `%APPDATA%\JML`；要固定目录就加 `--data-root 路径`（放在子命令前后都行）。
- **宿主服务会自动注册**（`jm.search` / `jm.detail` / `jm.download` / `jm.account` / `jm.session`），所以依赖它们的插件在命令行里也能正常装配；注册失败会打一行提示，不影响其它插件。
- `page --param folder=D:/comics` 可以模拟 `browser` / `action` 模板的文件夹参数。
- `action --row title=第一条 --index 0` 构造行 payload；也可以用 `--payload-json '{"row":{...}}'` 直接给完整 payload。
- 出错时打印 `[错误码] 消息`，错误码与主程序界面显示的一致。
- 工具本身只读插件代码，不会修改你的插件目录。

---

## 和主程序的关系

```
plugin_sdk/loader.py  ──调用──>  Python/jml/host/{manifest,loader,pages}.py  ──同一套代码──>  C# 界面
        ↑                                    ↑
   命令行自检                        主程序启动时装配
```

宿主对插件的所有校验、装配、钩子调用都在 Python 侧，C# 只负责渲染返回的 JSON。所以命令行里能跑通的插件，进主程序只可能差在"字段名写错导致界面不显示"这类渲染细节上——规范【六】的字段表就是用来对这件事的。

---

## 改协议时要同步的地方

协议不是只写不动的。改动下面任何一处，都必须同步更新协议文档，不兼容的变更要提升 `api_version`：

| 改了宿主这里 | 影响文档哪一节 |
| --- | --- |
| `Python/jml/host/manifest.py` 的字段校验 | 【三、plugin.json 字段规范】 |
| `Python/jml/host/config.py` 的配置规则 | 【四、config.schema.json 配置规范】 |
| `Python/jml/host/plugin_context.py` 的 ctx 接口 | 【五、main.py 与 setup(ctx)】 |
| `Python/jml/host/pages.py` 与 `Bridge/Dto/PluginPageDtos.cs` 的字段 | 【六、页面模板契约】 |
| `Python/jml/services/host_services.py` 的服务方法 | 【七、服务协作】 |
| `Python/jml/core/protocol.py` 的事件与 `Python/jml/events.py` 的校验 | 【八、事件总线】 |
| `Python/jml/core/errors.py` 的错误码与 `classify` | 【九、错误处理与诊断】 |
