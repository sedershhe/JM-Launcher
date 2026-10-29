"""JML 插件加载器（独立命令行工具）：不启动主程序，也能校验和试跑插件。

用途：写完插件后先在命令行把清单校验、页面加载、动作调用跑一遍，再丢进主程序。

用法（在仓库根目录执行）：
    python plugin_sdk/loader.py check  plugin_sdk/template
    python plugin_sdk/loader.py run    plugin_sdk/template
    python plugin_sdk/loader.py page   plugin_sdk/template my_plugin.main --page 1
    python plugin_sdk/loader.py action plugin_sdk/template my_plugin.main hello --row title=第一条

四个子命令：
    check   只校验 plugin.json（不导入插件代码，等价于宿主管理页的"扫描"）
    run     真正装配：导入入口 → setup(ctx) → 打印装载结果
    page    调页面加载钩子，打印宿主会拿到的完整信封（template/title/data）
    action  调动作钩子，参数按宿主真实 payload 构造

约定：本工具只读插件代码，数据目录默认落系统临时区，不碰 %APPDATA%。
需要固定数据目录时加 `--data-root 路径`（放在子命令前后都行）。

真源：Python/jml/host/manifest.py、loader.py、pages.py。协议见同目录的
《20261029_JML插件协议规范_v1.0.md》。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile

# Windows 控制台默认 GBK，中文输出需要显式转 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
PY_SRC = os.path.join(REPO_ROOT, "Python")
if PY_SRC not in sys.path:
    sys.path.insert(0, PY_SRC)

from jml.core.errors import classify  # noqa: E402
from jml.host import loader, manifest, pages, paths  # noqa: E402


# ----------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------

def _print_json(data) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _fail(message: str, code: int = 1) -> int:
    print(f"[失败] {message}", file=sys.stderr)
    return code


def _register_host_services() -> None:
    """注册宿主服务（jm.search / jm.detail / jm.download / jm.account / jm.session）。

    插件依赖这些服务时，不注册就会误报"缺少服务"。导入失败（缺第三方库等）
    不算致命错误：这时只影响依赖宿主服务的插件，其他插件照常装配。
    """
    try:
        from jml.services.host_services import register_host_services
        register_host_services()
    except Exception as exc:  # noqa: BLE001
        print(f"[提示] 宿主服务注册失败（依赖 jm.* 的插件会报缺少服务）：{exc}",
              file=sys.stderr)


def _plugin_dir(raw: str) -> str:
    """把用户给的路径规范成插件目录绝对路径。"""
    path = os.path.abspath(raw)
    if os.path.isfile(path) and os.path.basename(path) == manifest.MANIFEST_FILENAME:
        return os.path.dirname(path)
    return path


def _plugins_root(plugin_dir: str) -> str:
    """装载器按"插件根目录"扫描，这里取插件目录的上级。"""
    return os.path.dirname(os.path.normpath(plugin_dir))


def _configure_data_root(raw: str | None) -> str:
    root = os.path.abspath(raw) if raw else os.path.join(
        tempfile.gettempdir(), "jml_plugin_sdk",
    )
    paths.configure(plugin_data_root=root)
    return root


def _run_root(root: str) -> int:
    """根目录模式：扫描根下所有插件目录，报告各自成败。"""
    print(f"装载插件根：{root}")
    print(f"数据根    ：{paths.plugin_data_root()}")
    try:
        report = loader.load_all(root)
    except Exception as exc:  # noqa: BLE001 - 装配异常要原样显示
        return _fail(f"装配失败：{exc}")

    loaded = [item["id"] for item in report["loaded"]]
    failed = report["failed"]
    print(f"\n成功 {len(loaded)} 个：{', '.join(loaded) or '（无）'}")
    for item in failed:
        reason = item.get("reason") or item.get("blocked_reason") or "（无原因）"
        print(f"失败 {item['id']}：{reason}")

    services = report.get("services") or {}
    if services:
        print("服务表：" + ", ".join(f"{k}/{v}" for k, v in sorted(services.items())))

    if failed:
        return _fail(f"{len(failed)} 个插件未能装配")
    print("\n[通过] 全部插件装配完成")
    return 0


def _assemble(plugin_dir: str) -> tuple[dict, dict, str]:
    """装配插件目录的上级目录，返回 (清单, 装载报告, 插件 id)。"""
    if not os.path.isfile(os.path.join(plugin_dir, manifest.MANIFEST_FILENAME)):
        raise SystemExit(_fail(
            f"{plugin_dir} 下没有 plugin.json；这个子命令要指到具体插件目录"
        ))
    try:
        info = manifest.load_manifest(plugin_dir)
    except Exception as exc:  # noqa: BLE001 - 清单错误要原样显示
        raise SystemExit(_fail(f"清单校验失败：{exc}"))
    report = loader.load_all(_plugins_root(plugin_dir))
    return info, report, info["id"]


def _require_loaded(report: dict, plugin_id: str) -> bool:
    loaded = {item["id"] for item in report["loaded"]}
    if plugin_id in loaded:
        return True
    for item in report["failed"]:
        if item["id"] == plugin_id:
            _fail(f"插件 {plugin_id} 未能装载：{item.get('reason') or item.get('blocked_reason')}")
            return False
    _fail(f"插件 {plugin_id} 没有出现在装载结果里（目录被跳过？）")
    return False


# ----------------------------------------------------------------------
# 子命令
# ----------------------------------------------------------------------

def cmd_check(args) -> int:
    """只读清单，不导入插件代码。"""
    plugin_dir = _plugin_dir(args.path)
    try:
        info = manifest.load_manifest(plugin_dir)
    except Exception as exc:  # noqa: BLE001
        return _fail(f"清单校验失败：{exc}")

    print(f"[通过] {info['id']}  v{info['version']}  api_version={info['api_version']}")
    print(f"  名称      {info['name']}")
    print(f"  说明      {info['description'] or '（空）'}")
    print(f"  入口      {info['entry']}")
    print(f"  数据目录  {os.path.join(paths.plugin_data_root(), info['id'])}")
    print(f"  开发样例  {'是' if info['development_sample'] else '否'}")

    deps = info["dependencies"]["plugins"]
    print(f"  依赖插件  {', '.join(deps) if deps else '（无）'}")

    provides = info["services"]["provides"]
    requires = info["services"]["requires"]
    fmt = lambda items: ", ".join(f"{s['name']}/{s['version']}" for s in items) or "（无）"
    print(f"  提供服务  {fmt(provides)}")
    print(f"  需要服务  {fmt(requires)}")

    print(f"  页面      {len(info['pages'])} 个")
    for page in info["pages"]:
        print(f"    - {page['id']}  [{page['template']}]  {page['title']}")

    has_config = bool(info["config"].get("schema"))
    print(f"  配置      {'有' if has_config else '（无）'}")
    return 0


def cmd_run(args) -> int:
    plugin_dir = _plugin_dir(args.path)
    if not os.path.isfile(os.path.join(plugin_dir, manifest.MANIFEST_FILENAME)):
        return _run_root(plugin_dir)

    info, report, plugin_id = _assemble(plugin_dir)
    print(f"装载插件根：{_plugins_root(_plugin_dir(args.path))}")
    print(f"数据根    ：{paths.plugin_data_root()}")

    loaded_ids = [item["id"] for item in report["loaded"]]
    failed = report["failed"]
    print(f"\n成功 {len(loaded_ids)} 个：{', '.join(loaded_ids) or '（无）'}")
    for item in failed:
        reason = item.get("reason") or item.get("blocked_reason") or "（无原因）"
        print(f"失败 {item['id']}：{reason}")

    services = report.get("services") or {}
    if services:
        print("服务表：" + ", ".join(f"{k}/{v}" for k, v in sorted(services.items())))

    if plugin_id not in loaded_ids:
        return _fail(f"{plugin_id} 未装载成功，后续 page/action 也无法调用")

    print(f"\n[通过] {plugin_id} 装配完成，可以继续用 page / action 子命令试跑")
    return 0


def cmd_page(args) -> int:
    info, report, plugin_id = _assemble(_plugin_dir(args.path))
    if not _require_loaded(report, plugin_id):
        return 1

    params: dict = {"page": args.page}
    for pair in args.param:
        key, value = _split_pair(pair)
        params[key] = _coerce(value)

    try:
        envelope = pages.load_page(plugin_id, args.page_id, params)
    except Exception as exc:  # noqa: BLE001
        info_err = classify(exc)
        return _fail(f"[{info_err.error_code}] {info_err.error_message}")

    print(f"请求参数：{json.dumps(params, ensure_ascii=False)}")
    _print_json(envelope)
    return 0


def cmd_action(args) -> int:
    info, report, plugin_id = _assemble(_plugin_dir(args.path))
    if not _require_loaded(report, plugin_id):
        return 1

    if args.payload_json:
        try:
            payload = json.loads(args.payload_json)
        except ValueError as exc:
            return _fail(f"--payload-json 不是合法 JSON：{exc}")
    else:
        row = {}
        for pair in args.row:
            key, value = _split_pair(pair)
            row[key] = value
        payload = {"row": row, "row_index": args.index, "page": args.page}

    try:
        result = pages.run_page_action(plugin_id, args.page_id, args.action_id, payload)
    except Exception as exc:  # noqa: BLE001
        info_err = classify(exc)
        return _fail(f"[{info_err.error_code}] {info_err.error_message}")

    print(f"动作 payload：{json.dumps(payload, ensure_ascii=False)}")
    _print_json(result)
    return 0


# ----------------------------------------------------------------------
# 命令行组装
# ----------------------------------------------------------------------

def _split_pair(text: str) -> tuple[str, str]:
    if "=" not in text:
        raise SystemExit(_fail(f"参数要写成 key=value，收到：{text!r}"))
    key, value = text.split("=", 1)
    return key.strip(), value


def _coerce(value: str):
    """命令行里的值统一按字符串传；纯数字与 true/false 转成对应类型，便于试跑。"""
    lowered = value.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    try:
        return int(value)
    except ValueError:
        return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loader.py",
        description="JML 插件加载器：校验清单、试跑页面与动作，不用启动主程序。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "例子：\n"
            "  python plugin_sdk/loader.py check  plugin_sdk/template\n"
            "  python plugin_sdk/loader.py run    plugins\n"
            "  python plugin_sdk/loader.py page   plugin_sdk/template my_plugin.main --page 2\n"
            "  python plugin_sdk/loader.py action plugin_sdk/template my_plugin.main hello "
            "--row title=第一条\n"
        ),
    )
    parser.add_argument(
        "--data-root",
        default=None,
        help="插件数据根（默认系统临时目录/jml_plugin_sdk，不碰 %%APPDATA%%）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # 子命令也接受 --data-root（SUPPRESS 保证不覆盖写在前面的那个）
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--data-root",
        default=argparse.SUPPRESS,
        help="插件数据根（默认系统临时目录/jml_plugin_sdk，不碰 %%APPDATA%%）",
    )

    p_check = sub.add_parser("check", parents=[common], help="只校验 plugin.json")
    p_check.add_argument("path", help="插件目录（或其中的 plugin.json）")
    p_check.set_defaults(func=cmd_check)

    p_run = sub.add_parser("run", parents=[common], help="完整装配并执行 setup")
    p_run.add_argument("path", help="插件目录，或放着多个插件的根目录（如 plugins）")
    p_run.set_defaults(func=cmd_run)

    p_page = sub.add_parser("page", parents=[common], help="调用页面加载钩子")
    p_page.add_argument("path", help="插件目录")
    p_page.add_argument("page_id", help="清单里声明过的页面 id")
    p_page.add_argument("--page", type=int, default=1, help="页码，默认 1")
    p_page.add_argument(
        "--param", action="append", default=[], metavar="KEY=VALUE",
        help="额外加载参数，可重复（如 --param folder=D:/comics）",
    )
    p_page.set_defaults(func=cmd_page)

    p_action = sub.add_parser("action", parents=[common], help="调用动作钩子")
    p_action.add_argument("path", help="插件目录")
    p_action.add_argument("page_id", help="页面 id")
    p_action.add_argument("action_id", help="动作 id（row_actions[].id）")
    p_action.add_argument("--row", action="append", default=[], metavar="KEY=VALUE",
                          help="行数据的一个键值，可重复")
    p_action.add_argument("--index", type=int, default=-1, help="行号，默认 -1")
    p_action.add_argument("--page", type=int, default=1, help="当前页码，默认 1")
    p_action.add_argument("--payload-json", default=None,
                          help="直接给完整 payload（会忽略 --row/--index/--page）")
    p_action.set_defaults(func=cmd_action)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_data_root(args.data_root)
    _register_host_services()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
