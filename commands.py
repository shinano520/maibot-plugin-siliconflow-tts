"""``/tts`` 命令组的纯逻辑（不导入 ``maibot_sdk``，便于单测与本地校验）。

分工：
- ``plugin.py`` 用 ``@Command`` 声明命令并注入能力回调；
- 本模块只负责参数解析、状态落盘、消息文案与错误收敛。

为什么文本反馈也走 ``send.hybrid``：本插件 manifest 只声明了
``send.hybrid`` 与 ``config.get`` 两项能力，Host 会按 manifest 精确授权，
未声明的 ``send.text`` 会被拒绝（``未获授权能力: send.text``），
因此所有出站消息统一用 ``send.hybrid``（单段 ``text``）。
"""

from __future__ import annotations

import json
import os
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .audio import to_base64

#: 发送回调签名：(文本, stream_id) -> 是否成功
SendText = Callable[[str, str], Awaitable[bool]]

#: 混合消息回调签名：(消息段列表, stream_id) -> 是否成功
SendHybrid = Callable[[list[dict[str, Any]], str], Awaitable[bool]]

#: 合成回调签名：(文本) -> (实际朗读文本, 音频字节)
Synthesize = Callable[[str], Awaitable[tuple[str, bytes]]]

#: 音色列表回调签名：() -> 音色 uri 列表
ListVoices = Callable[[], Awaitable[Sequence[str]]]

# ── 命令正则（整条消息匹配，务必带 ^ 与 $）────────────────────────────
PATTERN_TEST = r"^/tts\s+test(?:\s+(?P<text>.+))?$"
PATTERN_VOICES = r"^/tts\s+voices$"
PATTERN_ON = r"^/tts\s+on$"
PATTERN_OFF = r"^/tts\s+off$"
PATTERN_STATUS = r"^/tts\s+status$"
PATTERN_HELP = r"^/tts(?:\s+help)?$"

#: ``/tts test`` 不带参数时使用的探针文本
DEFAULT_TEST_TEXT = "你好，我是麦麦，这是一条语音链路测试。"

#: 帮助文案
HELP_TEXT = "\n".join(
    (
        "🎙️ 硅基流动 TTS 命令：",
        "/tts test [文本] — 合成一条语音并直接发送（不走 Planner）",
        "/tts voices — 列出当前账号可用音色",
        "/tts status — 查看配置与运行状态",
        "/tts on — 开启语音合成",
        "/tts off — 关闭语音合成（回复保持纯文字）",
        "/tts help — 显示本帮助",
    )
)

#: 状态文件（落在 ``ctx.paths.data_dir``，命令不写配置）
STATE_FILE_NAME = "state.json"

#: ``/tts voices`` 最多展示多少条
MAX_VOICE_LINES = 30


