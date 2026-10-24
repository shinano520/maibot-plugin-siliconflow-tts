"""``cache.py`` 单测：键、命中、过期、容量淘汰、开关、清理。"""

from __future__ import annotations

import hashlib
import os
import time

import pytest

PAYLOAD = b"ID3\x03\x00fake-mp3-bytes"


def _kwargs(**overrides):
    values = {
        "text": "你好世界",
        "model": "FunAudioLLM/CosyVoice2-0.5B",
        "voice": "FunAudioLLM/CosyVoice2-0.5B:alex",
        "speed": 1.0,
        "gain": 0.0,
        "response_format": "mp3",
        "sample_rate": 44100,
    }
    values.update(overrides)
    return values


# --------------------------------------------------------------------------- #
# make_key
# --------------------------------------------------------------------------- #
def test_make_key_matches_frozen_formula(cache_module):
    kwargs = _kwargs()
    raw = (
        f"{kwargs['model']}|{kwargs['voice']}|{kwargs['speed']:.3f}|"
        f"{kwargs['gain']:.3f}|{kwargs['response_format']}|"
        f"{kwargs['sample_rate']}|{kwargs['text']}"
    )
    expected = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    assert cache_module.make_key(**kwargs) == expected
    assert len(expected) == 64


@pytest.mark.parametrize(
    "field,value",
    [
        ("text", "你好世界！"),
        ("model", "other/model"),
        ("voice", "FunAudioLLM/CosyVoice2-0.5B:anna"),
        ("speed", 1.5),
        ("gain", 2.0),
        ("response_format", "wav"),
        ("sample_rate", 32000),
    ],
)
def test_make_key_uses_every_field(cache_module, field, value):
    base = cache_module.make_key(**_kwargs())
    changed = cache_module.make_key(**_kwargs(**{field: value}))
    assert base != changed, f"{field} 变化必须换 key，避免换音色还放旧声音"


def test_instance_make_key_delegates(cache_module, tmp_path):
    cache = cache_module.AudioCache(tmp_path)
    assert cache.make_key(**_kwargs()) == cache_module.make_key(**_kwargs())


# --------------------------------------------------------------------------- #
# 命中 / 未命中
# --------------------------------------------------------------------------- #
def test_put_then_get_roundtrip(cache_module, tmp_path):
    cache = cache_module.AudioCache(tmp_path)
    key = cache_module.make_key(**_kwargs())
    assert cache.get(key) is None
    cache.put(key, PAYLOAD)
    assert cache.get(key) == PAYLOAD

    path = cache.path_for(key)
    assert path == tmp_path / "audio" / key[:2] / f"{key}.mp3"
    assert path.is_file()
    assert not list(path.parent.glob("*.tmp")), "不许残留 .tmp"


def test_get_missing_key_returns_none(cache_module, tmp_path):
    cache = cache_module.AudioCache(tmp_path)
    assert cache.get("0" * 64) is None
    assert cache.get("") is None


