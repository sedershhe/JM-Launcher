"""连通性测试插件的离线单元测试（方案 §10.1）。

覆盖三块：
- ``probe.redirect_probe``：手动跟随 301/302，超时与跳数上限收口（方案 §5.2）；
- ``probe.probe_domain`` / ``probe.classify``：单域判定表（方案 §4.2）；
- ``probe.proxies_from_jmcomic``：只读 jmcomic 的默认代理（方案 §4.1）；
- ``main.ConnectivityPage``：空候选页面与候选组装防回归（方案 §2.6 / §6.2）。

全部用例注入假 ``fetch``，不联网、不写磁盘、不依赖执行顺序。
插件目录只有放在 ``sys.path`` 上时 ``main.py`` 里的 ``import probe`` 才能命中，
所以加载函数临时插入再移除，避免污染全局路径。
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[2] / "plugins" / "connectivity_test"


def _load_main():
    """按方案 §10.1 的片段加载 ``probe`` 与 ``main``，返回两个模块。"""
    sys.path.insert(0, str(PLUGIN_DIR))
    try:
        import probe

        spec = importlib.util.spec_from_file_location(
            "connectivity_plugin_main", PLUGIN_DIR / "main.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return probe, module
    finally:
        sys.path.remove(str(PLUGIN_DIR))


@pytest.fixture(scope="module")
def loaded():
    """模块级只加载一次，避免每个用例重复 exec 插件代码。"""
    return _load_main()


@pytest.fixture()
def probe(loaded):
    return loaded[0]


@pytest.fixture()
def main(loaded):
    return loaded[1]


# ---------------------------------------------------------------------------
# 假对象：假响应 + 按队列驱动的假 fetch
# ---------------------------------------------------------------------------


class FakeResponse:
    """最小响应对象：``probe`` 只读 status_code / headers / text / url。"""

    def __init__(self, status, headers=None, text="", url=""):
        self.status_code = status
        self.headers = headers or {}
        self.text = text
        self.url = url


class FakeFetch:
    """按顺序吐出预置响应的假 fetch，并记录每次调用的参数。

    ``plan`` 的元素可以是 ``(status, headers, text)`` 元组，也可以是异常实例
    （用于模拟超时）。响应对象的 ``url`` 固定为本次请求的 url，模拟 curl 的行为。
    """

    def __init__(self, plan):
        self._plan = list(plan)
        self.calls = []

    def __call__(self, url, timeout=8.0, referer=""):
        self.calls.append({"url": url, "timeout": timeout, "referer": referer})
        assert self._plan, f"假 fetch 的预置响应不够用：多请求了 {url}"
        item = self._plan.pop(0)
        if isinstance(item, BaseException):
            raise item
        status, headers, text = item
        return FakeResponse(status, headers, text, url=url)


def _assert_jsonable(payload):
    """方案 §5.3：probe 的返回值必须能直接 json.dumps。"""
    assert isinstance(payload, dict)
    dumped = json.dumps(payload)
    assert isinstance(dumped, str) and dumped


class FakeCtx:
    """够用的插件上下文：只需要 ``log``（页面对象在 load 里写汇总日志）。"""

    def __init__(self):
        self.logs = []

    def log(self, message):
        self.logs.append(str(message))


# ---------------------------------------------------------------------------
# redirect_probe：重定向链（方案 §5.2 / §10.1 前 10 条）
# ---------------------------------------------------------------------------

ENTRY = "https://jm365.work/3YeBdF"


def test_redirect_301_then_302_stops_at_302(probe):
    """方案 §10.1：301 → 302，stop_on_302=True 时在 302 收口（永久网域入口）。"""
    fetch = FakeFetch(
        [
            (301, {"Location": "https://mid.example/hop"}, ""),
            (302, {"Location": "https://target.example/final"}, ""),
        ]
    )

    result = probe.redirect_probe(ENTRY, fetch, stop_on_302=True)

    assert result["verdict"] == "ok"
    assert result["http_status"] == 302
    assert len(result["hop_chain"]) == 2
    assert result["final_url"] == "https://target.example/final"
    assert result["error"] == ""
    _assert_jsonable(result)


def test_redirect_301_then_200_follows_to_200(probe):
    """方案 §10.1：301 → 200，stop_on_302=False 时跟到 200，final_url 是 200 的地址。"""
    fetch = FakeFetch(
        [
            (301, {"Location": "https://pub.example/landing"}, ""),
            (200, {}, "<html>ok</html>"),
        ]
    )

    result = probe.redirect_probe(ENTRY, fetch, stop_on_302=False)

    assert result["verdict"] == "ok"
    assert result["http_status"] == 200
    assert len(result["hop_chain"]) == 2
    assert result["final_url"] == "https://pub.example/landing"
    _assert_jsonable(result)


def test_redirect_302_not_stopped_continues_to_200(probe):
    """方案 §10.1：302 且 stop_on_302=False 时不就地收口，继续跟到 200。"""
    fetch = FakeFetch(
        [
            (302, {"Location": "https://next.example/"}, ""),
            (200, {}, "clean"),
        ]
    )

    result = probe.redirect_probe(ENTRY, fetch, stop_on_302=False)

    assert result["verdict"] == "ok"
    assert [hop["status"] for hop in result["hop_chain"]] == [302, 200]
    assert result["final_url"] == "https://next.example/"
    assert len(fetch.calls) == 2


def test_redirect_single_302(probe):
    """方案 §10.1：单跳 302，stop_on_302=True 直接判可用，只请求一次。"""
    fetch = FakeFetch([(302, {"Location": "https://site.example/"}, "")])

    result = probe.redirect_probe(ENTRY, fetch, stop_on_302=True)

    assert result["verdict"] == "ok"
    assert result["http_status"] == 302
    assert len(result["hop_chain"]) == 1
    assert len(fetch.calls) == 1


def test_redirect_400_is_restricted(probe):
    """方案 §10.1：400 走判定表，verdict=restricted 且 error/detail 非空。"""
    fetch = FakeFetch([(400, {}, "bad request")])

    result = probe.redirect_probe(ENTRY, fetch, stop_on_302=True)

    assert result["verdict"] == "restricted"
    assert result["http_status"] == 400
    assert result["error"]
    assert result["detail"]
    _assert_jsonable(result)


def test_redirect_timeout_is_fail(probe):
    """方案 §10.1：fetch 抛 TimeoutError 时收口成 fail，http_status=0，错误含异常类名。"""
    fetch = FakeFetch([TimeoutError("read timeout")])

    result = probe.redirect_probe(ENTRY, fetch, stop_on_302=True)

    assert result["verdict"] == "fail"
    assert result["http_status"] == 0
    assert "TimeoutError" in result["error"]
    assert result["hop_chain"] == []
    _assert_jsonable(result)


def test_redirect_200_with_region_mark_is_restricted(probe):
    """方案 §4.2：200 页面含 ``Restricted Access!`` 时判地区受限，不能算可用。"""
    fetch = FakeFetch([(200, {}, "<h1>Restricted Access!</h1>")])

    result = probe.redirect_probe(ENTRY, fetch, stop_on_302=True)

    assert result["verdict"] == "restricted"
    assert result["detail"] == "地区受限"
    assert result["error"] == "地区受限"


def test_redirect_more_than_four_hops_fails(probe):
    """方案 §5.2：连续 301 超过 4 跳仍未收口，判 fail 并说明超过跳数上限。"""
    plan = [
        (301, {"Location": f"https://hop{i}.example/next"}, "") for i in range(1, 6)
    ]
    fetch = FakeFetch(plan)

    result = probe.redirect_probe(ENTRY, fetch, max_hops=4, stop_on_302=True)

    assert result["verdict"] == "fail"
    assert "超过 4 跳" in result["error"]
    assert len(result["hop_chain"]) == 4
    _assert_jsonable(result)


def test_redirect_relative_location_is_joined(probe):
    """方案 §5.2：相对 ``Location`` 用 urljoin 补成绝对地址。"""
    fetch = FakeFetch([(302, {"Location": "/next"}, "")])

    result = probe.redirect_probe(ENTRY, fetch, stop_on_302=True)

    assert result["verdict"] == "ok"
    assert result["final_url"] == "https://jm365.work/next"


def test_redirect_hop_timeout_and_referer(probe):
    """方案 §5.2：每一跳都用 timeout=3.0，Referer 取入口 host 的根地址。"""
    fetch = FakeFetch(
        [
            (301, {"Location": "https://mid.example/hop"}, ""),
            (302, {"Location": "https://target.example/final"}, ""),
        ]
    )

    probe.redirect_probe(ENTRY, fetch, max_hops=4, hop_timeout=3.0, stop_on_302=True)

    assert len(fetch.calls) == 2
    for call in fetch.calls:
        assert call["timeout"] == 3.0
        assert call["referer"] == "https://jm365.work/"
    # 第二跳请求的是上一跳的 Location
    assert fetch.calls[1]["url"] == "https://mid.example/hop"


# ---------------------------------------------------------------------------
# probe_domain：单域判定（方案 §4.2 / §10.1）
# ---------------------------------------------------------------------------


def test_probe_domain_ok_has_empty_hop_chain(probe):
    """方案 §10.1：200 干净页面判 ok，单跳探测的 hop_chain 留空数组。"""
    fetch = FakeFetch([(200, {}, "<html>ok</html>")])

    result = probe.probe_domain("example.com", fetch)

    assert result["verdict"] == "ok"
    assert result["http_status"] == 200
    assert result["hop_chain"] == []
    assert result["error"] == ""
    assert result["final_url"] == "https://example.com/"
    _assert_jsonable(result)


@pytest.mark.parametrize(
    ("status", "detail"),
    [(403, "访问被拒"), (429, "请求过多"), (503, "服务端异常")],
)
def test_probe_domain_status_table(probe, status, detail):
    """方案 §4.2 判定表：403/429/5xx 都算受限，但细分文案不同。"""
    fetch = FakeFetch([(status, {}, "")])

    result = probe.probe_domain("example.com", fetch)

    assert result["verdict"] == "restricted"
    assert result["detail"] == detail
    assert result["http_status"] == status
    _assert_jsonable(result)


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        ("Could not connect to mysql", "服务端异常"),
        ("<title>Just a moment...</title>", "人机验证"),
    ],
)
def test_probe_domain_body_marks(probe, body, detail):
    """方案 §4.2：200 里出现数据库错误 / Cloudflare 挑战页特征时判受限。"""
    fetch = FakeFetch([(200, {}, body)])

    result = probe.probe_domain("example.com", fetch)

    assert result["verdict"] == "restricted"
    assert result["detail"] == detail


def test_probe_domain_request_arguments(probe):
    """方案 §4.1：只请求 ``https://{domain}/``，timeout=8.0，Referer 同域。"""
    fetch = FakeFetch([(200, {}, "ok")])

    probe.probe_domain("Example.COM", fetch, timeout=8.0)

    assert len(fetch.calls) == 1
    assert fetch.calls[0]["url"] == "https://Example.COM/"
    assert fetch.calls[0]["timeout"] == 8.0
    assert fetch.calls[0]["referer"] == "https://Example.COM/"


