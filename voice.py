"""音色管理：克隆 / 列表 / 删除（契约 ``docs/CONTRACT.md`` §3.6 / §8.5–8.7，v1.2）。

为什么单开一个模块：合成是回复热路径（每次 reply 都跑），音色管理是低频运维
（一个月几次）。分开后 ``provider.py`` 的职责与测试面保持不变，本模块可以只喂
一个假 provider 就把「不许重试 / 撞名 / 拒删当前音色 / 登记落盘」全测完。

硬约束：

- **不 import httpx**：所有网络经 ``provider.request_json`` 出去，连接复用、
  Bearer 鉴权、错误归一化（401/403→auth…）全仓库只有一份实现；
- **不 import maibot_sdk**、不读 ``ctx``、不读配置：``voice_dir`` 由调用方传入；
- 任何可能进日志/异常的字符串都过 :func:`provider.redact_secret`（契约 §12）。
"""

from __future__ import annotations

import base64
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .provider import (
    VOICE_LIST_PATH,
    ProviderError,
    SiliconFlowTTSProvider,
    redact_secret,
)

__all__ = [
    "AUDIO_MIME",
    "AUDIO_SUFFIXES",
    "CLONE_VOICE_PATH",
    "MAX_AUDIO_BYTES",
    "VOICE_DELETE_PATH",
    "VOICE_NAME_PATTERN",
    "VoiceProfile",
    "clone_voice",
    "delete_voice",
    "list_voices",
    "read_reference_audio",
]

#: 克隆：上传参考音频（契约 §8.5）。
CLONE_VOICE_PATH = "/uploads/audio/voice"
#: 删除：删音色（契约 §8.7）。
VOICE_DELETE_PATH = "/audio/voice/deletions"

#: 允许的参考音频后缀（小写比较，覆盖大小写混写）。
AUDIO_SUFFIXES: tuple[str, ...] = (".mp3", ".wav", ".pcm", ".opus")
#: 后缀 → MIME，用于 ``data:{mime};base64,...`` 的 data URL。
AUDIO_MIME: dict[str, str] = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".pcm": "audio/L16",
    ".opus": "audio/ogg",
}
#: 参考音频大小上限；官方建议 8–10s、硬上限 <30s。
MAX_AUDIO_BYTES: int = 10 * 1024 * 1024
#: 单个音色名约束：避免空格/冒号污染 ``speech:name:xxx`` 形式的 uri。
VOICE_NAME_PATTERN: str = r"^[A-Za-z0-9_-]{1,64}$"

#: 超过这个体积记一条 WARNING（很可能超过 30s，属于文档约束而非硬校验）。
_WARN_AUDIO_BYTES = 2 * 1024 * 1024
_ONE_MB = 1024 * 1024

_VOICE_NAME_RE = re.compile(VOICE_NAME_PATTERN)
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class VoiceProfile:
    """一条音色记录（``uri`` 可直接填进配置的 ``voice`` 字段）。"""

    uri: str
    name: str
    model: str


# --------------------------------------------------------------------------- #
# 参考音频读取（本地校验，不发网络）
# --------------------------------------------------------------------------- #
def read_reference_audio(voice_dir: Path, filename: str) -> tuple[bytes, str]:
    """读取 ``<voice_dir>/<filename>``，返回 ``(bytes, mime)``。

    只接受 ``voices/`` 下的**单层文件名**；**不做路径拼接宽容**：

    - 空名、``.`` 开头（隐藏文件）、含 ``/`` ``\\`` ``..`` → ``ValueError``
    - 后缀不在 :data:`AUDIO_SUFFIXES` → ``ValueError``
    - 大小 > :data:`MAX_AUDIO_BYTES` 或为空文件 → ``ValueError``
    - 文件不存在 → ``FileNotFoundError``（调用方转成「请先把参考音频放进 voices/」）
    """
    name = (filename or "").strip()
    if not name:
        raise ValueError("参考音频文件名不能为空")
    if name.startswith("."):
        raise ValueError(f"不接受隐藏文件：{name!r}")
    if "/" in name or "\\" in name or ".." in name:
        raise ValueError(f"只接受 voices/ 下的单层文件名：{name!r}")

    suffix = Path(name).suffix.lower()
    if suffix not in AUDIO_SUFFIXES:
        raise ValueError(
            f"不支持的参考音频后缀：{suffix or '(无后缀)'}；"
            f"支持 {', '.join(AUDIO_SUFFIXES)}"
        )

    base = Path(voice_dir)
    path = base / name
    # 双保险：上面的字符校验已经排除了目录穿越，这里再确认一次父目录没被改写。
    if path.parent != base:
        raise ValueError(f"参考音频路径越界：{name!r}")
    return _read_audio_file(path)


