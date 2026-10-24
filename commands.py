"""``/tts`` 命令组的纯逻辑（不导入 ``maibot_sdk``，便于单测与本地校验）。

分工：
- ``plugin.py`` 用 ``@Command`` 声明命令并注入能力回调；
- 本模块负责参数解析、状态落盘、消息文案与错误收敛；
- 音色管理直接调用 ``voice.*``（CONTRACT §3.6），``voice`` 延迟导入，
  这样 ``voice.py`` 尚未落地的安装包不会连带把整个插件拖挂。

为什么文本反馈也走 ``send.hybrid``：本插件 manifest 只声明了
``send.hybrid`` 与 ``config.get`` 两项能力，Host 会按 manifest 精确授权，
未声明的 ``send.text`` 会被拒绝（``未获授权能力: send.text``），
因此所有出站消息统一用 ``send.hybrid``（单段 ``text``）。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Awaitable, Callable, Mapping
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

# ── 命令正则（整条消息匹配，务必带 ^ 与 $）────────────────────────────
PATTERN_TEST = r"^/tts\s+test(?:\s+(?P<text>.+))?$"
PATTERN_VOICES = r"^/tts\s+voices$"
PATTERN_CLONE = r"^/tts\s+clone\s+(?P<name>\S+)\s+(?P<filename>\S+)\s+(?P<reference_text>.+)$"
PATTERN_VOICE_LIST = r"^/tts\s+voice\s+list$"
PATTERN_VOICE_DEL = r"^/tts\s+voice\s+del\s+(?P<target>\S+)$"
PATTERN_VOICE_RM = r"^/tts\s+voice\s+rm\s+(?P<target>\S+)$"
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
        "/tts voices — 列出账号下可用音色（名称 — uri）",
        "/tts clone <名称> <文件名> <参考文本> — 用 voices/ 目录下的参考音频克隆音色",
        "/tts voice list — 同上，列出音色",
        "/tts voice del <名称|uri> — 删除克隆音色",
        "/tts status — 查看配置与运行状态",
        "/tts on — 开启语音合成",
        "/tts off — 关闭语音合成（回复保持纯文字）",
        "/tts help — 显示本帮助",
    )
)

#: 参考音频的硬规则（与 CONTRACT §3.6 对齐）
REFERENCE_RULES = (
    "参考音频要求：单层文件名（不带路径/空格）、后缀 .mp3/.wav/.pcm/.opus、≤10MB，"
    "建议 8–10 秒且 <30 秒，内容与参考文本逐字对应。"
)

#: 状态文件（落在 ``ctx.paths.data_dir``，命令不写配置）
STATE_FILE_NAME = "state.json"

#: 音色列表最多展示多少条（CONTRACT §3.9）
MAX_VOICE_LINES = 20

#: ``voice.py`` 未就绪时的兜底规则（正常路径从 voice 模块读取）
_FALLBACK_SUFFIXES: tuple[str, ...] = (".mp3", ".wav", ".pcm", ".opus")
_FALLBACK_MAX_BYTES = 10 * 1024 * 1024
_FALLBACK_NAME_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"

#: 密钥脱敏（CONTRACT §12）：``sk-`` + 8 位以上字母数字
_SECRET_PATTERN = re.compile(r"sk-[A-Za-z0-9]{8,}")


class VoiceModuleUnavailable(RuntimeError):
    """``voice.py`` 尚未落地或导入失败：命令层据此给出可执行提示。"""


def redact_for_user(text: str) -> str:
    """命令回显统一脱敏：优先复用 ``provider.redact_secret``（CONTRACT §12）。"""

    try:
        from .provider import redact_secret
    except ImportError:  # 契约落地前的等价兜底
        return _SECRET_PATTERN.sub("sk-****", str(text))
    return redact_secret(str(text))


def _load_voice_module() -> Any:
    """延迟导入 ``voice.py``：voice 由 provider-dev 并行实现，缺失时转成可执行提示。"""

    try:
        from . import voice as voice_module  # noqa: PLC0415 - 刻意延迟，避免拖挂插件加载
    except ImportError as exc:
        raise VoiceModuleUnavailable(f"无法导入 voice.py：{exc}") from exc
    return voice_module


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


# ── 发送与错误收敛 ────────────────────────────────────────────────────
async def _safe_send_text(send_text: SendText, message: str, stream_id: str) -> bool:
    """发送文本反馈；发送失败只吞掉异常并返回 ``False``，绝不让命令抛错。"""

    if not stream_id:
        return False
    try:
        return bool(await send_text(message, stream_id))
    except Exception:  # noqa: BLE001 - 命令反馈失败不能影响主流程
        return False


async def _reply(
    send_text: SendText,
    stream_id: str,
    message: str,
    *,
    success: bool = True,
) -> tuple[bool, str, int]:
    """统一出口：先按契约 §12 脱敏，再发送，并以三元组返回给 Host。"""

    safe_message = redact_for_user(message)
    await _safe_send_text(send_text, safe_message, stream_id)
    return success, safe_message, 1


def _voice_unavailable_message(exc: Exception) -> str:
    return (
        "❌ 音色管理功能未就绪：插件包缺少 voice.py（或导入失败）。\n"
        "请更新到完整版本后重载插件；语音合成不受影响。\n"
        f"详情：{exc}"
    )


def missing_file_message(voice_dir: Path, filename: str) -> str:
    """参考音频不存在时的可自救文案：明确告诉用户放哪里。"""

    return "\n".join(
        (
            f"❌ 参考音频不存在：{filename}",
            f"请先把文件放到这个目录：{voice_dir}",
            f"然后重发：/tts clone <名称> {filename} <参考文本>",
            REFERENCE_RULES,
        )
    )


def provider_failure_message(exc: Exception, *, action: str) -> str:
    """把上游异常翻译成用户能自救的文案（不直接吐原始堆栈/请求头）。"""

    kind = str(getattr(exc, "kind", "") or "")
    status = getattr(exc, "status", None)
    detail = str(exc).strip()
    status_suffix = f"（HTTP {status}）" if status else ""

    if kind == "auth":
        advice = (
            f"Key 无效或账号未完成实名认证{status_suffix}：请更新 WebUI 里的 api_key，"
            "或到硅基流动控制台确认实名与权限后重试。"
        )
    elif kind == "rate_limit":
        advice = f"触发限流{status_suffix}：等 1 分钟再试，避免连续触发。"
    elif kind == "server":
        advice = f"硅基流动服务端异常{status_suffix}：稍后重试；持续失败可查看官方状态公告。"
    elif kind == "network":
        advice = "网络连接失败：检查 base_url、代理与出网权限后重试。"
    elif kind == "timeout":
        advice = "请求超时：网络较慢或参考音频较大，稍后重试。"
    elif kind == "bad_request":
        advice = f"服务端拒绝了请求（原文前 200 字）：{detail[:200] or '无返回内容'}"
    elif kind == "config":
        advice = "尚未配置 api_key：在 WebUI 填入，或设置环境变量 SILICONFLOW_API_KEY。"
    elif kind == "empty":
        advice = "服务端返回了空结果：稍后重试。"
    else:
        advice = f"错误详情：{detail[:200] or type(exc).__name__}"
    return f"❌ {action}失败：{advice}"


# ── 音色克隆 / 列表 / 删除（CONTRACT §3.6 + §3.9）──────────────────────
def validate_clone_request(
    voice_module: Any,
    voice_dir: Path,
    name: str,
    filename: str,
    reference_text: str,
) -> str:
    """克隆前的本地预检：返回空串表示通过，否则返回可执行的错误文案。"""

    if not name:
        return "❌ 缺少音色名称。用法：/tts clone <名称> <文件名> <参考文本>"
    name_pattern = str(getattr(voice_module, "VOICE_NAME_PATTERN", _FALLBACK_NAME_PATTERN))
    if re.fullmatch(name_pattern, name) is None:
        return "❌ 音色名称不合法：只能使用字母、数字、下划线或短横线（1–64 个字符），不要带空格或冒号。"

    if not filename:
        return "❌ 缺少参考音频文件名。用法：/tts clone <名称> <文件名> <参考文本>"

    if not reference_text:
        return (
            "❌ 参考文本必填：它必须与参考音频逐字对应，直接决定克隆质量。\n"
            "用法：/tts clone <名称> <文件名> <参考文本>"
        )

    if (
        filename.startswith(".")
        or "/" in filename
        or "\\" in filename
        or ".." in filename
        or Path(filename).is_absolute()
    ):
        return (
            "❌ 文件名非法：只能填 voices/ 目录下的单层文件名，不要带路径、不要 ..、不要以 . 开头。\n"
            f"参考音频目录：{voice_dir}"
        )

    suffix = Path(filename).suffix.lower()
    allowed = tuple(getattr(voice_module, "AUDIO_SUFFIXES", _FALLBACK_SUFFIXES))
    if suffix not in allowed:
        return (
            f"❌ 后缀 {suffix or '(无后缀)'} 不在白名单：仅支持 {' / '.join(allowed)}。\n"
            "请把音频转成 mp3 或 wav 后重试。"
        )

    path = Path(voice_dir) / filename
    if not path.exists():
        return missing_file_message(Path(voice_dir), filename)
    if not path.is_file():
        return f"❌ {filename} 不是普通文件：{voice_dir} 下只能放音频文件，不能放目录。"

    max_bytes = int(getattr(voice_module, "MAX_AUDIO_BYTES", _FALLBACK_MAX_BYTES))
    try:
        size = path.stat().st_size
    except OSError as exc:
        return f"❌ 读取文件信息失败：{exc}"
    if size == 0:
        return f"❌ 参考音频是空文件（0 字节）：请换一个有效的 {' / '.join(allowed)} 文件。"
    if size > max_bytes:
        return (
            f"❌ 参考音频 {size / 1024 / 1024:.1f}MB，超过上限 {max_bytes / 1024 / 1024:.0f}MB。\n"
            "请裁短到 8–10 秒或压缩后重试。"
        )
    return ""


async def handle_clone(
    *,
    stream_id: str,
    name: str,
    filename: str,
    reference_text: str,
    voice_dir: Path,
    provider: Any,
    timeout: float | None,
    send_text: SendText,
) -> tuple[bool, str, int]:
    """``/tts clone <名称> <文件名> <参考文本>``：读 voices/ 参考音频 → 克隆 → 回显 uri。"""

    voice_dir = Path(voice_dir)
    name = (name or "").strip()
    filename = (filename or "").strip()
    reference_text = (reference_text or "").strip()

    if provider is None:
        return await _reply(
            send_text,
            stream_id,
            "❌ TTS 客户端尚未初始化：插件可能还没加载完成，稍后重试或查看插件日志。",
            success=False,
        )

    try:
        voice_module = _load_voice_module()
    except VoiceModuleUnavailable as exc:
        return await _reply(send_text, stream_id, _voice_unavailable_message(exc), success=False)

    problem = validate_clone_request(voice_module, voice_dir, name, filename, reference_text)
    if problem:
        return await _reply(send_text, stream_id, problem, success=False)

    try:
        audio_bytes, mime = await asyncio.to_thread(voice_module.read_reference_audio, voice_dir, filename)
    except FileNotFoundError:
        return await _reply(send_text, stream_id, missing_file_message(voice_dir, filename), success=False)
    except ValueError as exc:
        return await _reply(
            send_text,
            stream_id,
            f"❌ 参考音频不合法：{exc}\n{REFERENCE_RULES}",
            success=False,
        )
    except OSError as exc:
        return await _reply(send_text, stream_id, f"❌ 读取参考音频失败：{exc}", success=False)

    try:
        profile = await voice_module.clone_voice(
            provider,
            name=name,
            reference_text=reference_text,
            audio_bytes=audio_bytes,
            mime=mime,
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001 - 命令只回文字，不向上抛
        return await _reply(send_text, stream_id, provider_failure_message(exc, action="克隆"), success=False)

    uri = str(getattr(profile, "uri", "") or "").strip()
    if not uri:
        return await _reply(
            send_text,
            stream_id,
            "❌ 克隆接口没有返回 uri：请重试；若持续出现，检查服务端返回格式是否变化。",
            success=False,
        )

    message = "\n".join(
        (
            f"✅ 克隆成功：{name}",
            f"uri：{uri}",
            "启用方式：把这串 uri 填进 WebUI 插件配置的 voice 字段（config.toml 的 [plugin].voice），"
            "保存后发 /tts status 确认当前音色。",
            "提示：参考音频建议 8–10 秒、<30 秒，且与参考文本逐字对应，否则听起来会不像。",
        )
    )
    return await _reply(send_text, stream_id, message)


async def handle_voice_list(
    *,
    stream_id: str,
    provider: Any,
    timeout: float | None,
    send_text: SendText,
    heading: str = "🎙️ 可用音色",
) -> tuple[bool, str, int]:
    """``/tts voices`` 与 ``/tts voice list``：列出克隆音色（名称 — uri，最多 20 条）。"""

    if provider is None:
        return await _reply(
            send_text,
            stream_id,
            "❌ TTS 客户端尚未初始化：插件可能还没加载完成，稍后重试或查看插件日志。",
            success=False,
        )

    try:
        voice_module = _load_voice_module()
    except VoiceModuleUnavailable as exc:
        return await _reply(send_text, stream_id, _voice_unavailable_message(exc), success=False)

    try:
        profiles = await voice_module.list_voices(provider, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - 命令只回文字，不向上抛
        return await _reply(send_text, stream_id, provider_failure_message(exc, action="读取音色列表"), success=False)

    ordered = sorted(profiles or [], key=lambda item: str(getattr(item, "name", "") or "").lower())
    if not ordered:
        return await _reply(
            send_text,
            stream_id,
            "\n".join(
                (
                    "⚠️ 还没有克隆音色。",
                    "系统音色：直接在配置 voice 里写「模型:音色名」（如 FunAudioLLM/CosyVoice2-0.5B:alex）。",
                    "克隆音色：把参考音频放进 voices/ 目录，然后 /tts clone <名称> <文件名> <参考文本>。",
                )
            ),
        )

    lines = [f"{heading}（共 {len(ordered)} 个）："]
    for profile in ordered[:MAX_VOICE_LINES]:
        profile_name = str(getattr(profile, "name", "") or "(未命名)")
        uri = str(getattr(profile, "uri", "") or "")
        lines.append(f"{profile_name} — {uri}")
    if len(ordered) > MAX_VOICE_LINES:
        lines.append(f"…其余 {len(ordered) - MAX_VOICE_LINES} 个已省略")
    lines.append("启用：把某个 uri 填进 WebUI 配置的 voice 字段，保存后 /tts status 确认。")
    return await _reply(send_text, stream_id, "\n".join(lines))


async def handle_voice_delete(
    *,
    stream_id: str,
    target: str,
    provider: Any,
    timeout: float | None,
    send_text: SendText,
) -> tuple[bool, str, int]:
    """``/tts voice del|rm <名称|uri>``：删除克隆音色，返回是否真的删掉。"""

    target = (target or "").strip()
    if not target:
        return await _reply(
            send_text,
            stream_id,
            "❌ 缺少要删除的音色。用法：/tts voice del <名称|uri>",
            success=False,
        )

    if provider is None:
        return await _reply(
            send_text,
            stream_id,
            "❌ TTS 客户端尚未初始化：插件可能还没加载完成，稍后重试或查看插件日志。",
            success=False,
        )

    try:
        voice_module = _load_voice_module()
    except VoiceModuleUnavailable as exc:
        return await _reply(send_text, stream_id, _voice_unavailable_message(exc), success=False)

    uri = target
    if ":" not in target:  # 传的是名称，先解析成 uri
        try:
            profiles = await voice_module.list_voices(provider, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - 命令只回文字，不向上抛
            return await _reply(
                send_text,
                stream_id,
                provider_failure_message(exc, action="查询音色列表"),
                success=False,
            )
        matched = [item for item in profiles or [] if str(getattr(item, "name", "") or "") == target]
        if not matched:
            return await _reply(
                send_text,
                stream_id,
                f"❌ 未找到名为「{target}」的音色。\n"
                "先发 /tts voice list 确认现有名称，或直接传 speech: 开头的 uri。",
                success=False,
            )
        uri = str(getattr(matched[0], "uri", "") or "")

    try:
        deleted = await voice_module.delete_voice(provider, uri=uri, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - 命令只回文字，不向上抛
        return await _reply(send_text, stream_id, provider_failure_message(exc, action="删除音色"), success=False)

    if not deleted:
        return await _reply(
            send_text,
            stream_id,
            "⚠️ 未找到该音色：可能已被删除，或这个 uri 不属于当前账号。\n"
            "发 /tts voice list 核对一下现有音色。",
            success=False,
        )

    return await _reply(
        send_text,
        stream_id,
        "\n".join(
            (
                f"✅ 已删除音色：{target}",
                "删除不可恢复，需要重新克隆才能再用。",
                "如果配置里的 voice 还指向这个 uri，请改回系统音色（如 FunAudioLLM/CosyVoice2-0.5B:alex），"
                "否则合成会失败。",
            )
        ),
    )


# ── 既有命令（test / switch / status / help）──────────────────────────
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
        return await _reply(
            send_text,
            stream_id,
            "⚠️ 语音合成当前处于关闭状态：先发 /tts on，或把配置 enabled 打开。",
            success=False,
        )

    try:
        spoken, audio_bytes = await synthesize(text)
    except Exception as exc:  # noqa: BLE001 - 命令必须返回三元组而不是抛异常
        return await _reply(send_text, stream_id, f"❌ 语音合成失败：{exc}", success=False)

    if not audio_bytes:
        return await _reply(send_text, stream_id, "❌ 语音合成返回空音频，已中止发送。", success=False)

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
        return await _reply(send_text, stream_id, f"✅ 语音已发送（{len(audio_bytes)} 字节 / {len(spoken)} 字）")

    return await _reply(
        send_text,
        stream_id,
        (
            f"⚠️ 语音合成成功（{len(audio_bytes)} 字节），但发送失败："
            f"{failure or '适配器未返回成功'}\n"
            "请检查适配器是否支持 record 段，或改用普通回复触发 voice 扩展。"
        ),
        success=False,
    )


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
        return await _reply(send_text, stream_id, f"❌ 保存开关状态失败：{exc}", success=False)

    message = (
        "✅ 已开启语音合成：回复扩展下次被选中时会合成语音。"
        if enabled
        else "⏸️ 已关闭语音合成：回复保持纯文字（配置里的 enabled 总开关不受影响）。"
    )
    return await _reply(send_text, stream_id, message)


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

    return await _reply(send_text, stream_id, render_status(summary))


async def handle_help(*, stream_id: str, send_text: SendText) -> tuple[bool, str, int]:
    """``/tts`` 或 ``/tts help``：显示命令帮助。"""

    return await _reply(send_text, stream_id, HELP_TEXT)