# ---------------------------------------------------------------------------
# proxies_from_jmcomic：只读 jmcomic 默认代理（方案 §4.1）
# ---------------------------------------------------------------------------


def _fake_jmcomic(monkeypatch, proxies):
    """把一个假的 jmcomic 模块塞进 sys.modules，只带 JmModuleConfig.DEFAULT_PROXIES。"""
    module = types.ModuleType("jmcomic")
    module.JmModuleConfig = types.SimpleNamespace(DEFAULT_PROXIES=proxies)
    monkeypatch.setitem(sys.modules, "jmcomic", module)
    return module


def test_proxies_jmcomic_missing_returns_none(probe, monkeypatch):
    """方案 §10.1：``import jmcomic`` 失败时返回 None（直连）。

    ``sys.modules["jmcomic"] = None`` 会让 import 直接抛 ImportError，
    不依赖本机是否真的装了 jmcomic。
    """
    monkeypatch.setitem(sys.modules, "jmcomic", None)

    assert probe.proxies_from_jmcomic() is None


@pytest.mark.parametrize("raw", [{}, None])
def test_proxies_empty_returns_none(probe, monkeypatch, raw):
    """方案 §10.1：``DEFAULT_PROXIES`` 为 {} 或 None 时返回 None（系统没配代理）。"""
    _fake_jmcomic(monkeypatch, raw)

    assert probe.proxies_from_jmcomic() is None