def _read_audio_file(path: Path) -> tuple[bytes, str]:
    """读取并校验一个已定位的音频文件，返回 ``(bytes, mime)``。"""
    suffix = path.suffix.lower()
    if suffix not in AUDIO_SUFFIXES:
        raise ValueError(
            f"不支持的参考音频后缀：{suffix or '(无后缀)'}；"
            f"支持 {', '.join(AUDIO_SUFFIXES)}"
        )
    if not path.is_file():
        raise FileNotFoundError(f"参考音频不存在：{path}")

    size = path.stat().st_size
    if size == 0:
        raise ValueError(f"参考音频是空文件：{path.name}")
    if size > MAX_AUDIO_BYTES:
        raise ValueError(
            f"参考音频 {size / _ONE_MB:.1f}MB 超过上限 {MAX_AUDIO_BYTES / _ONE_MB:.0f}MB："
            f"{path.name}"
        )
    return path.read_bytes(), AUDIO_MIME[suffix]


# --------------------------------------------------------------------------- #
# 克隆
# --------------------------------------------------------------------------- #
async def clone_voice(
    provider: SiliconFlowTTSProvider,
    *,
    name: str,
    reference_text: str,
    audio_path: Path | None = None,
    audio_bytes: bytes | None = None,
    mime: str = "audio/mpeg",
    timeout: float | None = None,
) -> VoiceProfile:
    """上传参考音频，克隆一个音色，返回 :class:`VoiceProfile`。

    - ``name`` 必须匹配 :data:`VOICE_NAME_PATTERN`；``reference_text`` 必填非空
      （它是参考音频里说的那句话，直接影响克隆质量）。
    - ``audio_bytes`` 优先；否则读 ``audio_path``（此时 MIME 以后缀为准）。
    - 走 ``provider.request_json``，**不重试**（重复提交会在账号里留下同名音色）。
    """
    clean_name = (name or "").strip()
    if not _VOICE_NAME_RE.match(clean_name):
        raise ValueError(
            f"音色名不合法：{redact_secret(clean_name)!r}；"
            f"需匹配 {VOICE_NAME_PATTERN}（1–64 位字母/数字/下划线/连字符）"
        )

    text = (reference_text or "").strip()
    if not text:
        raise ValueError("参考文本（reference_text）必填：请填写参考音频里说的那句话")

    if audio_bytes is not None:
        data = bytes(audio_bytes)
        if not data:
            raise ValueError("参考音频为空")
        if len(data) > MAX_AUDIO_BYTES:
            raise ValueError(
                f"参考音频 {len(data) / _ONE_MB:.1f}MB 超过上限 "
                f"{MAX_AUDIO_BYTES / _ONE_MB:.0f}MB"
            )
        content_type = mime or "audio/mpeg"
    elif audio_path is not None:
        data, content_type = _read_audio_file(Path(audio_path))
    else:
        raise ValueError("需要 audio_bytes 或 audio_path 之一")

    if len(data) > _WARN_AUDIO_BYTES:
        _warn(
            "参考音频 %.1fMB，可能超过 30s 上限（官方建议 8–10s、单人、无噪）",
            len(data) / _ONE_MB,
        )

    encoded = base64.b64encode(data).decode("ascii")
    payload: dict[str, object] = {
        "model": provider.settings.model,
        "customName": clean_name,
        "text": text,
        "audio": f"data:{content_type};base64,{encoded}",
    }
    result = await provider.request_json(
        "POST", CLONE_VOICE_PATH, payload=payload, timeout=timeout
    )

    uri = _extract_uri(result)
    if not uri:
        raise ProviderError(
            redact_secret(
                f"克隆响应缺少 uri 字段：{_short_repr(result)}"
            ),
            kind="bad_request",
        )
    return VoiceProfile(uri=uri, name=clean_name, model=provider.settings.model)


