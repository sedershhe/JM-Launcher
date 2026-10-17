"""分层方向回归测试（阶段 2 硬化 #11）。

- ``import jml.core.downloader`` 不得顺带拉起插件宿主（loader/config/manifest）；
- ``jml.host.events`` 是 ``jml.events`` 的兼容 re-export，reset 后 BUS 仍动态跟随。
"""

from __future__ import annotations

import os
import subprocess
import sys

import jml.events as core_events
from jml.host import events as compat_events


def _python_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_core_downloader_import_does_not_pull_host_modules():
    root = _python_root()
    code = (
        "import sys, jml.core.downloader, jml_backend; "
        "print('jml.host' in sys.modules, "
        "'jml.host.loader' in sys.modules, "
        "'jml.host.config' in sys.modules, "
        "'jml.host.manifest' in sys.modules, "
        "'PIL' in sys.modules)"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")

    output = subprocess.check_output(
        [sys.executable, "-c", code], cwd=root, env=env
    ).decode("utf-8").strip()

    assert output == "False False False False False"


def test_host_events_is_compat_reexport_and_tracks_reset():
    old_bus = compat_events.BUS
    assert compat_events.EventBus is core_events.EventBus

    compat_events.set_sink(lambda event_type, payload: None)
    assert core_events.BUS.sink() is not None
    compat_events.clear_sink()

    compat_events.reset()
    assert compat_events.BUS is core_events.BUS
    assert compat_events.BUS is not old_bus