def test_proxies_dict_values_get_scheme(probe, monkeypatch):
    """方案 §10.1：dict 代理原样返回，值里的 host:port 补成 ``http://``。"""
    _fake_jmcomic(monkeypatch, {"http": "127.0.0.1:7890", "https": "127.0.0.1:7890"})

    assert probe.proxies_from_jmcomic() == {
        "http": "http://127.0.0.1:7890",
        "https": "http://127.0.0.1:7890",
    }


def test_proxies_string_becomes_two_keys(probe, monkeypatch):
    """方案 §10.1：字符串代理展开成 http/https 两个键。"""
    _fake_jmcomic(monkeypatch, "127.0.0.1:7890")

    assert probe.proxies_from_jmcomic() == {
        "http": "http://127.0.0.1:7890",
        "https": "http://127.0.0.1:7890",
    }


# ---------------------------------------------------------------------------
# main.ConnectivityPage：空候选与候选组装（方案 §6.2 / §2.6）
# ---------------------------------------------------------------------------


def test_page_payload_with_empty_candidates(main, monkeypatch):
    """方案 §10.1：注入空候选时 rows=[]、total=0、total_pages=1，empty_text 生效。"""
    monkeypatch.setattr(main.ConnectivityPage, "_candidates", lambda self: [])
    page = main.ConnectivityPage(FakeCtx())

    rows = page._run_round()
    assert rows == []

    page._rows = rows
    payload = page._page_payload(1)

    assert payload["rows"] == []
    assert payload["total"] == 0
    assert payload["total_pages"] == 1
    assert payload["page"] == 1
    assert payload["empty_text"] == "没有可测试的网页域名。"
    _assert_jsonable(payload)


def test_candidates_assembly(main, monkeypatch):
    """方案 §2.6 / §14.3：候选最多 8 项、含两个入口、域名不重复。

    ``_jmcomic_value`` 固定返回 None，走插件内置常量，避免受本机 jmcomic 安装状态影响。
    """
    monkeypatch.setattr(main, "_jmcomic_value", lambda name: None)
    page = main.ConnectivityPage(FakeCtx())

    items = page._candidates()

    assert len(items) <= main.MAX_CANDIDATES == 8
    roles = [item["role"] for item in items]
    assert "redirect_entry" in roles
    assert "publish_entry" in roles

    domains = [item["domain"].lower() for item in items]
    assert len(domains) == len(set(domains)), f"候选域名重复：{domains}"
    for item in items:
        assert item["domain"]
        assert item["endpoint"].startswith("https://")
