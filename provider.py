"""硅基流动 TTS HTTP 客户端。

公开签名（冻结，见 ``docs/CONTRACT.md`` §3.2 / §8）：

- ``ProviderError``：统一错误类型，``kind`` 取值
  ``config|auth|bad_request|rate_limit|server|network|timeout|empty``
- ``TTSSettings``（frozen dataclass）+ ``TTSSettings.from_config(cfg)``
- ``SiliconFlowTTSProvider(settings)``：``synthesize`` / ``list_voices`` / ``aclose``
- ``create_provider(cfg) -> SiliconFlowTTSProvider``

约束：**不依赖 maibot_sdk**，只用 httpx，可被 pytest 直接测（可注入 MockTransport）。
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_TIMEOUT",
    "ProviderError",
    "SiliconFlowTTSProvider",
    "TTSSettings",
    "create_provider",
]

DEFAULT_BASE_URL = "https://api.siliconflow.cn/v1"
DEFAULT_TIMEOUT = 60.0
DEFAULT_MODEL = "FunAudioLLM/CosyVoice2-0.5B"
USER_AGENT = "maibot-plugin-siliconflow-tts/0.1.0"

SPEECH_PATH = "/audio/speech"
VOICE_LIST_PATH = "/audio/voice/list"

#: 指数退避基数（秒）：第 n 次重试前等 ``_BACKOFF_BASE * 2**n``。
_BACKOFF_BASE = 0.5
#: 错误信息里保留的响应体长度上限。
_MAX_ERROR_BODY = 300
#: 单次 ``Retry-After`` 的上限，避免服务端给出离谱值把整个超时预算耗光。
_MAX_RETRY_AFTER = 30.0
#: 值得重试的 ``ProviderError.kind``。
RETRYABLE_KINDS = frozenset({"rate_limit", "server", "timeout", "network"})


class ProviderError(RuntimeError):
    """统一的 TTS 调用错误。

    ``kind``：

    - ``config``：配置问题（如 ``api_key`` 为空）
    - ``auth``：401 / 403
    - ``bad_request``：其它 4xx（400 / 404 / 422 …）
    - ``rate_limit``：429
    - ``server``：5xx
    - ``network``：连接层错误
    - ``timeout``：超时
    - ``empty``：输入为空或响应音频为空
    """

    kind: str
    status: int | None
    retry_after: float | None

    def __init__(
        self,
        message: str,
        *,
        kind: str,
        status: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.retry_after = retry_after

    def __repr__(self) -> str:  # pragma: no cover - 仅调试可读性
        return (
            f"ProviderError(kind={self.kind!r}, status={self.status!r}, "
            f"message={str(self)!r})"
        )


@dataclass(frozen=True, slots=True)
class TTSSettings:
    """一次合成所需的全部参数（不可变，换配置就换实例）。"""

    api_key: str
    base_url: str
    model: str
    voice: str
    speed: float
    gain: float
    response_format: str
    sample_rate: int
    retry: int
    timeout: float

    @classmethod
    def from_config(cls, cfg: object) -> "TTSSettings":
        """从 ``SiliconFlowTTSConfig``（或任意同名属性的对象）读出一份快照。

        这里只做 ``getattr``，不 import 配置类，保证本模块可脱离 SDK 使用。
        """

        def pick(name: str, default: Any) -> Any:
            value = getattr(cfg, name, default)
            return default if value is None else value

        api_key = str(
            pick("effective_api_key", "")
            or pick("api_key", "")
            or os.environ.get("SILICONFLOW_API_KEY", "")
        ).strip()

        model = str(pick("model", DEFAULT_MODEL) or DEFAULT_MODEL)
        raw_voice = str(pick("voice", "") or "")
        voice = str(pick("effective_voice", "") or "")
        if not voice:
            voice = raw_voice or model
            if ":" not in voice and not voice.startswith("speech:"):
                voice = f"{model}:{voice}"

        base_url = str(pick("base_url", DEFAULT_BASE_URL) or DEFAULT_BASE_URL).rstrip("/")

        timeout = pick("timeout_seconds", None)
        if timeout is None:
            timeout_ms = pick("timeout_ms", None)
            timeout = float(timeout_ms) / 1000.0 if timeout_ms else DEFAULT_TIMEOUT

        return cls(
            api_key=api_key,
            base_url=base_url or DEFAULT_BASE_URL,
            model=model,
            voice=voice,
            speed=float(pick("speed", 1.0)),
            gain=float(pick("gain", 0.0)),
            response_format=_clean_format(pick("response_format", "mp3")),
            sample_rate=int(pick("sample_rate", 44100)),
            retry=max(0, int(pick("retry", 2))),
            timeout=max(0.001, float(timeout)),
        )


class SiliconFlowTTSProvider:
    """复用同一个 ``httpx.AsyncClient`` 的 TTS 客户端。

    实例方法都是协程；``aclose()`` 在插件卸载时调用。
    ``transport`` / ``client`` 只给单测注入用，线上不传。
    """

    def __init__(
        self,
        settings: TTSSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self._transport = transport
        self._client = client
        self._owns_client = client is None

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    @property
    def client(self) -> httpx.AsyncClient:
        """懒创建并复用 ``httpx.AsyncClient``。"""
        if self._client is None:
            self._client = httpx.AsyncClient(
                transport=self._transport,
                timeout=self.settings.timeout or DEFAULT_TIMEOUT,
                headers={"User-Agent": USER_AGENT},
            )
        return self._client

    async def aclose(self) -> None:
        client, self._client = self._client, None
        if client is not None and self._owns_client:
            await client.aclose()

    async def __aenter__(self) -> "SiliconFlowTTSProvider":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------ #
    # 合成
    # ------------------------------------------------------------------ #
    async def synthesize(self, text: str, *, timeout: float | None = None) -> bytes:
        """把**已清洗**的文本合成成音频字节（未转码、未 base64）。"""
        cleaned = (text or "").strip()
        if not cleaned:
            raise ProviderError("TTS 输入文本为空", kind="empty")
        if not self.settings.api_key:
            raise ProviderError(
                "未配置 api_key（config.api_key 或环境变量 SILICONFLOW_API_KEY）",
                kind="config",
            )

        payload = {
            "model": self.settings.model,
            "voice": self.settings.voice,
            "input": cleaned,
            "speed": float(self.settings.speed),
            "gain": float(self.settings.gain),
            "response_format": self.settings.response_format,
            "sample_rate": int(self.settings.sample_rate),
        }

        budget = _resolve_budget(timeout, self.settings.timeout)
        deadline = time.monotonic() + budget
        attempts = max(0, int(self.settings.retry)) + 1
        last_error: ProviderError | None = None

        for attempt in range(attempts):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProviderError(
                    f"TTS 请求超时（预算 {budget:.1f}s）",
                    kind="timeout",
                )
            try:
                response = await self.client.post(
                    self._url(SPEECH_PATH),
                    json=payload,
                    headers=self._auth_headers(),
                    timeout=remaining,
                )
            except httpx.TimeoutException as exc:
                last_error = ProviderError(
                    f"TTS 请求超时：{type(exc).__name__}",
                    kind="timeout",
                )
            except httpx.TransportError as exc:
                last_error = ProviderError(
                    f"TTS 网络错误：{type(exc).__name__}: {self._scrub(str(exc))}",
                    kind="network",
                )
            else:
                if response.status_code == 200:
                    audio = response.content
                    if not audio:
                        raise ProviderError(
                            "TTS 响应音频为空",
                            kind="empty",
                            status=response.status_code,
                        )
                    return audio
                error = self._http_error(response)
                if error.kind not in RETRYABLE_KINDS:
                    raise error
                last_error = error

            if attempt + 1 >= attempts:
                break
            delay = self._retry_delay(attempt, last_error)
            remaining = deadline - time.monotonic()
            if remaining <= 0 or delay >= remaining:
                break
            await asyncio.sleep(delay)

        if last_error is None:  # pragma: no cover - 循环必然赋值
            raise ProviderError("TTS 调用失败", kind="network")
        raise last_error

    # ------------------------------------------------------------------ #
    # 音色列表
    # ------------------------------------------------------------------ #
    async def list_voices(self, *, timeout: float | None = None) -> list[str]:
        """``GET {base_url}/audio/voice/list``，返回音色 ``uri`` 列表。"""
        if not self.settings.api_key:
            raise ProviderError(
                "未配置 api_key（config.api_key 或环境变量 SILICONFLOW_API_KEY）",
                kind="config",
            )
        budget = _resolve_budget(timeout, self.settings.timeout)
        try:
            response = await self.client.get(
                self._url(VOICE_LIST_PATH),
                headers=self._auth_headers(accept="application/json"),
                timeout=budget,
            )
        except httpx.TimeoutException as exc:
            raise ProviderError(
                f"获取音色列表超时：{type(exc).__name__}", kind="timeout"
            ) from exc
        except httpx.TransportError as exc:
            raise ProviderError(
                f"获取音色列表网络错误：{type(exc).__name__}: {self._scrub(str(exc))}",
                kind="network",
            ) from exc

        if response.status_code != 200:
            raise self._http_error(response)
        return self._extract_voices(response)

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _url(self, path: str) -> str:
        return f"{self.settings.base_url.rstrip('/')}{path}"

    def _auth_headers(self, *, accept: str = "audio/*") -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.api_key}",
            "Content-Type": "application/json",
            "Accept": accept,
        }

    def _http_error(self, response: httpx.Response) -> ProviderError:
        status = response.status_code
        body = self._body_text(response)
        if status in (401, 403):
            return ProviderError(
                f"TTS 鉴权失败（HTTP {status}）：{body}", kind="auth", status=status
            )
        if status == 429:
            return ProviderError(
                f"TTS 触发限流（HTTP 429）：{body}",
                kind="rate_limit",
                status=status,
                retry_after=self._retry_after(response),
            )
        if 400 <= status < 500:
            return ProviderError(
                f"TTS 请求被拒（HTTP {status}）：{body}",
                kind="bad_request",
                status=status,
            )
        return ProviderError(
            f"TTS 服务端错误（HTTP {status}）：{body}", kind="server", status=status
        )

    def _body_text(self, response: httpx.Response) -> str:
        try:
            text = response.text
        except Exception:  # pragma: no cover - 解码异常不该冒泡到调用方
            return "<unreadable body>"
        return self._scrub(text[:_MAX_ERROR_BODY])

    def _scrub(self, text: str) -> str:
        """脱敏：错误信息里绝不出现 ``api_key`` / ``Authorization``。"""
        key = self.settings.api_key
        if key:
            text = text.replace(key, "***")
        return text.replace("Bearer ", "Bearer ***")

    @staticmethod
    def _retry_after(response: httpx.Response) -> float | None:
        raw = response.headers.get("Retry-After") or response.headers.get("retry-after")
        if not raw:
            return None
        try:
            value = float(str(raw).strip())
        except (TypeError, ValueError):
            return None
        return max(0.0, min(value, _MAX_RETRY_AFTER))

    @staticmethod
    def _retry_delay(attempt: int, error: ProviderError | None) -> float:
        if error is not None and error.retry_after is not None:
            return error.retry_after
        return _BACKOFF_BASE * (2**attempt)

    @staticmethod
    def _extract_voices(response: httpx.Response) -> list[str]:
        try:
            payload = response.json()
        except ValueError:
            return []
        items: Any = payload
        if isinstance(payload, Mapping):
            items = []
            for field in ("data", "result", "voices", "list", "uris"):
                if field in payload:
                    items = payload[field]
                    break
        if not isinstance(items, (list, tuple)):
            return []
        uris: list[str] = []
        for item in items:
            if isinstance(item, str):
                if item:
                    uris.append(item)
                continue
            if isinstance(item, Mapping):
                for field in ("uri", "name", "id", "voice"):
                    value = item.get(field)
                    if isinstance(value, str) and value:
                        uris.append(value)
                        break
        return uris


def create_provider(cfg: object) -> SiliconFlowTTSProvider:
    """按配置建一个可复用实例（``on_load`` 调用一次）。"""
    return SiliconFlowTTSProvider(TTSSettings.from_config(cfg))


def _resolve_budget(timeout: float | None, fallback: float) -> float:
    if timeout is not None:
        try:
            value = float(timeout)
        except (TypeError, ValueError):
            value = 0.0
        if value > 0:
            return value
    return float(fallback) if fallback and fallback > 0 else DEFAULT_TIMEOUT


def _clean_format(value: Any) -> str:
    text = str(value or "mp3").strip().lstrip(".").lower()
    return text or "mp3"
