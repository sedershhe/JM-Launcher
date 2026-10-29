"""空白插件模板：拿到一个新插件目录里改。

改这三处就能跑：
1. 下面的 PAGE_ID 与 plugin.json 里 pages[].id 保持一致；
2. 把「我的页面」的标题、列、行数据换成你自己的；
3. 需要额外动作就在 pages[] 的插件里加页面、在 setup 里多注册几个动作。

本文件只依赖宿主提供的 ctx，不要 import 宿主内部模块（jml.host / jml.core 里
的协议常量除外——想抛带错误码的异常时可以 import jml.core.errors）。
"""

from __future__ import annotations

# 必须与 plugin.json 里 pages[].id 完全一致，否则注册时会被拒绝。
PAGE_ID = "my_plugin.main"

# 每页条数的兜底值；真实值从配置里读（config.schema.json 的 page_size）。
DEFAULT_PAGE_SIZE = 20


class MyPluginPage:
    """一个页面对象：持有 ctx，提供加载钩子和若干动作钩子。"""

    def __init__(self, ctx) -> None:
        self._ctx = ctx

    # ---------- 加载钩子 ----------

    def load(self, params: dict) -> dict:
        """宿主每次进页面 / 翻页 / 改筛选都会调用，返回值就是页面数据。

        params 里能拿到什么：
        - params["page"]        页码，从 1 开始（list 模板）
        - params["folder"]      选中的文件夹（仅 browser / action 模板，可能没有）
        - 筛选值平铺在顶层：返回 filters[].key = "category" 时，宿主会传
          params["category"] = 当前选中值（不是塞进 params["filters"]）
        """
        page = self._page_of(params)
        page_size = self._page_size()

        # TODO: 换成你自己的数据来源。
        #   需要网络/下载能力时用 ctx.call_service(...) 调宿主服务，别自己起线程。
        all_items = self._load_items()
        start = (page - 1) * page_size
        page_items = all_items[start:start + page_size]

        return {
            "columns": [
                {"key": "title", "title": "标题", "width": 260},
                {"key": "updated", "title": "更新时间"},
            ],
            # 行里能用的键：title / author / book_id / result / page_count /
            # click_count / time / updated / path / description / thumb_book_id
            # 其它键会被原样带回行 payload，但界面上不显示。
            "rows": [
                {"title": item["title"], "updated": item.get("updated", "")}
                for item in page_items
            ],
            "page": page,
            "page_size": page_size,
            "total": len(all_items),
            "total_pages": max(1, (len(all_items) + page_size - 1) // page_size),
            "row_actions": [
                {"id": "hello", "title": "打个招呼", "primary": True},
            ],
            "row_double_click_action": "",
            "filters": [],
            "empty_text": "还没有内容。",
        }

    # ---------- 动作钩子 ----------

    def action_hello(self, payload: dict) -> dict:
        """行内按钮被点击。payload 结构固定：

        {"row": {整行数据}, "row_index": 行号, "page": 当前页}
        """
        row = payload.get("row") or {}
        title = str(row.get("title", ""))
        self._ctx.log(f"点击了行：{title}")

        # 返回约定：
        #   ok          成功与否
        #   refresh     true → 宿主重新调用 load() 刷新当前页
        #   message     显示在状态栏的提示
        #   intent      可选：{"kind": "detail"/"download", ...} 让宿主跳转
        return {"ok": True, "refresh": False, "message": f"已处理：{title}"}

    # ---------- 内部工具 ----------

    def _page_of(self, params: dict) -> int:
        try:
            page = int(params.get("page", 1) or 1)
        except (TypeError, ValueError):
            page = 1
        return max(1, page)

    def _page_size(self) -> int:
        try:
            return max(1, int(self._ctx.config_get("page_size")))
        except (TypeError, ValueError):
            return DEFAULT_PAGE_SIZE

    def _load_items(self) -> list[dict]:
        """示例数据。换成真实数据时记得把对象转成基本类型（str/int/bool/list/dict），
        datetime、Path、set 都不能直接返回，否则界面会显示 INVALID_DATA。"""
        return [
            {"title": "第一条", "updated": "2026-01-01 10:00"},
            {"title": "第二条", "updated": "2026-01-02 11:30"},
        ]


def setup(ctx) -> None:
    """宿主唯一入口：装配时调用一次（重新装配时会再调用一次，别放不可重入的状态）。

    可以在这里做的事：
    - ctx.get_service(name, version)        取依赖服务（清单里要在 services.requires 声明）
    - ctx.register_service(name, ver, ref)  提供能力给别的插件
    - ctx.subscribe(event_type, handler)    订阅宿主事件（清单外的事件会被拒绝）
    - ctx.config_get / config_set           读写配置
    """
    page = MyPluginPage(ctx)
    ctx.register_page(PAGE_ID, page.load)
    ctx.register_page_action(PAGE_ID, "hello", page.action_hello)
    ctx.log(f"{ctx.plugin_id} 已装载，数据目录：{ctx.data_dir}")
