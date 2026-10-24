"""硅基流动 TTS HTTP 客户端。

公开签名（冻结，见 ``docs/CONTRACT.md`` §3.2 / §8）：

- ``ProviderError``：统一错误类型，``kind`` 取值
  ``config|auth|bad_request|rate_limit|server|network|timeout|empty``
- ``redact_secret(text)``：把 ``sk-xxxxxxxx`` 脱敏成 ``sk-****``（契约 §12）
- ``TTSSettings``（frozen dataclass）+ ``TTSSettings.from_config(cfg)``
- ``SiliconFlowTTSProvider(settings)``：``synthesize`` / ``request_json`` /
  ``list_voices``（v1.2 兼容薄壳）/ ``aclose``
- ``create_provider(cfg) -> SiliconFlowTTSProvider``

约束：**不依赖 maibot_sdk**，只用 httpx，可被 pytest 直接测（可注入 MockTransport）。
本模块是全仓库**唯一** ``import httpx`` 的地方：音色管理（``voice.py``）一律通过
``request_json`` 复用这里的连接、鉴权与错误归一化。
"""

from __future__ import annotations

import asyncio
import os
import re
import time
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
    "redact_secret",
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

#: 契约 §12：``sk-`` 开头的长串一律脱敏，展示用掩码固定 ``sk-****``。
_SECRET_RE = re.compile(r"sk-[A-Za-z0-9]{8,}")


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


def redact_secret(text: str) -> str:
    """把 ``sk-`` 开头的长串替换为 ``sk-****``；不改变其他内容。

    契约 §12 冻结签名。匹配 ``sk-[A-Za-z0-9]{8,}``，展示用掩码固定为
    ``sk-****``（不保留前后缀）。所有可能进日志/异常的字符串都要先过这里。
    """
    if not text:
        return "" if text is None else str(text)
    return _SECRET_RE.sub("sk-****", str(text))


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
    # 通用 JSON 调用（v1.2：音色管理走这里，不重复实现 HTTP）
    # ------------------------------------------------------------------ #
    async def request_json(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, object] | None = None,
        timeout: float | None = None,
    ) -> object:
        """带鉴权的 JSON 请求，返回解析后的原生对象（空体返回 ``None``）。

        - ``path`` 以 ``/`` 开头（缺了自动补），拼 ``{base_url}{path}``；
        - 2xx：解析 JSON 返回 ``dict``/``list``；**响应体不是 JSON 时**抛
          ``ProviderError(kind="bad_request", status=2xx)`` —— 调用方据此区分
          「HTTP 成功但内容不可解析」和真正的 HTTP 错误；
        - 非 2xx：按 :meth:`_http_error` 归一化（401/403→auth、4xx→bad_request…）；
        - **不套用重试**：音色管理是低频运维，失败让用户重试即可；
          二进制合成不走这里（用 :meth:`synthesize`）。
        """
        if not self.settings.api_key:
            raise ProviderError(
                "未配置 api_key（config.api_key 或环境变量 SILICONFLOW_API_KEY）",
                kind="config",
            )

        verb = (method or "GET").strip().upper() or "GET"
        endpoint = str(path or "/")
        if not endpoint.startswith("/"):
            endpoint = f"/{endpoint}"
        budget = _resolve_budget(timeout, self.settings.timeout)

        kwargs: dict[str, Any] = {
            "headers": self._auth_headers(accept="application/json"),
            "timeout": budget,
        }
        if payload is not None:
            kwargs["json"] = payload

        try:
            response = await self.client.request(
                verb, self._url(endpoint), **kwargs
            )
        except httpx.TimeoutException as exc:
            raise ProviderError(
                f"{verb} {endpoint} 超时：{type(exc).__name__}", kind="timeout"
            ) from exc
        except httpx.TransportError as exc:
            raise ProviderError(
                f"{verb} {endpoint} 网络错误：{type(exc).__name__}: {self._scrub(str(exc))}",
                kind="network",
            ) from exc

        if not 200 <= response.status_code < 300:
            raise self._http_error(response)

        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            raise ProviderError(
                f"{verb} {endpoint} 返回非 JSON（HTTP {response.status_code}）："
                f"{self._body_text(response)}",
                kind="bad_request",
                status=response.status_code,
            ) from None

    # ------------------------------------------------------------------ #
    # 音色列表（v1.2 兼容薄壳，v1.3 移除）
    # ------------------------------------------------------------------ #
    async def list_voices(self, *, timeout: float | None = None) -> list[str]:
        """兼容薄壳：委托 :func:`voice.list_voices`，只取 ``uri`` 字符串。

        保留它只为不打断已落地的 ``plugin.py`` 与既有测试；新代码一律用
        ``voice.list_voices``（能拿到 ``VoiceProfile`` 的 name/model）。
        """
        from . import voice as voice_module  # 函数内懒导入，避免模块级循环依赖

        profiles = await voice_module.list_voices(self, timeout=timeout)
        return [profile.uri for profile in profiles]

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
        """脱敏：错误信息里绝不出现 ``api_key`` / ``Authorization`` 原文。"""
        if not text:
            return ""
        key = self.settings.api_key
        if key:
            text = text.replace(key, "sk-****")
        return redact_secret(text)

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
