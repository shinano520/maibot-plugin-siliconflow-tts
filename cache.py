"""音频缓存：按内容寻址落盘，带 TTL 过期与容量 LRU 淘汰。

公开签名（冻结，见 ``docs/CONTRACT.md`` §3.3 / §7）：

- 模块级 ``make_key(*, text, model, voice, speed, gain, response_format, sample_rate) -> str``
- ``AudioCache(root, *, ttl_hours=72, max_mb=200, enabled=True)``
- ``AudioCache.make_key(**kwargs)`` / ``.get(key)`` / ``.put(key, data)`` /
  ``.clear()`` / ``.stats()``

文件布局::

    <root>/audio/<key[:2]>/<key>.<response_format>

全部 IO 为**同步**实现，调用方用 ``asyncio.to_thread`` 包（契约 §5.2）。
不依赖 maibot_sdk。
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from pathlib import Path
from typing import Iterator

__all__ = [
    "DEFAULT_FORMAT",
    "MAX_FILE_BYTES",
    "AudioCache",
    "make_key",
]

#: 兜底扩展名（调用方没传 ``response_format`` 时用它命名文件）。
DEFAULT_FORMAT = "mp3"
#: 单文件上限，超过不落盘（契约 §7：``min(20MB, max_mb)``）。
MAX_FILE_BYTES = 20 * 1024 * 1024

_ONE_MB = 1024 * 1024
_TMP_SUFFIX = ".tmp"


def make_key(
    *,
    text: str,
    model: str,
    voice: str,
    speed: float,
    gain: float,
    response_format: str,
    sample_rate: int,
) -> str:
    """内容键：七个字段全参与（模型/音色/语速/增益/格式/采样率/文本）。

    ``text`` 必须是 ``text.prepare()`` 之后的文本，保证「同一句话、不同原始
    Markdown」命中同一个缓存。
    """
    raw = "|".join(
        (
            str(model),
            str(voice),
            f"{float(speed):.3f}",
            f"{float(gain):.3f}",
            str(response_format),
            str(int(sample_rate)),
            text or "",
        )
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class AudioCache:
    """磁盘音频缓存。

    - ``get``：命中返回值；文件不存在 / 过期 / 损坏 / ``enabled=False`` 一律 ``None``。
    - ``put``：``<key>.<fmt>.tmp`` 写完再 ``os.replace`` 原子替换；写后触发 LRU 淘汰。
    - ``ttl_hours=0`` 表示永不过期；``max_mb=0`` 表示不限容量。
    - 过期与 LRU 都以文件 ``mtime``（写入时间）为准，读命中不刷新时间戳，
      因此行为完全可复现、便于测试。
    """

    def __init__(
        self,
        root: Path | str,
        *,
        ttl_hours: int = 72,
        max_mb: float = 200,
        enabled: bool = True,
        response_format: str = DEFAULT_FORMAT,
        logger: object | None = None,
    ) -> None:
        self.root = Path(root)
        self.ttl_hours = int(ttl_hours)
        self.max_mb = max_mb
        self.enabled = bool(enabled)
        self.response_format = _clean_format(response_format) or DEFAULT_FORMAT
        self.logger = logger
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # 路径
    # ------------------------------------------------------------------ #
    @property
    def audio_dir(self) -> Path:
        """``<root>/audio``。"""
        return self.root / "audio"

    @property
    def max_bytes(self) -> int:
        """容量上限字节数；``0`` 表示不限制。"""
        if not self.max_mb or float(self.max_mb) <= 0:
            return 0
        return int(float(self.max_mb) * _ONE_MB)

    def path_for(self, key: str, response_format: str | None = None) -> Path:
        """``<root>/audio/<key[:2]>/<key>.<fmt>``。"""
        fmt = _clean_format(response_format) or self.response_format
        return self.audio_dir / key[:2] / f"{key}.{fmt}"

    # ------------------------------------------------------------------ #
    # 读 / 写
    # ------------------------------------------------------------------ #
    def make_key(self, **kwargs: object) -> str:
        """委托模块级 :func:`make_key`，保证全插件只有一份实现。"""
        return make_key(**kwargs)  # type: ignore[arg-type]

    def get(self, key: str) -> bytes | None:
        """命中返回音频字节；未命中 / 过期 / 损坏 / 关闭缓存返回 ``None``。"""
        if not self.enabled or not key:
            return None
        path = self._find(key)
        if path is None:
            return None
        if self._is_expired(path):
            self._unlink(path)
            return None
        try:
            data = path.read_bytes()
        except OSError:
            return None
        if not data:
            self._unlink(path)
            return None
        return data

    def put(
        self,
        key: str,
        data: bytes,
        *,
        response_format: str | None = None,
    ) -> None:
        """原子写入并触发容量淘汰。

        ``response_format`` 只决定扩展名（便于排查）；读取端按前缀匹配，
        扩展名不参与命中判断，所以老缓存不会因为换格式而失联。
        """
        if not self.enabled or not key or not data:
            return
        limit = MAX_FILE_BYTES
        if self.max_bytes:
            limit = min(limit, self.max_bytes)
        if len(data) > limit:
            # 契约 §7：单文件超限不落盘，只记一条 WARNING（有 logger 才记）。
            self._warn(
                "音频缓存跳过写入：单条 %.1fKB 超过上限 %.1fKB",
                len(data) / 1024,
                limit / 1024,
            )
            return

        path = self.path_for(key, response_format or _sniff_format(data))
        tmp = path.with_name(path.name + _TMP_SUFFIX)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except OSError:
            self._unlink(tmp)
            return

        self._evict()

    # ------------------------------------------------------------------ #
    # 维护
    # ------------------------------------------------------------------ #
    def clear(self) -> int:
        """删除全部缓存文件（含残留 ``.tmp``），返回删除数量。"""
        removed = 0
        with self._lock:
            for path in list(self._iter_files(include_tmp=True)):
                if self._unlink(path):
                    removed += 1
            base = self.audio_dir
            if base.is_dir():
                for shard in base.iterdir():
                    if shard.is_dir():
                        try:
                            shard.rmdir()
                        except OSError:
                            pass
        return removed

    def purge_expired(self) -> int:
        """扫一遍全库删掉过期文件，返回删除数量（TTL 为 0 时直接返回 0）。"""
        if self.ttl_hours <= 0:
            return 0
        removed = 0
        with self._lock:
            for path in list(self._iter_files()):
                if self._is_expired(path) and self._unlink(path):
                    removed += 1
        return removed

    def stats(self) -> dict[str, int | str]:
        """``{"count": 文件数, "bytes": 总字节, "root": 根目录}``。"""
        count = 0
        total = 0
        for path in self._iter_files():
            try:
                total += path.stat().st_size
            except OSError:
                continue
            count += 1
        return {"count": count, "bytes": total, "root": str(self.root)}

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _iter_files(self, *, include_tmp: bool = False) -> Iterator[Path]:
        base = self.audio_dir
        if not base.is_dir():
            return
        for shard in sorted(base.iterdir()):
            if not shard.is_dir():
                continue
            for path in sorted(shard.iterdir()):
                if not path.is_file():
                    continue
                if not include_tmp and path.name.endswith(_TMP_SUFFIX):
                    continue
                yield path

    def _find(self, key: str) -> Path | None:
        shard = self.audio_dir / key[:2]
        if not shard.is_dir():
            return None
        for path in shard.iterdir():
            if (
                path.is_file()
                and path.name.startswith(key)
                and not path.name.endswith(_TMP_SUFFIX)
            ):
                return path
        return None

    def _is_expired(self, path: Path) -> bool:
        if self.ttl_hours <= 0:
            return False
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return True
        return (mtime + self.ttl_hours * 3600) < time.time()

    def _evict(self) -> int:
        """按 ``mtime`` 升序淘汰到容量上限以内，返回删除数量。"""
        cap = self.max_bytes
        if cap <= 0:
            return 0
        with self._lock:
            entries: list[tuple[float, str, int, Path]] = []
            total = 0
            for path in self._iter_files():
                try:
                    stat = path.stat()
                except OSError:
                    continue
                entries.append((stat.st_mtime, path.name, stat.st_size, path))
                total += stat.st_size
            if total <= cap:
                return 0
            entries.sort(key=lambda item: (item[0], item[1]))
            removed = 0
            for _mtime, _name, size, path in entries:
                if total <= cap:
                    break
                if self._unlink(path):
                    total -= size
                    removed += 1
            return removed

    @staticmethod
    def _unlink(path: Path) -> bool:
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError:
            return False

    def _warn(self, message: str, *args: object) -> None:
        """日志是可选能力：没传 logger（或 logger 不认识 warning）就静默。"""
        warning = getattr(self.logger, "warning", None)
        if not callable(warning):
            return
        try:
            warning(message, *args)
        except Exception:  # pragma: no cover - 日志失败不该影响缓存
            pass


def _clean_format(value: str | None) -> str:
    if not value:
        return ""
    return str(value).strip().lstrip(".").lower()


def _sniff_format(data: bytes) -> str | None:
    """按魔数猜容器格式，猜不出返回 ``None``。

    调用方只传 ``put(key, data)`` 时（契约冻结签名的常态），靠这一步保证
    磁盘上的扩展名仍然是真实格式（契约 §7）。
    """
    if not data:
        return None
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    if data[:4] == b"OggS":
        return "opus"
    if data[:3] == b"ID3":
        return "mp3"
    if len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        return "mp3"
    return None