# --------------------------------------------------------------------------- #
# 列表
# --------------------------------------------------------------------------- #
async def list_voices(
    provider: SiliconFlowTTSProvider,
    *,
    timeout: float | None = None,
) -> list[VoiceProfile]:
    """列出账号下已有音色，按 ``name`` 升序（大小写不敏感，展示用）。

    解析容错：裸 ``list``、``{"results": [...]}``、``{"data": [...]}``、
    ``{"voices": [...]}`` 都接受；单项缺 ``uri`` 跳过；结构无法识别（含 2xx 但
    响应体不是 JSON）→ 返回 ``[]`` 并记 WARNING —— 命令展示不该因解析失败而崩。
    """
    try:
        payload = await provider.request_json(
            "GET", VOICE_LIST_PATH, timeout=timeout
        )
    except ProviderError as exc:
        # 2xx 却解析不出 JSON：按「结构无法识别」降级，其余错误原样上抛。
        if exc.status is not None and 200 <= exc.status < 300:
            _warn("音色列表响应无法解析，按空列表处理：%s", exc)
            return []
        raise

    items = _extract_items(payload)
    if items is None:
        _warn(
            "音色列表结构无法识别（%s），按空列表处理",
            type(payload).__name__,
        )
        return []

    profiles: list[VoiceProfile] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        uri = item.get("uri")
        if not isinstance(uri, str) or not uri.strip():
            continue  # 契约 §3.6：单项缺 uri 跳过
        raw_name = item.get("customName")
        if not isinstance(raw_name, str) or not raw_name:
            raw_name = item.get("name")
        raw_model = item.get("model")
        profiles.append(
            VoiceProfile(
                uri=uri.strip(),
                name=str(raw_name) if isinstance(raw_name, str) else "",
                model=str(raw_model) if isinstance(raw_model, str) else "",
            )
        )

    profiles.sort(key=lambda profile: (profile.name.lower(), profile.uri))
    return profiles


# --------------------------------------------------------------------------- #
# 删除
# --------------------------------------------------------------------------- #
async def delete_voice(
    provider: SiliconFlowTTSProvider,
    *,
    uri: str,
    timeout: float | None = None,
) -> bool:
    """删除一个音色：2xx → ``True``；404 → ``False``（已经不存在，幂等）。

    其余错误按契约 §8.2 归一化后抛 ``ProviderError``。
    """
    target = (uri or "").strip()
    if not target:
        raise ValueError("删除音色需要 uri")
    try:
        await provider.request_json(
            "POST", VOICE_DELETE_PATH, payload={"uri": target}, timeout=timeout
        )
    except ProviderError as exc:
        if exc.status == 404:
            return False
        raise
    return True


# --------------------------------------------------------------------------- #
# 内部
# --------------------------------------------------------------------------- #
def _extract_items(payload: object) -> list[object] | None:
    """从各种可能的容器形状里取出音色条目列表；认不出返回 ``None``。"""
    if isinstance(payload, (list, tuple)):
        return list(payload)
    if isinstance(payload, Mapping):
        for field in ("results", "data", "voices", "list", "uris"):
            if field in payload:
                value = payload[field]
                if isinstance(value, (list, tuple)):
                    return list(value)
                return None
        return None
    return None


def _extract_uri(result: object) -> str | None:
    if isinstance(result, Mapping):
        uri = result.get("uri")
        if isinstance(uri, str) and uri.strip():
            return uri.strip()
    return None


def _short_repr(value: object) -> str:
    text = repr(value)
    return text if len(text) <= 200 else f"{text[:200]}…"


def _warn(message: str, *args: object) -> None:
    """日志入口：消息与字符串参数都过 ``redact_secret``（契约 §12 R3）。"""
    safe_args = tuple(
        redact_secret(arg) if isinstance(arg, str) else arg for arg in args
    )
    _LOGGER.warning(redact_secret(message), *safe_args)
