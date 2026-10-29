# 空白插件模板

复制出去、改三处身份信息，就是一个能跑的新插件。

## 怎么用

```bat
xcopy /E /I plugin_sdk\template plugins\my_plugin
```

然后把目录名改成你的插件 id（建议与 `plugin.json` 里的 `id` 一致）。

## 复制后必须改的地方

| 文件 | 改什么 | 说明 |
| --- | --- | --- |
| `plugin.json` | `id` | 唯一标识，只能是英文小写+下划线；它决定数据目录、配置路径、模块名，改了等于换新插件 |
| `plugin.json` | `name` | 界面上显示的插件名 |
| `plugin.json` | `description` | 插件管理页里的一句话说明 |
| `plugin.json` | `pages[].id` | 推荐 `{插件id}.main`，同一插件内不得重复 |
| `plugin.json` | `pages[].title` | 插件功能页的标题 |
| `plugin.json` | `pages[].template` | `list` / `browser` / `action` 三选一（`form` 宿主暂不渲染） |
| `plugin.json` | `development_sample` | 改成 `false` 就不会被标成"开发样例" |
| `main.py` | `PAGE_ID` | 必须与 `pages[].id` 完全一致，否则注册会被拒绝、插件加载失败 |
| `main.py` | `MyPluginPage.load` | 换成你的数据来源与返回字段 |
| `main.py` | `_load_items` | 换成真实数据（记得把对象转成 str/int/bool/list/dict） |
| `config.schema.json` | 按需要增删 | 不需要配置就把 `plugin.json` 里的 `"config"` 整个删掉 |

`main.py` 里还留着 `action_hello` 这个动作示例，不需要就删掉它，同时删掉 `setup` 里对应的 `ctx.register_page_action(...)` 和 `load()` 里的 `row_actions` 项。

## 三个文件各自管什么

- **`plugin.json`**：静态声明，宿主导入代码前先读它。字段写错会直接加载失败，错误信息是中文，一条一条对着改。
- **`main.py`**：唯一入口是 `setup(ctx)`。页面钩子（加载/动作/图片）都在 `setup` 里注册，注册的 `page_id` 必须是清单里声明过的。
- **`config.schema.json`**：配置项定义，驱动设置界面自动生成控件。密码类项（`ui: password`）不得声明非空 `default`，也不会落盘。

## 自检顺序

```bat
python plugin_sdk\loader.py check  plugins\my_plugin
python plugin_sdk\loader.py run    plugins\my_plugin
python plugin_sdk\loader.py page   plugins\my_plugin my_plugin.main --page 1
python plugin_sdk\loader.py action plugins\my_plugin my_plugin.main hello --row title=第一条
```

四条都过之后，把插件目录放进主程序 `plugins/` 下重启，应该就能在插件页看到它。

## 新手最容易踩的五个坑

1. **`page_id` 拼错或没在 `pages[]` 里声明** → 插件加载失败，提示"页面未在 manifest 的 pages[] 中声明"。
2. **返回了 `datetime` / `Path` / `set`** → 界面显示 `INVALID_DATA`，因为这些不能 JSON 序列化。
3. **行字段名写错**（比如 `Name` 应为 `title`）→ 不报错，只是那一栏空白。字段表见协议【6.3】。
4. **`thumb_book_id` 填了图片路径** → 缩略图那张位置显示不出来；这个键要填 JM 本子号，由宿主去搜索接口取封面。
5. **在 `setup` 里做网络请求** → 启动变慢甚至卡死；网络操作放到钩子里，或调 `ctx.call_service` 让宿主后台跑。

更多细节看同目录的协议文档：[`../20261029_JML插件协议规范_v1.0.md`](../20261029_JML插件协议规范_v1.0.md)。
