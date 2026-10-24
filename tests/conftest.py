"""pytest 公共夹具：把插件根当成一个包加载，避免相对导入失败。

契约 §9 的样例是加载 ``plugin.py``；本任务只测 ``provider`` / ``cache`` /
``text`` 三个纯逻辑模块，而 ``plugin.py`` 依赖 maibot_sdk（离线环境没有）。
所以这里只注册一个**合成包**（``__path__`` 指向插件根），再按需 import 子模块——
包名同样是 ``_maibot_plugin_*`` 那种「由插件 ID 生成的模块名」形态。
"""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG = "_tts_under_test"


def _install_package() -> None:
    if PKG in sys.modules:
        return
    package = types.ModuleType(PKG)
    package.__path__ = [str(ROOT)]  # type: ignore[attr-defined]
    package.__package__ = PKG
    sys.modules[PKG] = package


_install_package()


def load(name: str):
    """按包语义加载插件根下的模块（相对导入可用）。"""
    return importlib.import_module(f"{PKG}.{name}")


@pytest.fixture(scope="session")
def text_module():
    return load("text")


@pytest.fixture(scope="session")
def cache_module():
    return load("cache")


@pytest.fixture(scope="session")
def provider_module():
    return load("provider")


@pytest.fixture(scope="session")
def voice_module():
    return load("voice")


@pytest.fixture(scope="session")
def reply_ext_module():
    return load("reply_ext")


@pytest.fixture(scope="session")
def config_module():
    return load("config")


@pytest.fixture(scope="session")
def plugin_module():
    return load("plugin")
