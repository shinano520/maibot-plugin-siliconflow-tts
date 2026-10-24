"""硅基流动 TTS 插件入口：装配 + 三个生命周期 + ``voice`` 回复扩展 + ``/tts`` 命令组。

v1.2 起命令组包含音色克隆（``/tts clone``、``/tts voice list|del|rm``）；
音色 HTTP 细节全部在 ``voice.py``，本文件只提供 ``voice_dir`` 并转交调用。

职责边界（PLAN §4.3）：

- 本插件**绝不替主程序发送回复**：回复正文只在 ``before_send`` 阶段被改写，
  发送、历史与监控都由 Host 统一处理；
- 只有用户显式触发的 ``/tts`` 命令才会主动发出消息（命令本身就是「发一条」）。

能力授权：manifest 只声明 ``send.hybrid`` 与 ``config.get``，Host 会精确校验；
因此命令反馈用 ``send.hybrid`` 的单段 ``text``，回复扩展则完全不调用 ``ctx.send.*``。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from maibot_sdk import Command, MaiBotPlugin, ReplyExtension

from . import audio as audio_module
from . import commands as commands_module
from . import text as text_module
from .cache import AudioCache
from .config import API_KEY_ENV_VAR, SiliconFlowTTSConfig
from .provider import ProviderError, create_provider
from .reply_ext import (
    ATTACH_VOICE_KEY,
    KEEP_TEXT_KEY,
    VOICE_EXTENSION_NAME,
    build_messages,
    extension_parameters,
    is_chat_allowed,
    parse_peer_list,
    prepare_result,
    resolve_chat_peer,
)

if TYPE_CHECKING:  # pragma: no cover - 仅用于类型标注
    from .provider import SiliconFlowTTSProvider

#: ``@ReplyExtension(timeout_ms=)`` 的静态值；运行期 provider 超时取
#: ``min(config.timeout_ms/1000, MAX_PROVIDER_TIMEOUT_SECONDS)`` 以免被 Host 先掐断。
DEFAULT_EXTENSION_TIMEOUT_MS = 60000
MAX_PROVIDER_TIMEOUT_SECONDS = 55.0

#: 参考音频目录名：``<data_dir>/voices/``（CONTRACT §3.8，v1.2）
VOICE_DIR_NAME = "voices"


class EmptySpeechText(ValueError):
    """``text.prepare`` 之后文本为空：直接降级，不调 API。"""


class SiliconFlowTTSPlugin(MaiBotPlugin):
    """硅基流动语音合成插件。"""

    config_model = SiliconFlowTTSConfig

    def __init__(self) -> None:
        super().__init__()
        self.provider: "SiliconFlowTTSProvider | None" = None
        self.audio_cache: AudioCache | None = None
        self.runtime_state: commands_module.RuntimeState | None = None

    # ── 基础设施 ──────────────────────────────────────────────
    def _logger(self) -> logging.Logger:
        """优先用 Host 注入的插件 logger，上下文未就绪时回落到模块 logger。"""

        try:
            return self.ctx.logger
        except RuntimeError:
            return logging.getLogger(__name__)

    def _build_cache(self, cfg: SiliconFlowTTSConfig) -> AudioCache:
        """缓存根目录固定为 ``ctx.paths.data_dir``（插件不自拼路径）。"""

        return AudioCache(
            Path(self.ctx.paths.data_dir),
            ttl_hours=cfg.cache_ttl_hours,
            max_mb=cfg.cache_max_mb,
            enabled=cfg.cache_enabled,
        )

    def _provider_timeout(self) -> float:
        """provider 单次调用的超时（秒），不超过 Host 的 60s 计时上限。"""

        return min(self.config.timeout_seconds, MAX_PROVIDER_TIMEOUT_SECONDS)

    @staticmethod
    def _coerce_sample_rate(cfg: SiliconFlowTTSConfig) -> None:
        """把非法采样率回落成格式默认值，保证 provider 请求与缓存键一致。

        写入必须落到 ``cfg.plugin``（pydantic 字段所在层）；外层
        ``cfg.sample_rate`` 只是只读转发属性。
        """

        try:
            fmt = audio_module.normalize_format(cfg.response_format)
        except ValueError:
            return
        section = cfg.plugin
        resolved = audio_module.resolve_sample_rate(fmt, section.sample_rate)
        if resolved != section.sample_rate:
            section.sample_rate = resolved

    def _require_provider(self) -> "SiliconFlowTTSProvider":
        if self.provider is None:
            raise RuntimeError("TTS provider 尚未初始化（插件未完成 on_load）")
        return self.provider

    def is_enabled(self) -> bool:
        """运行期开关：``/tts on|off`` 的覆盖优先于配置 ``enabled``。"""

        try:
            configured = bool(self.config.enabled)
        except RuntimeError:
            configured = False
        if self.runtime_state is None:
            return configured
        return self.runtime_state.is_enabled(configured)

    @property
    def voice_dir(self) -> Path:
        """音色参考音频目录：``self.ctx.paths.data_dir / "voices"``（CONTRACT §3.8）。"""

        return Path(self.ctx.paths.data_dir) / VOICE_DIR_NAME

    def _ensure_voice_dir(self, logger: logging.Logger) -> None:
        """``on_load`` 时确保 ``voices/`` 存在；建不出来只告警，不阻断合成主链路。"""

        try:
            self.voice_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.warning("创建音色参考音频目录失败：%s（/tts clone 将不可用）", self.voice_dir, exc_info=True)
        else:
            logger.info("音色参考音频目录：%s", self.voice_dir)

    # ── 生命周期（必须由子类覆盖，Runner 会校验）────────────────
    async def on_load(self) -> None:
        cfg = self.config
        self._coerce_sample_rate(cfg)
        self.audio_cache = self._build_cache(cfg)
        self.provider = create_provider(cfg)
        self.runtime_state = commands_module.RuntimeState.load(self.ctx.paths.data_dir)

        logger = self._logger()
        self._ensure_voice_dir(logger)
        logger.info(
            "硅基流动 TTS 已加载：model=%s voice=%s format=%s/%sHz 缓存=%s 运行开关=%s 转码能力=%s",
            cfg.model,
            cfg.effective_voice or "(未配置)",
            cfg.response_format,
            cfg.sample_rate,
            "开" if cfg.cache_enabled else "关",
            "开" if self.is_enabled() else "关",
            audio_module.probe_transcoders(),
        )
        if not cfg.effective_api_key:
            logger.warning("未配置 api_key 且环境变量 %s 为空：voice 扩展会直接放行原消息", API_KEY_ENV_VAR)
        if cfg.auto_voice:
            logger.info(
                "auto_voice=true：当前版本尚未接入 reply Hook，Planner 未选 voice 时不会自动加语音（CONTRACT §5.4）"
            )

    async def on_unload(self) -> None:
        provider = self.provider
        self.provider = None
        self.audio_cache = None
        self.runtime_state = None
        if provider is None:
            return
        try:
            await provider.aclose()
        except Exception:  # noqa: BLE001 - 卸载阶段不再向上抛
            self._logger().warning("关闭 TTS provider 失败", exc_info=True)
        else:
            self._logger().info("硅基流动 TTS 已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        logger = self._logger()
        if scope != "self":
            logger.debug("全局配置热重载与本插件无关：scope=%s version=%s", scope, version)
            return

        cfg = self.config  # scope == "self" 时已是最新值，不重读 TOML
        self._coerce_sample_rate(cfg)
        previous = self.provider
        self.provider = create_provider(cfg)
        self.audio_cache = self._build_cache(cfg)
        if previous is not None:
            try:
                await previous.aclose()
            except Exception:  # noqa: BLE001
                logger.warning("热更新时关闭旧 provider 失败", exc_info=True)
        logger.info(
            "TTS 配置已热更新（version=%s）：voice=%s format=%s/%sHz 缓存=%s",
            version,
            cfg.effective_voice or "(未配置)",
            cfg.response_format,
            cfg.sample_rate,
            "开" if cfg.cache_enabled else "关",
        )

    # ── 合成链路（唯一「文本 → 音频」入口）──────────────────────
    def prepare_spoken_text(self, text: str) -> str:
        """按配置的 ``max_chars`` 清洗文本（去 markdown/艾特、按句截断）。"""

        return text_module.prepare(text or "", self.config.max_chars)

    async def synthesize_with_spoken(self, text: str) -> tuple[str, bytes]:
        """返回 ``(实际朗读文本, 音频字节)``：prepare → cache → provider → 格式闸门 → 回写缓存。"""

        cfg = self.config
        spoken = self.prepare_spoken_text(text)
        if not spoken:
            raise EmptySpeechText("text.prepare 之后文本为空")

        fmt = audio_module.normalize_format(cfg.response_format)
        sample_rate = audio_module.resolve_sample_rate(fmt, cfg.sample_rate)

        cache = self.audio_cache
        key = ""
        if cache is not None:
            key = cache.make_key(
                text=spoken,
                model=cfg.model,
                voice=cfg.effective_voice,
                speed=cfg.speed,
                gain=cfg.gain,
                response_format=fmt,
                sample_rate=sample_rate,
            )
            cached = await _to_thread(cache.get, key)
            if cached:
                self._logger().info("语音缓存命中：%s 字 / %s 字节", len(spoken), len(cached))
                return spoken, cached

        provider = self._require_provider()
        raw = await provider.synthesize(spoken, timeout=self._provider_timeout())
        audio_bytes = audio_module.ensure_compatible(raw, fmt)

        if cache is not None and key:
            try:
                await _to_thread(cache.put, key, audio_bytes, response_format=fmt)
            except Exception:  # noqa: BLE001 - 缓存写失败不影响本次发送
                self._logger().debug("语音缓存写入失败", exc_info=True)
        return spoken, audio_bytes

    async def _synthesize(self, text: str) -> bytes:
        """CONTRACT §3.7 冻结入口：返回音频字节。"""

        _, audio_bytes = await self.synthesize_with_spoken(text)
        return audio_bytes

    def _make_messages(
        self,
        messages: list[dict[str, Any]],
        *,
        text: str,
        audio: bytes,
        keep_text: bool,
    ) -> list[dict[str, Any]]:
        """CONTRACT §3.7 冻结签名：把回复消息组改写成「原消息（可去文字）+ 语音段」。"""

        audio_bytes = audio_module.ensure_compatible(audio, self.config.response_format)
        return build_messages(
            messages,
            text=text,
            audio_base64=audio_module.to_base64(audio_bytes),
            keep_text=keep_text,
        )

    # ── 回复扩展 ──────────────────────────────────────────────
    @ReplyExtension(
        VOICE_EXTENSION_NAME,
        description="需要语音回复时启用：把本次回复合成语音，可选择是否保留文字",
        parameters=extension_parameters(),
        platforms="qq",
        timeout_ms=DEFAULT_EXTENSION_TIMEOUT_MS,
    )
    async def reply_voice(
        self,
        *,
        phase: str,
        parameters: dict[str, Any],
        text: str,
        messages: list[dict[str, Any]],
        **context: Any,
    ) -> dict[str, Any]:
        """``prepare`` 注入朗读要求；``before_send`` 把回复改写成语音消息。"""

        try:
            if not self._voice_allowed(context, phase=phase):
                return {}
            if not self._extension_active(parameters):
                return {}
            if phase == "prepare":
                return prepare_result()
            if phase != "before_send":
                return {}
            return await self._build_voice_reply(parameters, text, messages)
        except Exception as exc:  # noqa: BLE001 - 默认必须降级，绝不拖垮整次 reply
            if self._should_raise_on_error():
                self._logger().error(
                    "语音回复失败（fallback_on_error=fail，联调模式）：%s",
                    exc,
                    exc_info=True,
                )
                raise
            self._log_extension_failure(exc, phase)
            return {}

    def _voice_allowed(self, context: dict[str, Any], *, phase: str = "before_send") -> bool:
        """按群/私聊开关策略决定本次是否发声；任何异常一律按默认白名单拒绝。

        同一次 reply 的 ``prepare`` 与 ``before_send`` 都会调用本方法，但只在
        ``before_send`` 阶段打拒绝日志，避免一条回复刷两条。
        """

        try:
            chat = context.get("chat") if isinstance(context, dict) else None
            cfg = self.config
            allowed = is_chat_allowed(
                chat,
                group_policy=cfg.group_policy,
                group_list=cfg.group_list,
                private_policy=cfg.private_policy,
                private_list=cfg.private_list,
            )
            if not allowed and phase == "before_send":
                self._log_policy_denied(chat, cfg)
            return allowed
        except Exception as exc:  # noqa: BLE001 - 判定失败必须降级为不发声
            self._logger().warning("语音开关判定失败，按默认白名单拒绝：%s", exc)
            return False

    def _log_policy_denied(self, chat: Any, cfg: Any) -> None:
        """拒绝时打一条可定位的 INFO；日志自身异常绝不影响主链路。"""

        try:
            resolved = resolve_chat_peer(chat)
            if resolved is None:
                self._logger().info("语音开关拒绝: chat 中缺少 is_group_chat/群号/QQ号")
                return
            is_group, peer = resolved
            if is_group:
                policy = cfg.group_policy
                ids = parse_peer_list(cfg.group_list)
            else:
                policy = cfg.private_policy
                ids = parse_peer_list(cfg.private_list)
            self._logger().info(
                "语音开关拒绝: is_group=%s peer=%s policy=%s hit=%s",
                is_group,
                peer,
                policy,
                peer in ids,
            )
        except Exception:  # noqa: BLE001 - 日志失败不能拖垮回复
            pass

    def _extension_active(self, parameters: Any) -> bool:
        """扩展是否应该参与本次回复：参数开关、运行开关与 API Key 三重判断。"""

        if not isinstance(parameters, dict):
            parameters = {}
        if parameters.get(ATTACH_VOICE_KEY, True) is False:
            return False
        if not self.is_enabled():
            return False
        try:
            cfg = self.config
        except RuntimeError:
            return False
        return bool(cfg.effective_api_key)

    async def _build_voice_reply(
        self,
        parameters: dict[str, Any],
        text: str,
        messages: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if not isinstance(parameters, dict):
            parameters = {}
        if not isinstance(messages, list) or not messages:
            return {}

        cfg = self.config
        try:
            spoken, audio_bytes = await self.synthesize_with_spoken(text)
        except EmptySpeechText:
            self._logger().info("清洗后文本为空，跳过语音（保留原消息）")
            return {}

        keep_text = bool(parameters.get(KEEP_TEXT_KEY, cfg.keep_text))
        built = self._make_messages(messages, text=spoken, audio=audio_bytes, keep_text=keep_text)
        self._logger().info(
            "语音回复已生成：%s 字 / %s 字节（keep_text=%s）",
            len(spoken),
            len(audio_bytes),
            keep_text,
        )
        return {"messages": built}

    def _should_raise_on_error(self) -> bool:
        try:
            return self.config.fallback_on_error == "fail"
        except RuntimeError:
            return False

    def _log_extension_failure(self, exc: Exception, phase: str) -> None:
        logger = self._logger()
        if isinstance(exc, (ProviderError, EmptySpeechText, ValueError)):
            logger.warning("语音回复扩展 %s 阶段失败，已降级为纯文字：%s", phase, exc)
        else:
            logger.exception("语音回复扩展 %s 阶段出现未预期异常，已降级为纯文字", phase)

    # ── 发送辅助（只走已声明的 send.hybrid）─────────────────────
    async def _send_text_via_hybrid(self, message: str, stream_id: str) -> bool:
        result = await self.ctx.send.hybrid([{"type": "text", "content": message}], stream_id)
        return bool(result)

    async def _send_voice_via_hybrid(self, segments: list[dict[str, Any]], stream_id: str) -> bool:
        result = await self.ctx.send.hybrid(segments, stream_id)
        return bool(result)

    def _config_summary(self) -> dict[str, Any]:
        """``/tts status`` 用的状态摘要（有序，便于直接渲染）。"""

        cfg = self.config
        try:
            fmt = audio_module.normalize_format(cfg.response_format)
            rate = audio_module.resolve_sample_rate(fmt, cfg.sample_rate)
        except ValueError:
            fmt, rate = str(cfg.response_format), cfg.sample_rate

        cache_stats: dict[str, Any] = {}
        if self.audio_cache is not None:
            try:
                cache_stats = dict(self.audio_cache.stats())
            except Exception:  # noqa: BLE001 - 统计失败不影响状态展示
                cache_stats = {}

        cache_line = f"{'开' if cfg.cache_enabled else '关'}；TTL {cfg.cache_ttl_hours}h；上限 {cfg.cache_max_mb}MB"
        if cache_stats:
            cache_line += f"；现有 {cache_stats.get('count', 0)} 个 / {cache_stats.get('bytes', 0)} 字节"

        return {
            "运行开关": "开启" if self.is_enabled() else "关闭",
            "配置总开关": "开启" if cfg.enabled else "关闭",
            "API Key": "已配置" if cfg.effective_api_key else f"未配置（可设环境变量 {API_KEY_ENV_VAR}）",
            "模型": cfg.model,
            "音色": cfg.effective_voice or "(未配置)",
            "格式 / 采样率": f"{fmt} / {rate} Hz",
            "语速 / 增益": f"{cfg.speed} / {cfg.gain:+.1f} dB",
            "单次文本上限": f"{cfg.max_chars} 字",
            "回复超时": f"{cfg.timeout_ms} ms（provider 实际 {self._provider_timeout():.0f} s）",
            "重试": f"{cfg.retry} 次（仅 429/5xx）",
            "缓存": cache_line,
            "失败策略": "降级为纯文字" if cfg.fallback_on_error == "text" else "让本次回复失败（联调）",
            "auto_voice": "开（当前版本未接入 Hook）" if cfg.auto_voice else "关",
            "语音开关": "\n"
            + commands_module.format_policy_summary(
                group_policy=cfg.group_policy,
                group_list=cfg.group_list,
                private_policy=cfg.private_policy,
                private_list=cfg.private_list,
            ),
        }

    # ── /tts 命令组 ────────────────────────────────────────────
    @staticmethod
    def _matched(kwargs: dict[str, Any], key: str) -> str:
        groups = kwargs.get("matched_groups")
        if not isinstance(groups, dict):
            return ""
        value = groups.get(key)
        return str(value) if value else ""

    @Command(
        "tts_test",
        description="合成并发送一条测试语音（确定性验证配置→合成→发送链路）",
        pattern=commands_module.PATTERN_TEST,
    )
    async def handle_tts_test(self, **kwargs: Any) -> tuple[bool, str, int]:
        return await commands_module.handle_test(
            stream_id=str(kwargs.get("stream_id", "") or ""),
            requested_text=self._matched(kwargs, "text"),
            synthesize=self.synthesize_with_spoken,
            send_hybrid=self._send_voice_via_hybrid,
            send_text=self._send_text_via_hybrid,
            enabled=self.is_enabled(),
        )

    @Command("tts_voices", description="列出账号下可用音色（名称 — uri）", pattern=commands_module.PATTERN_VOICES)
    async def handle_tts_voices(self, **kwargs: Any) -> tuple[bool, str, int]:
        return await commands_module.handle_voice_list(
            stream_id=str(kwargs.get("stream_id", "") or ""),
            provider=self.provider,
            timeout=self._provider_timeout(),
            send_text=self._send_text_via_hybrid,
        )

    @Command(
        "tts_clone",
        description="用 voices/ 目录下的参考音频克隆音色（operator）",
        pattern=commands_module.PATTERN_CLONE,
        permission="operator",
    )
    async def handle_tts_clone(self, **kwargs: Any) -> tuple[bool, str, int]:
        return await commands_module.handle_clone(
            stream_id=str(kwargs.get("stream_id", "") or ""),
            name=self._matched(kwargs, "name"),
            filename=self._matched(kwargs, "filename"),
            reference_text=self._matched(kwargs, "reference_text"),
            voice_dir=self.voice_dir,
            provider=self.provider,
            timeout=self._provider_timeout(),
            send_text=self._send_text_via_hybrid,
        )

    @Command(
        "tts_voice_list",
        description="列出已克隆音色（与 /tts voices 同义）",
        pattern=commands_module.PATTERN_VOICE_LIST,
    )
    async def handle_tts_voice_list(self, **kwargs: Any) -> tuple[bool, str, int]:
        return await commands_module.handle_voice_list(
            stream_id=str(kwargs.get("stream_id", "") or ""),
            provider=self.provider,
            timeout=self._provider_timeout(),
            send_text=self._send_text_via_hybrid,
        )

    @Command(
        "tts_voice_del",
        description="删除克隆音色（名称或 uri，operator）",
        pattern=commands_module.PATTERN_VOICE_DEL,
        permission="operator",
    )
    async def handle_tts_voice_del(self, **kwargs: Any) -> tuple[bool, str, int]:
        return await self._delete_voice(kwargs)

    @Command(
        "tts_voice_rm",
        description="删除克隆音色（CONTRACT §3.9 的 rm 写法，operator）",
        pattern=commands_module.PATTERN_VOICE_RM,
        permission="operator",
    )
    async def handle_tts_voice_rm(self, **kwargs: Any) -> tuple[bool, str, int]:
        return await self._delete_voice(kwargs)

    async def _delete_voice(self, kwargs: dict[str, Any]) -> tuple[bool, str, int]:
        return await commands_module.handle_voice_delete(
            stream_id=str(kwargs.get("stream_id", "") or ""),
            target=self._matched(kwargs, "target"),
            provider=self.provider,
            timeout=self._provider_timeout(),
            send_text=self._send_text_via_hybrid,
        )

    @Command("tts_status", description="查看硅基流动 TTS 配置与运行状态", pattern=commands_module.PATTERN_STATUS)
    async def handle_tts_status(self, **kwargs: Any) -> tuple[bool, str, int]:
        return await commands_module.handle_status(
            stream_id=str(kwargs.get("stream_id", "") or ""),
            summary=self._config_summary(),
            send_text=self._send_text_via_hybrid,
        )

    @Command(
        "tts_on",
        description="开启语音合成（运行期开关）",
        pattern=commands_module.PATTERN_ON,
        permission="operator",
    )
    async def handle_tts_on(self, **kwargs: Any) -> tuple[bool, str, int]:
        return await self._switch(kwargs, "on")

    @Command(
        "tts_off",
        description="关闭语音合成（运行期开关）",
        pattern=commands_module.PATTERN_OFF,
        permission="operator",
    )
    async def handle_tts_off(self, **kwargs: Any) -> tuple[bool, str, int]:
        return await self._switch(kwargs, "off")

    @Command("tts_help", description="显示 /tts 命令帮助", pattern=commands_module.PATTERN_HELP)
    async def handle_tts_help(self, **kwargs: Any) -> tuple[bool, str, int]:
        return await commands_module.handle_help(
            stream_id=str(kwargs.get("stream_id", "") or ""),
            send_text=self._send_text_via_hybrid,
        )

    async def _switch(self, kwargs: dict[str, Any], action: str) -> tuple[bool, str, int]:
        if self.runtime_state is None:
            self.runtime_state = commands_module.RuntimeState.load(self.ctx.paths.data_dir)
        return await commands_module.handle_switch(
            stream_id=str(kwargs.get("stream_id", "") or ""),
            action=action,
            state=self.runtime_state,
            send_text=self._send_text_via_hybrid,
        )


async def _to_thread(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """把同步缓存 IO 放到线程池，避免阻塞事件循环（CONTRACT §5.2）。"""

    return await asyncio.to_thread(func, *args, **kwargs)


def create_plugin() -> SiliconFlowTTSPlugin:
    """Runner 入口：必须返回已实例化的插件对象。"""

    return SiliconFlowTTSPlugin()
