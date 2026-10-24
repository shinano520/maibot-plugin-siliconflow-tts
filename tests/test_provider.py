"""``provider.py`` 单测：错误归一化、重试、请求体、音色列表。

全部走 ``httpx.MockTransport``，不联网、不打真实 API。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

BASE = "https://api.test.local/v1"
AUDIO = b"ID3\x03\x00fake-audio-bytes"


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


def _provider(provider_module, handler, **overrides):
    return provider_module.SiliconFlowTTSProvider(
        _settings(provider_module, **overrides),
        transport=httpx.MockTransport(handler),
    )


async def _synthesize(provider, text="你好世界", *, timeout=None):
    try:
        return await provider.synthesize(text, timeout=timeout)
    finally:
        await provider.aclose()


@pytest.fixture(autouse=True)
def _no_backoff(provider_module, monkeypatch):
    """退避基数归零，单测不真的 sleep。"""
    monkeypatch.setattr(provider_module, "_BACKOFF_BASE", 0.0)


# --------------------------------------------------------------------------- #
# 成功路径
# --------------------------------------------------------------------------- #
def test_synthesize_success_returns_bytes(provider_module):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=AUDIO, headers={"content-type": "audio/mpeg"})

    provider = _provider(provider_module, handler)
    audio = asyncio.run(_synthesize(provider, "你好世界"))

    assert audio == AUDIO
    assert len(seen) == 1
    request = seen[0]
    assert request.method == "POST"
    assert str(request.url) == f"{BASE}/audio/speech"
    assert request.headers["authorization"] == "Bearer sk-secret"
    assert request.headers["content-type"] == "application/json"

    body = json.loads(request.content)
    assert set(body) == {
        "model",
        "voice",
        "input",
        "speed",
        "gain",
        "response_format",
        "sample_rate",
    }
    assert body["input"] == "你好世界"
    assert " " not in body["input"]
    assert body["model"] == "FunAudioLLM/CosyVoice2-0.5B"
    assert body["voice"] == "FunAudioLLM/CosyVoice2-0.5B:alex"
    assert body["speed"] == 1.0
    assert body["gain"] == 0.0
    assert body["response_format"] == "mp3"
    assert body["sample_rate"] == 44100


def test_base_url_trailing_slash_is_normalised(provider_module):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, content=AUDIO)

    provider = _provider(provider_module, handler, base_url="https://api.test.local/v1///")
    asyncio.run(_synthesize(provider))
    assert seen == [f"{BASE}/audio/speech"]


def test_aclose_closes_client(provider_module):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=AUDIO)

    async def scenario() -> bool:
        provider = _provider(provider_module, handler)
        await provider.synthesize("你好")
        client = provider.client
        assert not client.is_closed
        await provider.aclose()
        return client.is_closed

    assert asyncio.run(scenario()) is True


# --------------------------------------------------------------------------- #
# 鉴权 / 断言错误
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("status", [401, 403])
def test_auth_errors_are_not_retried(provider_module, status):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, json={"message": "invalid token sk-secret"})

    provider = _provider(provider_module, handler, retry=3)
    with pytest.raises(provider_module.ProviderError) as info:
        asyncio.run(_synthesize(provider))

    error = info.value
    assert error.kind == "auth"
    assert error.status == status
    assert len(calls) == 1, "4xx 不重试"
    assert "sk-secret" not in str(error), "错误信息必须脱敏"


def test_bad_request_is_not_retried(provider_module):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(422, json={"message": "input too long"})

    provider = _provider(provider_module, handler, retry=2)
    with pytest.raises(provider_module.ProviderError) as info:
        asyncio.run(_synthesize(provider))

    assert info.value.kind == "bad_request"
    assert info.value.status == 422
    assert len(calls) == 1


def test_missing_api_key_raises_config_without_request(provider_module):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        calls.append(request)
        return httpx.Response(200, content=AUDIO)

    provider = _provider(provider_module, handler, api_key="")
    with pytest.raises(provider_module.ProviderError) as info:
        asyncio.run(_synthesize(provider))

    assert info.value.kind == "config"
    assert calls == []


@pytest.mark.parametrize("text", ["", "   ", "\n\t"])
def test_blank_text_raises_empty_without_request(provider_module, text):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        calls.append(request)
        return httpx.Response(200, content=AUDIO)

    provider = _provider(provider_module, handler)
    with pytest.raises(provider_module.ProviderError) as info:
        asyncio.run(_synthesize(provider, text))

    assert info.value.kind == "empty"
    assert calls == []


# --------------------------------------------------------------------------- #
# 限流 / 服务端 / 网络 / 超时（可重试）
# --------------------------------------------------------------------------- #
def test_rate_limit_is_retried_then_raised(provider_module):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(429, json={"message": "slow down"}, headers={"Retry-After": "0"})

    provider = _provider(provider_module, handler, retry=2)
    with pytest.raises(provider_module.ProviderError) as info:
        asyncio.run(_synthesize(provider))

    assert info.value.kind == "rate_limit"
    assert info.value.status == 429
    assert len(calls) == 3, "retry=2 → 首次 + 2 次重试"


def test_rate_limit_then_success(provider_module):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, json={"message": "slow down"})
        return httpx.Response(200, content=AUDIO)

    provider = _provider(provider_module, handler, retry=2)
    assert asyncio.run(_synthesize(provider)) == AUDIO
    assert len(calls) == 2


def test_server_error_is_retried(provider_module):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(503, text="upstream unavailable")

    provider = _provider(provider_module, handler, retry=1)
    with pytest.raises(provider_module.ProviderError) as info:
        asyncio.run(_synthesize(provider))

    assert info.value.kind == "server"
    assert info.value.status == 503
    assert len(calls) == 2
    assert "upstream unavailable" in str(info.value)[:400]


def test_timeout_is_normalised_and_retried(provider_module):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ConnectTimeout("connect timeout", request=request)

    provider = _provider(provider_module, handler, retry=2)
    with pytest.raises(provider_module.ProviderError) as info:
        asyncio.run(_synthesize(provider))

    assert info.value.kind == "timeout"
    assert info.value.status is None
    assert len(calls) == 3


def test_network_error_is_normalised_and_retried(provider_module):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.ConnectError("connection refused", request=request)

    provider = _provider(provider_module, handler, retry=1)
    with pytest.raises(provider_module.ProviderError) as info:
        asyncio.run(_synthesize(provider))

    assert info.value.kind == "network"
    assert len(calls) == 2


def test_empty_response_body_is_not_retried(provider_module):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, content=b"")

    provider = _provider(provider_module, handler, retry=2)
    with pytest.raises(provider_module.ProviderError) as info:
        asyncio.run(_synthesize(provider))

    assert info.value.kind == "empty"
    assert len(calls) == 1


# --------------------------------------------------------------------------- #
# 音色列表
# --------------------------------------------------------------------------- #
def test_list_voices_extracts_uris(provider_module):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={"data": [{"uri": "speech:alex:1"}, {"uri": "speech:anna:2"}, {"name": "fallback"}]},
        )

    async def scenario():
        provider = _provider(provider_module, handler)
        try:
            return await provider.list_voices()
        finally:
            await provider.aclose()

    assert asyncio.run(scenario()) == ["speech:alex:1", "speech:anna:2", "fallback"]
    assert str(seen[0].url) == f"{BASE}/audio/voice/list"
    assert seen[0].method == "GET"
    assert seen[0].headers["authorization"] == "Bearer sk-secret"


def test_list_voices_error_is_normalised(provider_module):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "bad key"})

    async def scenario():
        provider = _provider(provider_module, handler)
        try:
            return await provider.list_voices()
        finally:
            await provider.aclose()

    with pytest.raises(provider_module.ProviderError) as info:
        asyncio.run(scenario())
    assert info.value.kind == "auth"


def test_list_voices_unknown_payload_returns_empty(provider_module):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    async def scenario():
        provider = _provider(provider_module, handler)
        try:
            return await provider.list_voices()
        finally:
            await provider.aclose()

    assert asyncio.run(scenario()) == []


# --------------------------------------------------------------------------- #
# 配置快照
# --------------------------------------------------------------------------- #
def test_settings_from_config_fills_defaults(provider_module, monkeypatch):
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
    settings = provider_module.TTSSettings.from_config(SimpleNamespace())

    assert settings.api_key == ""
    assert settings.base_url == provider_module.DEFAULT_BASE_URL
    assert settings.model == "FunAudioLLM/CosyVoice2-0.5B"
    assert settings.voice == "FunAudioLLM/CosyVoice2-0.5B:FunAudioLLM/CosyVoice2-0.5B"
    assert settings.response_format == "mp3"
    assert settings.sample_rate == 44100
    assert settings.retry == 2
    assert settings.timeout == 60.0


def test_settings_from_config_reads_plugin_config_shape(provider_module, monkeypatch):
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
    cfg = SimpleNamespace(
        api_key="sk-plain",
        base_url="https://proxy.local/v1/",
        model="FunAudioLLM/CosyVoice2-0.5B",
        voice="alex",
        speed=1.2,
        gain=-2.5,
        response_format="WAV",
        sample_rate=16000,
        retry=4,
        timeout_ms=30000,
    )
    settings = provider_module.TTSSettings.from_config(cfg)

    assert settings.api_key == "sk-plain"
    assert settings.base_url == "https://proxy.local/v1"
    assert settings.voice == "FunAudioLLM/CosyVoice2-0.5B:alex"
    assert settings.speed == 1.2
    assert settings.gain == -2.5
    assert settings.response_format == "wav"
    assert settings.sample_rate == 16000
    assert settings.retry == 4
    assert settings.timeout == 30.0


def test_settings_from_config_prefers_derived_properties(provider_module, monkeypatch):
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
    cfg = SimpleNamespace(
        api_key="sk-plain",
        effective_api_key="sk-derived",
        model="m",
        voice="alex",
        effective_voice="m:alex",
        timeout_seconds=12.5,
    )
    settings = provider_module.TTSSettings.from_config(cfg)
    assert settings.api_key == "sk-derived"
    assert settings.voice == "m:alex"
    assert settings.timeout == 12.5


def test_settings_from_config_env_fallback(provider_module, monkeypatch):
    monkeypatch.setenv("SILICONFLOW_API_KEY", "sk-from-env")
    settings = provider_module.TTSSettings.from_config(SimpleNamespace())
    assert settings.api_key == "sk-from-env"


def test_create_provider_uses_config(provider_module):
    cfg = SimpleNamespace(
        api_key="sk-x",
        base_url=BASE,
        model="m",
        voice="m:v",
        speed=1.0,
        gain=0.0,
        response_format="opus",
        sample_rate=48000,
        retry=1,
        timeout_ms=15000,
    )
    provider = provider_module.create_provider(cfg)
    assert isinstance(provider, provider_module.SiliconFlowTTSProvider)
    assert provider.settings.api_key == "sk-x"
    assert provider.settings.response_format == "opus"
    assert provider.settings.timeout == 15.0