def test_corrupt_or_empty_file_counts_as_miss(cache_module, tmp_path):
    cache = cache_module.AudioCache(tmp_path)
    key = cache_module.make_key(**_kwargs())
    path = cache.path_for(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    assert cache.get(key) is None
    assert not path.exists(), "空文件应被清掉"


# --------------------------------------------------------------------------- #
# enabled=False
# --------------------------------------------------------------------------- #
def test_disabled_cache_bypasses_io(cache_module, tmp_path):
    cache = cache_module.AudioCache(tmp_path, enabled=False)
    key = cache_module.make_key(**_kwargs())
    cache.put(key, PAYLOAD)
    assert cache.get(key) is None
    assert cache.stats()["count"] == 0


# --------------------------------------------------------------------------- #
# TTL
# --------------------------------------------------------------------------- #
def test_expired_entry_is_deleted(cache_module, tmp_path):
    cache = cache_module.AudioCache(tmp_path, ttl_hours=1)
    key = cache_module.make_key(**_kwargs())
    cache.put(key, PAYLOAD)
    path = cache.path_for(key)

    old = time.time() - 2 * 3600
    os.utime(path, (old, old))

    assert cache.get(key) is None
    assert not path.exists()


def test_fresh_entry_survives_ttl(cache_module, tmp_path):
    cache = cache_module.AudioCache(tmp_path, ttl_hours=1)
    key = cache_module.make_key(**_kwargs())
    cache.put(key, PAYLOAD)
    assert cache.get(key) == PAYLOAD


def test_ttl_zero_never_expires(cache_module, tmp_path):
    cache = cache_module.AudioCache(tmp_path, ttl_hours=0)
    key = cache_module.make_key(**_kwargs())
    cache.put(key, PAYLOAD)
    old = time.time() - 999 * 3600
    os.utime(cache.path_for(key), (old, old))
    assert cache.get(key) == PAYLOAD


def test_purge_expired_sweeps_old_files(cache_module, tmp_path):
    cache = cache_module.AudioCache(tmp_path, ttl_hours=1)
    old_key = cache_module.make_key(**_kwargs(text="旧"))
    new_key = cache_module.make_key(**_kwargs(text="新"))
    cache.put(old_key, PAYLOAD)
    cache.put(new_key, PAYLOAD)

    old = time.time() - 5 * 3600
    os.utime(cache.path_for(old_key), (old, old))

    assert cache.purge_expired() == 1
    assert cache.get(new_key) == PAYLOAD


# --------------------------------------------------------------------------- #
# 容量 LRU
# --------------------------------------------------------------------------- #
def test_eviction_removes_oldest_until_under_cap(cache_module, tmp_path):
    # 0.01MB = 10485 字节：三个 4096 放不下，最老的会被淘汰
    cache = cache_module.AudioCache(tmp_path, max_mb=0.01, ttl_hours=0)
    keys = [cache_module.make_key(**_kwargs(text=f"文本{i}")) for i in range(3)]
    blobs = [bytes([65 + i]) * 4096 for i in range(3)]

    for index, (key, blob) in enumerate(zip(keys, blobs)):
        cache.put(key, blob)
        stamp = 1000.0 + index * 100
        os.utime(cache.path_for(key), (stamp, stamp))

    assert not cache.path_for(keys[0]).exists(), "最老的应当被淘汰"
    assert cache.path_for(keys[1]).exists()
    assert cache.path_for(keys[2]).exists()
    assert cache.stats()["bytes"] <= cache.max_bytes


def test_single_file_larger_than_cap_is_not_written(cache_module, tmp_path):
    cache = cache_module.AudioCache(tmp_path, max_mb=0.001)  # 1048 字节
    key = cache_module.make_key(**_kwargs())
    cache.put(key, b"x" * 4096)
    assert cache.get(key) is None
    assert cache.stats()["count"] == 0


def test_oversized_write_logs_one_warning(cache_module, tmp_path):
    class _Recorder:
        def __init__(self):
            self.calls = []

        def warning(self, message, *args):
            self.calls.append((message, args))

    recorder = _Recorder()
    cache = cache_module.AudioCache(tmp_path, max_mb=0.001, logger=recorder)
    cache.put(cache_module.make_key(**_kwargs()), b"x" * 4096)

    assert len(recorder.calls) == 1
    assert "跳过写入" in recorder.calls[0][0]


def test_max_mb_zero_means_unlimited(cache_module, tmp_path):
    cache = cache_module.AudioCache(tmp_path, max_mb=0)
    keys = [cache_module.make_key(**_kwargs(text=f"t{i}")) for i in range(3)]
    for key in keys:
        cache.put(key, b"x" * 4096)
    assert cache.stats()["count"] == 3


# --------------------------------------------------------------------------- #
# stats / clear
# --------------------------------------------------------------------------- #
def test_stats_reports_count_bytes_root(cache_module, tmp_path):
    cache = cache_module.AudioCache(tmp_path)
    stats = cache.stats()
    assert stats == {"count": 0, "bytes": 0, "root": str(tmp_path)}

    key = cache_module.make_key(**_kwargs())
    cache.put(key, PAYLOAD)
    stats = cache.stats()
    assert stats["count"] == 1
    assert stats["bytes"] == len(PAYLOAD)
    assert stats["root"] == str(tmp_path)


def test_clear_removes_everything(cache_module, tmp_path):
    cache = cache_module.AudioCache(tmp_path)
    for index in range(3):
        cache.put(cache_module.make_key(**_kwargs(text=f"c{index}")), PAYLOAD)
    assert cache.stats()["count"] == 3

    assert cache.clear() == 3
    assert cache.stats()["count"] == 0
    assert cache.clear() == 0


# --------------------------------------------------------------------------- #
# 扩展名 / 原子写
# --------------------------------------------------------------------------- #
def test_extension_follows_response_format_but_get_is_agnostic(cache_module, tmp_path):
    cache = cache_module.AudioCache(tmp_path)
    key = cache_module.make_key(**_kwargs(response_format="wav"))
    cache.put(key, b"RIFF....WAVE", response_format="wav")

    path = cache.path_for(key, "wav")
    assert path.name == f"{key}.wav"
    # 不传 response_format 也能读到（按 key 前缀匹配）
    assert cache.get(key) == b"RIFF....WAVE"


def test_read_does_not_refresh_mtime(cache_module, tmp_path):
    """TTL 与 LRU 都以写入时间为准，读命中不改时间戳，行为才可复现。"""
    cache = cache_module.AudioCache(tmp_path, ttl_hours=0)
    key = cache_module.make_key(**_kwargs())
    cache.put(key, PAYLOAD)
    path = cache.path_for(key)
    before = path.stat().st_mtime
    assert cache.get(key) == PAYLOAD
    assert path.stat().st_mtime == before


@pytest.mark.parametrize(
    ("blob", "ext"),
    [
        (b"RIFF\x24\x00\x00\x00WAVEfmt ", "wav"),
        (b"OggS\x00\x02\x00\x00opushead", "opus"),
        (b"ID3\x03\x00mp3tag", "mp3"),
        (b"\xff\xfb\x90\x00mp3frame", "mp3"),
        (b"random-bytes-without-magic", "mp3"),
    ],
)
def test_put_sniffs_container_when_format_not_given(cache_module, tmp_path, blob, ext):
    """只传 ``put(key, data)`` 时也要按真实容器命名文件（契约 §7）。"""
    cache = cache_module.AudioCache(tmp_path)  # 未指定 response_format
    key = cache_module.make_key(**_kwargs())
    cache.put(key, blob)

    path = cache.path_for(key, ext)
    assert path.exists(), f"期望落到 .{ext}"
    assert cache.get(key) == blob