@dataclass
class RuntimeState:
    """``/tts on|off`` 的运行期开关，持久化到数据目录。

    配置里的 ``enabled`` 仍是总开关：本状态只做「运行期覆盖」，
    未设置时以配置值为准。小文件同步读写（几十字节），不阻塞事件循环。
    """

    path: Path
    enabled_override: bool | None = None

    @classmethod
    def load(cls, data_dir: str | Path) -> "RuntimeState":
        """从 ``data_dir/state.json`` 读取运行期状态；文件损坏时按未设置处理。"""

        path = Path(data_dir) / STATE_FILE_NAME
        override: bool | None = None
        try:
            if path.is_file():
                raw = json.loads(path.read_text(encoding="utf-8"))
                value = raw.get("enabled_override") if isinstance(raw, dict) else None
                if isinstance(value, bool):
                    override = value
        except (OSError, ValueError):
            override = None
        return cls(path=path, enabled_override=override)

    def is_enabled(self, default: bool) -> bool:
        """返回当前是否启用：有运行期覆盖时以覆盖值为准。"""

        if self.enabled_override is None:
            return bool(default)
        return bool(self.enabled_override)

    def set_enabled(self, enabled: bool) -> None:
        """写入运行期开关（原子替换，避免半截文件）。"""

        self.enabled_override = bool(enabled)
        payload = {
            "enabled_override": bool(enabled),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f"{self.path.name}.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)

    def clear_override(self) -> None:
        """删除运行期覆盖，回到配置值。"""

        self.enabled_override = None
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass


async def _safe_send_text(send_text: SendText, message: str, stream_id: str) -> bool:
    """发送文本反馈；发送失败只吞掉异常并返回 ``False``，绝不让命令抛错。"""

    if not stream_id:
        return False
    try:
        return bool(await send_text(message, stream_id))
    except Exception:  # noqa: BLE001 - 命令反馈失败不能影响主流程
        return False


async def handle_test(
    *,
    stream_id: str,
    requested_text: str,
    synthesize: Synthesize,
    send_hybrid: SendHybrid,
    send_text: SendText,
    enabled: bool,
) -> tuple[bool, str, int]:
    """``/tts test``：text → 合成 → Base64 → ``send.hybrid`` 直发语音。

    这是确定性链路验证：不经过 Planner、不依赖回复扩展参数。
    """

    text = (requested_text or "").strip() or DEFAULT_TEST_TEXT
    if not enabled:
        message = "⚠️ 语音合成当前处于关闭状态：先发 /tts on，或把配置 enabled 打开。"
        await _safe_send_text(send_text, message, stream_id)
        return False, message, 1

    try:
        spoken, audio_bytes = await synthesize(text)
    except Exception as exc:  # noqa: BLE001 - 命令必须返回三元组而不是抛异常
        message = f"❌ 语音合成失败：{exc}"
        await _safe_send_text(send_text, message, stream_id)
        return False, message, 1

    if not audio_bytes:
        message = "❌ 语音合成返回空音频，已中止发送。"
        await _safe_send_text(send_text, message, stream_id)
        return False, message, 1

    base64_audio = to_base64(audio_bytes)
    # Host 的 _normalize_plugin_segment 会把 voice 段的 content 归一化为
    # binary_data_base64；额外带 data 让 Host 侧记录朗读文本。
    segments: list[dict[str, Any]] = [
        {"type": "voice", "content": base64_audio, "data": spoken},
    ]

    failure = ""
    sent = False
    try:
        sent = bool(await send_hybrid(segments, stream_id))
    except Exception as exc:  # noqa: BLE001 - 适配器不支持 record 时给出可读提示
        failure = str(exc)

    if sent:
        message = f"✅ 语音已发送（{len(audio_bytes)} 字节 / {len(spoken)} 字）"
        return True, message, 1

    message = (
        f"⚠️ 语音合成成功（{len(audio_bytes)} 字节），但发送失败："
        f"{failure or '适配器未返回成功'}\n"
        "请检查适配器是否支持 record 段，或改用普通回复触发 voice 扩展。"
    )
    await _safe_send_text(send_text, message, stream_id)
    return False, message, 1


async def handle_voices(
    *,
    stream_id: str,
    list_voices: ListVoices,
    send_text: SendText,
) -> tuple[bool, str, int]:
    """``/tts voices``：列出硅基流动账号可用音色。"""

    try:
        voices = [str(voice) for voice in await list_voices()]
    except Exception as exc:  # noqa: BLE001 - 失败时提示配置问题
        message = f"⚠️ 读取音色列表失败：{exc}\n请检查 api_key、base_url 与网络后重试。"
        await _safe_send_text(send_text, message, stream_id)
        return False, message, 1

    if not voices:
        message = "⚠️ 没有取到可用音色：请确认 api_key 已配置，且模型支持音色列表接口。"
        await _safe_send_text(send_text, message, stream_id)
        return False, message, 1

    lines = [f"🎙️ 可用音色（共 {len(voices)} 个）："]
    for index, voice in enumerate(voices[:MAX_VOICE_LINES], start=1):
        lines.append(f"{index}. {voice}")
    if len(voices) > MAX_VOICE_LINES:
        lines.append(f"…其余 {len(voices) - MAX_VOICE_LINES} 个已省略")
    lines.append("把音色填进配置 voice（支持 模型:音色 或 speech: 开头的动态音色）。")
    message = "\n".join(lines)
    await _safe_send_text(send_text, message, stream_id)
    return True, message, 1


async def handle_switch(
    *,
    stream_id: str,
    action: str,
    state: RuntimeState,
    send_text: SendText,
) -> tuple[bool, str, int]:
    """``/tts on|off``：写入数据目录下的运行期开关。"""

    enabled = action == "on"
    try:
        state.set_enabled(enabled)
    except OSError as exc:
        message = f"❌ 保存开关状态失败：{exc}"
        await _safe_send_text(send_text, message, stream_id)
        return False, message, 1

    message = (
        "✅ 已开启语音合成：回复扩展下次被选中时会合成语音。"
        if enabled
        else "⏸️ 已关闭语音合成：回复保持纯文字（配置里的 enabled 总开关不受影响）。"
    )
    await _safe_send_text(send_text, message, stream_id)
    return True, message, 1


def render_status(summary: Mapping[str, Any]) -> str:
    """把状态摘要渲染成多行文本（纯函数，便于单测）。"""

    lines = ["🎙️ 硅基流动 TTS 状态："]
    for label, value in summary.items():
        lines.append(f"- {label}：{value}")
    return "\n".join(lines)


async def handle_status(
    *,
    stream_id: str,
    summary: Mapping[str, Any],
    send_text: SendText,
) -> tuple[bool, str, int]:
    """``/tts status``：展示开关、模型、音色、缓存与降级策略。"""

    message = render_status(summary)
    await _safe_send_text(send_text, message, stream_id)
    return True, message, 1


async def handle_help(*, stream_id: str, send_text: SendText) -> tuple[bool, str, int]:
    """``/tts`` 或 ``/tts help``：显示命令帮助。"""

    await _safe_send_text(send_text, HELP_TEXT, stream_id)
    return True, HELP_TEXT, 1
