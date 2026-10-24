"""``voice.py`` 单测：参考音频校验、克隆、列表容错、删除幂等。

全部 mock HTTP（``httpx.MockTransport``）或喂桩 provider，不联网。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from types import SimpleNamespace

import httpx
import pytest

BASE = "https://api.test.local/v1"
CLONE_URL = f"{BASE}/uploads/audio/voice"
LIST_URL = f"{BASE}/audio/voice/list"
DELETE_URL = f"{BASE}/audio/voice/deletions"

AUDIO = b"ID3\x03\x00fake-reference-audio"


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
def _settings(provider_module, **overrides):
    values = {
        "api_key": "sk-secret",
        "base_url": BASE,
        "model": "FunAudioLLM/CosyVoice2-0.5B",
        "voice": "FunAudioLLM/CosyVoice2-0.5B:alex",
        "speed": 1.0,
        "gain": 0.0,
        "response_format": "mp3",
        "sample_rate": 44100,
        "retry": 2,
        "timeout": 5.0,
    }
    values.update(overrides)
    return provider_module.TTSSettings(**values)


def run_http(provider_module, handler, scenario, **overrides):
    """用 MockTransport 起一个真 provider，跑完自动 aclose。"""

    async def main():
        provider = provider_module.SiliconFlowTTSProvider(
            _settings(provider_module, **overrides),
            transport=httpx.MockTransport(handler),
        )
        try:
            return await scenario(provider)
        finally:
            await provider.aclose()

    return asyncio.run(main())


class _StubProvider:
    """只记录调用、不发网络；``result`` 为 Exception 时抛出。"""

    def __init__(self, result=None, *, model="stub-model"):
        self.settings = SimpleNamespace(model=model)
        self.result = result
        self.calls: list[tuple[str, str, dict | None]] = []

    async def request_json(self, method, path, *, payload=None, timeout=None):
        self.calls.append((method, path, payload))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def run_stub(voice_module, stub, scenario):
    """桩 provider：只跑协程、不发网络。第一个参数是被测模块，便于在桩路径上也断言模块面。"""

    async def main():
        return await scenario(stub)

    return asyncio.run(main())


@pytest.fixture()
def voices_dir(tmp_path):
    base = tmp_path / "voices"
    base.mkdir()
    return base


# --------------------------------------------------------------------------- #
# read_reference_audio：路径与后缀校验
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("filename", "mime"),
    [
        ("ref.mp3", "audio/mpeg"),
        ("ref.wav", "audio/wav"),
        ("ref.pcm", "audio/L16"),
        ("ref.opus", "audio/ogg"),
        ("REF.MP3", "audio/mpeg"),  # 后缀大小写不敏感
    ],
)
def test_read_reference_audio_ok(voice_module, voices_dir, filename, mime):
    (voices_dir / filename).write_bytes(AUDIO)
    data, got_mime = voice_module.read_reference_audio(voices_dir, filename)
    assert data == AUDIO
    assert got_mime == mime


@pytest.mark.parametrize(
    "filename",
    [
        "",
        "   ",
        ".hidden.mp3",
        ".mp3",
        "sub/ref.mp3",
        "sub\\ref.mp3",
        "../ref.mp3",
        "..\\ref.mp3",
        "a..b.mp3",
        "C:\\Windows\\ref.mp3",
        "/etc/ref.mp3",
    ],
)
def test_read_reference_audio_rejects_bad_names(voice_module, voices_dir, filename):
    with pytest.raises(ValueError):
        voice_module.read_reference_audio(voices_dir, filename)


@pytest.mark.parametrize("filename", ["ref.txt", "ref.mp4", "ref", "ref.mp3.exe"])
def test_read_reference_audio_rejects_bad_suffix(voice_module, voices_dir, filename):
    (voices_dir / filename).write_bytes(AUDIO)
    with pytest.raises(ValueError) as info:
        voice_module.read_reference_audio(voices_dir, filename)
    assert "后缀" in str(info.value)


def test_read_reference_audio_missing_file(voice_module, voices_dir):
    with pytest.raises(FileNotFoundError):
        voice_module.read_reference_audio(voices_dir, "nope.mp3")


def test_read_reference_audio_empty_file(voice_module, voices_dir):
    (voices_dir / "empty.mp3").write_bytes(b"")
    with pytest.raises(ValueError) as info:
        voice_module.read_reference_audio(voices_dir, "empty.mp3")
    assert "空文件" in str(info.value)


def test_read_reference_audio_oversize(voice_module, voices_dir, monkeypatch):
    monkeypatch.setattr(voice_module, "MAX_AUDIO_BYTES", 16)
    (voices_dir / "big.mp3").write_bytes(b"x" * 17)
    with pytest.raises(ValueError) as info:
        voice_module.read_reference_audio(voices_dir, "big.mp3")
    assert "上限" in str(info.value)


# --------------------------------------------------------------------------- #
# clone_voice：成功路径
# --------------------------------------------------------------------------- #
def test_clone_voice_payload_and_profile(voice_module):
    stub = _StubProvider({"uri": "speech:my-voice:1:2"})
    profile = run_stub(
        voice_module,
        stub,
        lambda p: voice_module.clone_voice(
            p, name="my-voice", reference_text="这句话是被克隆的声音", audio_bytes=AUDIO
        ),
    )

    assert profile.uri == "speech:my-voice:1:2"
    assert profile.name == "my-voice"
    assert profile.model == "stub-model"

    method, path, payload = stub.calls[0]
    assert (method, path) == ("POST", "/uploads/audio/voice")
    assert set(payload) == {"model", "customName", "text", "audio"}
    assert payload["customName"] == "my-voice"
    assert payload["text"] == "这句话是被克隆的声音"
    prefix = "data:audio/mpeg;base64,"
    assert payload["audio"].startswith(prefix)
    assert base64.b64decode(payload["audio"][len(prefix) :]) == AUDIO


def test_clone_voice_honours_explicit_mime(voice_module):
    stub = _StubProvider({"uri": "speech:v:1:2"})
    run_stub(
        voice_module,
        stub,
        lambda p: voice_module.clone_voice(
            p, name="v", reference_text="t", audio_bytes=AUDIO, mime="audio/wav"
        ),
    )
    assert stub.calls[0][2]["audio"].startswith("data:audio/wav;base64,")


def test_clone_voice_from_audio_path_uses_suffix_mime(voice_module, voices_dir):
    (voices_dir / "ref.wav").write_bytes(AUDIO)
    stub = _StubProvider({"uri": "speech:v:1:2"})
    run_stub(
        voice_module,
        stub,
        lambda p: voice_module.clone_voice(
            p,
            name="v",
            reference_text="t",
            audio_path=voices_dir / "ref.wav",
            mime="audio/mpeg",  # 有路径时以后缀为准
        ),
    )
    assert stub.calls[0][2]["audio"].startswith("data:audio/wav;base64,")


def test_clone_voice_over_http(provider_module, voice_module):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"uri": "speech:real:aa:bb"})

    profile = run_http(
        provider_module,
        handler,
        lambda p: voice_module.clone_voice(
            p, name="real", reference_text="你好", audio_bytes=AUDIO
        ),
    )

    assert profile.uri == "speech:real:aa:bb"
    request = seen[0]
    assert request.method == "POST"
    assert str(request.url) == CLONE_URL
    assert request.headers["authorization"] == "Bearer sk-secret"
    assert request.headers["content-type"] == "application/json"
    body = json.loads(request.content)
    assert body["model"] == "FunAudioLLM/CosyVoice2-0.5B"
    assert body["customName"] == "real"


# --------------------------------------------------------------------------- #
# clone_voice：本地校验（不发请求）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", ["", "  ", "my voice", "a:b", "名字", "x" * 65])
def test_clone_voice_rejects_bad_name(voice_module, name):
    stub = _StubProvider({"uri": "speech:x:1:2"})
    with pytest.raises(ValueError) as info:
        run_stub(
            voice_module,
            stub,
            lambda p: voice_module.clone_voice(
                p, name=name, reference_text="t", audio_bytes=AUDIO
            ),
        )
    assert "音色名不合法" in str(info.value)
    assert stub.calls == []


@pytest.mark.parametrize("text", ["", "   ", "\n"])
def test_clone_voice_requires_reference_text(voice_module, text):
    stub = _StubProvider({"uri": "speech:x:1:2"})
    with pytest.raises(ValueError) as info:
        run_stub(
            voice_module,
            stub,
            lambda p: voice_module.clone_voice(
                p, name="ok", reference_text=text, audio_bytes=AUDIO
            ),
        )
    assert "参考文本" in str(info.value)
    assert stub.calls == []


def test_clone_voice_requires_audio_source(voice_module):
    stub = _StubProvider({"uri": "speech:x:1:2"})
    with pytest.raises(ValueError):
        run_stub(
            voice_module,
            stub,
            lambda p: voice_module.clone_voice(p, name="ok", reference_text="t"),
        )
    assert stub.calls == []


def test_clone_voice_rejects_empty_bytes(voice_module):
    stub = _StubProvider({"uri": "speech:x:1:2"})
    with pytest.raises(ValueError):
        run_stub(
            voice_module,
            stub,
            lambda p: voice_module.clone_voice(
                p, name="ok", reference_text="t", audio_bytes=b""
            ),
        )
    assert stub.calls == []


def test_clone_voice_rejects_oversize_bytes(voice_module, monkeypatch):
    monkeypatch.setattr(voice_module, "MAX_AUDIO_BYTES", 16)
    stub = _StubProvider({"uri": "speech:x:1:2"})
    with pytest.raises(ValueError) as info:
        run_stub(
            voice_module,
            stub,
            lambda p: voice_module.clone_voice(
                p, name="ok", reference_text="t", audio_bytes=b"x" * 17
            ),
        )
    assert "上限" in str(info.value)
    assert stub.calls == []


def test_clone_voice_warns_on_large_reference(voice_module, caplog):
    stub = _StubProvider({"uri": "speech:x:1:2"})
    with caplog.at_level(logging.WARNING):
        run_stub(
            voice_module,
            stub,
            lambda p: voice_module.clone_voice(
                p,
                name="ok",
                reference_text="t",
                audio_bytes=b"x" * (2 * 1024 * 1024 + 1),
            ),
        )
    assert any("30s" in record.getMessage() for record in caplog.records)


# --------------------------------------------------------------------------- #
# clone_voice：上游错误归一化
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("status", "kind"),
    [(400, "bad_request"), (401, "auth"), (403, "auth"), (422, "bad_request"), (500, "server")],
)
def test_clone_voice_upstream_errors(provider_module, voice_module, status, kind):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"message": "音频不合规 / 未实名"})

    with pytest.raises(provider_module.ProviderError) as info:
        run_http(
            provider_module,
            handler,
            lambda p: voice_module.clone_voice(
                p, name="ok", reference_text="t", audio_bytes=AUDIO
            ),
        )

    error = info.value
    assert error.kind == kind
    assert error.status == status
    assert "音频不合规" in str(error)


def test_clone_voice_response_without_uri(provider_module, voice_module):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "no-uri-here"})

    with pytest.raises(provider_module.ProviderError) as info:
        run_http(
            provider_module,
            handler,
            lambda p: voice_module.clone_voice(
                p, name="ok", reference_text="t", audio_bytes=AUDIO
            ),
        )
    assert info.value.kind == "bad_request"
    assert "uri" in str(info.value)


def test_clone_voice_error_redacts_secret(provider_module, voice_module):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"message": "bad key sk-abcdefghijklmn for project"}
        )

    with pytest.raises(provider_module.ProviderError) as info:
        run_http(
            provider_module,
            handler,
            lambda p: voice_module.clone_voice(
                p, name="ok", reference_text="t", audio_bytes=AUDIO
            ),
        )

    message = str(info.value)
    assert "sk-abcdefghijklmn" not in message
    assert "sk-****" in message


# --------------------------------------------------------------------------- #
# list_voices
# --------------------------------------------------------------------------- #
def test_list_voices_results_shape_and_sorting(provider_module, voice_module):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {"uri": "speech:zoe:1", "customName": "zoe", "model": "m"},
                    {"uri": "speech:anna:1", "customName": "Anna", "model": "m2"},
                    {"customName": "broken"},  # 缺 uri → 跳过
                    {"uri": "speech:bob:1", "name": "bob"},
                ]
            },
        )

    profiles = run_http(
        provider_module,
        handler,
        lambda p: voice_module.list_voices(p),
    )

    assert [profile.name for profile in profiles] == ["Anna", "bob", "zoe"]
    assert [profile.uri for profile in profiles] == [
        "speech:anna:1",
        "speech:bob:1",
        "speech:zoe:1",
    ]
    assert profiles[0].model == "m2"
    assert profiles[1].model == ""


@pytest.mark.parametrize(
    "payload",
    [
        [{"uri": "speech:a:1", "customName": "a"}],                     # 裸 list
        {"data": [{"uri": "speech:a:1", "customName": "a"}]},
        {"voices": [{"uri": "speech:a:1", "customName": "a"}]},
        {"list": [{"uri": "speech:a:1", "customName": "a"}]},
    ],
)
def test_list_voices_tolerant_shapes(provider_module, voice_module, payload):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    profiles = run_http(provider_module, handler, lambda p: voice_module.list_voices(p))
    assert [profile.uri for profile in profiles] == ["speech:a:1"]


def test_list_voices_empty(provider_module, voice_module):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": []})

    assert run_http(provider_module, handler, lambda p: voice_module.list_voices(p)) == []


def test_list_voices_unknown_structure_returns_empty(provider_module, voice_module, caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"foo": "bar"})

    with caplog.at_level(logging.WARNING):
        profiles = run_http(
            provider_module, handler, lambda p: voice_module.list_voices(p)
        )

    assert profiles == []
    assert any("结构无法识别" in record.getMessage() for record in caplog.records)


def test_list_voices_non_json_body_returns_empty(provider_module, voice_module, caplog):
    """2xx 但不是 JSON：按「结构无法识别」降级，命令展示不该崩。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>gateway</html>")

    with caplog.at_level(logging.WARNING):
        profiles = run_http(
            provider_module, handler, lambda p: voice_module.list_voices(p)
        )

    assert profiles == []
    assert any("无法解析" in record.getMessage() for record in caplog.records)


