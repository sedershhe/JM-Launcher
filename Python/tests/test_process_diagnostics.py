"""Python 进程诊断（``write_process_diagnostics``）测试。

覆盖：
- 诊断正文包含 ``sys.executable`` / ``sys.version`` / pid / cwd 与目录类环境变量；
- 对配置根、系统临时根、C# 指定的临时根 / 下载目录各做一次实测写探针；
- 不可写目录记录异常类型 + errno/winerror + 完整路径；
- 同一份正文追加到 runtime-data-root.txt 与 startup-diagnostics.log；
- 写盘失败不抛异常，只把原因附在返回文本里。

全部离线；APPDATA 由 conftest 指向临时目录，真实用户目录不被读写。
"""

from __future__ import annotations

import os
import re

import jml_backend
from jml.host import paths


def test_write_process_diagnostics_appends_env_and_probes(tmp_path):
    diagnostic_path = tmp_path / "runtime-data-root.txt"
    log_path = tmp_path / "logs" / "startup-diagnostics.log"
    download_dir = tmp_path / "downloads"
    download_dir.mkdir()

    text = jml_backend.write_process_diagnostics(
        str(diagnostic_path),
        str(log_path),
        str(download_dir),
        str(tmp_path / "work-tmp"),
    )

    # 进程信息
    assert "sys.executable：" in text
    assert "sys.version：" in text
    assert "os.getpid()：" in text
    assert "os.getcwd()：" in text
    # 目录类环境变量
    assert "APPDATA=" in text
    assert "LOCALAPPDATA=" in text
    assert "TEMP=" in text
    assert "TMP=" in text
    assert "USERPROFILE=" in text
    # 探针覆盖：配置根 / 系统临时根 / 下载临时根 / 下载目录
    assert "[配置根（jml.host.paths.config_root）]" in text
    assert "[系统临时根（tempfile.gettempdir）]" in text
    assert "[下载临时根（C# AppPaths.TempDirectory）]" in text
    assert "[下载目录（C# 已保存的下载目录）]" in text
    assert "结果：可写" in text
    assert "探针文件：.jml-write-probe-" in text
    assert "完整路径：" in text

    # 同一份正文追加到两个由调用方传入的绝对路径
    assert diagnostic_path.is_file()
    assert text in diagnostic_path.read_text(encoding="utf-8")
    assert log_path.is_file()
    assert text in log_path.read_text(encoding="utf-8")


def test_write_process_diagnostics_skips_download_probe_when_unspecified(tmp_path):
    text = jml_backend.write_process_diagnostics(str(tmp_path / "runtime-data-root.txt"))

    assert "[下载目录] 未指定" in text
    assert "[下载目录（C# 已保存的下载目录）]" not in text


def test_write_process_diagnostics_records_errno_and_path_for_unwritable_dir(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    unwritable = blocker / "child"

    text = jml_backend.write_process_diagnostics(
        str(tmp_path / "runtime-data-root.txt"),
        "",
        "",
        str(unwritable),
    )

    line = next(
        item for item in text.splitlines() if "[下载临时根（C# AppPaths.TempDirectory）]" in item
    )
    assert "结果：不可写" in line
    assert "失败阶段：" in line
    assert "异常：" in line
    assert re.search(r"errno：\d+", line)
    assert "winerror：" in line
    assert os.path.abspath(str(unwritable)) in line


def test_write_process_diagnostics_skips_missing_download_dir_without_creating_it(tmp_path):
    missing = tmp_path / "not-created-yet"

    text = jml_backend.write_process_diagnostics(
        str(tmp_path / "runtime-data-root.txt"),
        "",
        str(missing),
        "",
    )

    assert "不存在或不是目录，跳过写探针" in text
    assert not missing.exists()


def test_write_process_diagnostics_never_raises_on_unwritable_diagnostic_path(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")

    # 诊断文件父路径是文件：追加失败只能体现在文本里，不能抛给 C#
    text = jml_backend.write_process_diagnostics(str(blocker / "runtime-data-root.txt"))

    assert "sys.executable：" in text
    assert "runtime-data-root.txt 追加失败" in text


def test_probe_writable_detailed_reports_success(tmp_path):
    detail = paths.probe_writable_detailed(tmp_path / "ok")

    assert detail["writable"] is True
    assert detail["directory"] == os.path.abspath(str(tmp_path / "ok"))
    assert detail["probe_file"].startswith(".jml-write-probe-")
    assert detail["probe_file"].endswith(".tmp")
    assert detail["probe_path"] == os.path.join(
        detail["directory"], detail["probe_file"]
    )
    assert not os.path.exists(detail["probe_path"])
    assert detail["stage"] == ""
    assert detail["error_type"] == ""


def test_probe_writable_detailed_reports_failure_details(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")

    detail = paths.probe_writable_detailed(blocker / "child")

    assert detail["writable"] is False
    assert detail["stage"] in {"创建目录", "写探针文件", "删除探针文件"}
    assert detail["error_type"]
    assert detail["errno"] is not None
    assert detail["probe_path"].startswith(
        os.path.abspath(str(blocker / "child"))
    )
    # 失败也必须尽力清理探针文件（与旧实现一致）
    assert not os.path.exists(detail["probe_path"])
