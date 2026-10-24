"""音频格式决策：白名单、采样率回落、Base64 编码与转码闸门。

设计要点（CONTRACT §3.5）：

- 默认 ``mp3`` 直出：``ensure_compatible`` 只做兜底校验并原样返回字节。
- ``wav`` / ``silk`` 转码属于**预留分支**：容器内 ``ffmpeg`` / ``pysilk`` 的可用性
  由 verifier 实测判定，本模块只在调用时才探测，绝不硬依赖。

本模块不导入 ``maibot_sdk``，pytest 可直接导入。
"""

from __future__ import annotations

import base64
import importlib.util
from logging import getLogger
from shutil import which

logger = getLogger(__name__)

#: 硅基流动支持且本插件接受的输出格式（CONTRACT §8.3）
SUPPORTED_FORMATS: tuple[str, ...] = ("mp3", "wav", "opus")

#: 每种格式的合法采样率（CONTRACT §8.3）
SAMPLE_RATES: dict[str, tuple[int, ...]] = {
    "mp3": (32000, 44100),
    "wav": (8000, 16000, 24000, 32000, 44100),
    "opus": (48000,),
}

#: 非法采样率回落值（mp3/wav 44100，opus 48000）
DEFAULT_SAMPLE_RATES: dict[str, int] = {"mp3": 44100, "wav": 44100, "opus": 48000}

#: 格式别名（只保留官方常见写法，其余一律拒绝）
_FORMAT_ALIASES: dict[str, str] = {"mpeg": "mp3"}

#: 预留的转码目标（当前未启用）
TRANSCODE_TARGETS: tuple[str, ...] = ("silk",)


def normalize_format(fmt: str) -> str:
    """把小写化 + 别名归一化后的格式名返回，未知格式抛 ``ValueError``。"""

    normalized = str(fmt or "").strip().lower()
    normalized = _FORMAT_ALIASES.get(normalized, normalized)
    if normalized not in SUPPORTED_FORMATS:
        raise ValueError(f"不支持的音频格式: {fmt!r}；可选 {', '.join(SUPPORTED_FORMATS)}")
    return normalized


def resolve_sample_rate(fmt: str, sample_rate: int) -> int:
    """按格式校验采样率；非法值回落默认值并记 WARNING，不阻断合成。"""

    normalized = normalize_format(fmt)
    allowed = SAMPLE_RATES[normalized]
    try:
        candidate = int(sample_rate)
    except (TypeError, ValueError):
        candidate = -1
    if candidate in allowed:
        return candidate
    fallback = DEFAULT_SAMPLE_RATES[normalized]
    logger.warning(
        "采样率 %r 不适用于 %s（合法值 %s），已回落到 %s",
        sample_rate,
        normalized,
        allowed,
        fallback,
    )
    return fallback


def ensure_compatible(data: bytes, fmt: str) -> bytes:
    """兜底闸门：校验非空与格式白名单后原样返回音频字节。

    未来接入 silk/ffmpeg 时**只改本函数**，签名保持不变：

    .. code-block:: python

        if normalize_format(fmt) in TRANSCODE_TARGETS:
            return transcode_to_silk(payload)
    """

    if data is None or data == b"":
        raise ValueError("empty audio")
    if not isinstance(data, (bytes, bytearray)):
        raise ValueError("empty audio")
    payload = bytes(data)
    if not payload:
        raise ValueError("empty audio")
    normalize_format(fmt)
    return payload


def to_base64(data: bytes) -> str:
    """把音频字节编码成 Host ``voice`` 段要求的 ``binary_data_base64``。"""

    return base64.b64encode(bytes(data)).decode("ascii")


def _module_available(module_name: str) -> bool:
    """检测可选模块是否已安装（只探测，不导入，避免副作用）。"""

    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ValueError):
        return False


def probe_transcoders() -> dict[str, bool]:
    """探测容器内可选转码能力，仅用于诊断日志；默认链路不依赖它们。"""

    return {
        "ffmpeg": which("ffmpeg") is not None,
        "pysilk": _module_available("pysilk") or _module_available("pysilk_async"),
    }


def transcode_to_silk(data: bytes, *, sample_rate: int = 24000) -> bytes:
    """预留的 silk 转码入口。

    只有在真实环境确认「QQ 必须收 silk 才能播放」时，才在此处接
    ``ffmpeg`` + ``pysilk`` 实现；当前 mp3 直出，无需调用。
    """

    raise NotImplementedError("silk 转码尚未实现；默认 mp3 直出，无需调用本函数")