def test_list_voices_http_error_propagates(provider_module, voice_module):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "boom"})

    with pytest.raises(provider_module.ProviderError) as info:
        run_http(provider_module, handler, lambda p: voice_module.list_voices(p))
    assert info.value.kind == "server"


def test_list_voices_request_shape(provider_module, voice_module):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"results": []})

    run_http(provider_module, handler, lambda p: voice_module.list_voices(p))
    assert seen[0].method == "GET"
    assert str(seen[0].url) == LIST_URL
    assert seen[0].headers["accept"] == "application/json"


# --------------------------------------------------------------------------- #
# delete_voice
# --------------------------------------------------------------------------- #
def test_delete_voice_ok(provider_module, voice_module):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    assert (
        run_http(
            provider_module,
            handler,
            lambda p: voice_module.delete_voice(p, uri="speech:gone:1"),
        )
        is True
    )
    assert seen[0].method == "POST"
    assert str(seen[0].url) == DELETE_URL
    assert json.loads(seen[0].content) == {"uri": "speech:gone:1"}


def test_delete_voice_is_idempotent(provider_module, voice_module):
    """第一次删成功，第二次上游 404 → False（不抛）。"""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if len(calls) == 1:
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"message": "voice not found"})

    async def scenario(provider):
        first = await voice_module.delete_voice(provider, uri="speech:gone:1")
        second = await voice_module.delete_voice(provider, uri="speech:gone:1")
        return first, second

    assert run_http(provider_module, handler, scenario) == (True, False)
    assert len(calls) == 2


def test_delete_voice_rejects_empty_uri(voice_module):
    stub = _StubProvider({"ok": True})
    with pytest.raises(ValueError):
        run_stub(voice_module, stub, lambda p: voice_module.delete_voice(p, uri="  "))
    assert stub.calls == []


@pytest.mark.parametrize(("status", "kind"), [(401, "auth"), (500, "server")])
def test_delete_voice_other_errors_propagate(provider_module, voice_module, status, kind):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"message": "nope"})

    with pytest.raises(provider_module.ProviderError) as info:
        run_http(
            provider_module,
            handler,
            lambda p: voice_module.delete_voice(p, uri="speech:x:1"),
        )
    assert info.value.kind == kind


# --------------------------------------------------------------------------- #
# 模块边界
# --------------------------------------------------------------------------- #
def test_voice_module_does_not_import_httpx(voice_module):
    """契约 §3.6：voice.py 不直接碰 HTTP，连接与鉴权只有 provider 一份实现。"""
    assert not hasattr(voice_module, "httpx")


def test_voice_module_source_has_no_forbidden_imports(voice_module):
    """验收项：voice.py 既不 import maibot_sdk，也不 import httpx（源码级断言）。"""
    import re as _re
    from pathlib import Path as _Path

    source = _Path(voice_module.__file__).read_text(encoding="utf-8")
    assert _re.search(r"^\s*(?:from|import)\s+maibot_sdk", source, _re.M) is None
    assert _re.search(r"^\s*(?:from|import)\s+httpx", source, _re.M) is None
