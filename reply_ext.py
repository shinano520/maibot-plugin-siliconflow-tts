"""``voice`` 回复扩展的纯逻辑（不导入 ``maibot_sdk``，便于单测与本地校验）。

装饰器声明在 ``plugin.py``；本模块只负责：

- 组件名、参数名与 ``prepare`` 阶段注入的朗读要求常量；
- 参数 Schema 构造；
- 把 ``before_send`` 阶段的结果消息组装配成 Host 强校验能通过的形状。
"""

from __future__ import annotations

import re
from typing import Any

#: 回复扩展组件名；Host 会拼成 ``local.siliconflow-tts.voice``
VOICE_EXTENSION_NAME: str = "voice"

#: 群/私聊开关策略：all=全部开启，whitelist=仅名单内，blacklist=名单外才开启
POLICY_ALL: str = "all"
POLICY_WHITELIST: str = "whitelist"
POLICY_BLACKLIST: str = "blacklist"

#: 允许写成 ``qq:123`` / ``QQ：123`` 的前缀
_QQ_PREFIX_PATTERN = re.compile(r"(?i)^\s*qq\s*[:：]\s*")
#: 名单项内的分隔符（逗号、分号、任意空白）
_ID_SEPARATOR_PATTERN = re.compile(r"[\s,，;；]+")

#: ``prepare`` 阶段注入的朗读要求（CONTRACT §3.6 冻结常量）
PREPARE_EXTRA_PROMPT: str = (
    "这次回复会转成语音朗读，请用适合朗读的自然口语，不要用 Markdown、列表、代码块、颜文字和括号动作。"
)

#: 扩展参数名（Host 按此键校验并补齐默认值）
ATTACH_VOICE_KEY: str = "attach_voice"
KEEP_TEXT_KEY: str = "keep_text"

#: 消息段类型
VOICE_SEGMENT_TYPE: str = "voice"
TEXT_SEGMENT_TYPE: str = "text"

#: Host 对 ``voice`` 段唯一认可的音频字段名（适配器 ``segment_encoder.py`` 只读这个键）
VOICE_BASE64_KEY: str = "binary_data_base64"


def normalize_peer_id(value: Any) -> str:
    """把群号/QQ 号归一化成纯数字串（容忍 ``qq:`` 前缀、全角数字与空白）。

    - ``None`` / 空串 / 全空白 → ``""``；
    - 去掉 ``qq:`` / ``QQ：`` 前缀；
    - 全角数字（``１２３``）转半角；
    - 去掉内部空白（含列表项里手写的 ``123 456``）。
    """

    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    text = _QQ_PREFIX_PATTERN.sub("", text)
    text = "".join(
        chr(ord(char) - 0xFEE0) if "\uff10" <= char <= "\uff19" else char
        for char in text
    )
    return re.sub(r"\s+", "", text)


def parse_peer_list(values: Any) -> set[str]:
    """把配置里的名单归一化成集合（支持一行多个、逗号/分号/空白分隔）。"""

    if values is None:
        return set()
    if isinstance(values, str):
        items: list[Any] = [values]
    elif isinstance(values, (list, tuple, set)):
        items = list(values)
    else:
        items = [values]

    result: set[str] = set()
    for item in items:
        for part in _ID_SEPARATOR_PATTERN.split(str(item or "")):
            normalized = normalize_peer_id(part)
            if normalized:
                result.add(normalized)
    return result


def resolve_chat_peer(chat: Any) -> tuple[bool, str] | None:
    """从 ``context["chat"]`` 解出 ``(是否群聊, 对端号码)``；拿不到返回 ``None``。

    Host 的 ``chat`` 为 ``asdict(ToolAvailabilityContext)``，键固定为
    ``session_id/stream_id/is_group_chat/group_id/user_id/platform``。
    ``is_group_chat`` 缺失/非布尔 → 不猜，直接判定失败（默认白名单拒绝）。
    """

    if not isinstance(chat, dict):
        return None

    flag = chat.get("is_group_chat")
    if isinstance(flag, bool):
        is_group = flag
    elif isinstance(flag, int) and flag in (0, 1):
        is_group = bool(flag)
    else:
        return None

    raw_peer = chat.get("group_id") if is_group else chat.get("user_id")
    peer = normalize_peer_id(raw_peer)
    if not peer:
        return None
    return is_group, peer


def is_allowed(
    *,
    is_group: bool,
    peer_id: Any,
    group_policy: Any,
    group_list: Any,
    private_policy: Any,
    private_list: Any,
) -> bool:
    """按群/私聊各自的三态策略判定是否允许发声。

    - ``all`` → 允许；
    - ``whitelist`` → 仅在名单内才允许；
    - ``blacklist`` → 名单外才允许；
    - 策略未知/为空 → 拒绝（默认白名单语义）。
    """

    peer = normalize_peer_id(peer_id)
    if not peer:
        return False

    policy = str((group_policy if is_group else private_policy) or "").strip().lower()
    ids = parse_peer_list(group_list if is_group else private_list)

    if policy == POLICY_ALL:
        return True
    if policy == POLICY_WHITELIST:
        return peer in ids
    if policy == POLICY_BLACKLIST:
        return peer not in ids
    return False


def is_chat_allowed(
    chat: Any,
    *,
    group_policy: Any,
    group_list: Any,
    private_policy: Any,
    private_list: Any,
) -> bool:
    """``context["chat"]`` 入口：解不出对端或策略异常时一律拒绝。"""

    resolved = resolve_chat_peer(chat)
    if resolved is None:
        return False
    is_group, peer = resolved
    return is_allowed(
        is_group=is_group,
        peer_id=peer,
        group_policy=group_policy,
        group_list=group_list,
        private_policy=private_policy,
        private_list=private_list,
    )


def extension_parameters() -> dict[str, dict[str, object]]:
    """返回 ``@ReplyExtension(parameters=...)`` 使用的属性字典（每次调用返回新对象）。"""

    return {
        ATTACH_VOICE_KEY: {
            "type": "boolean",
            "default": True,
            "description": "是否合成语音",
        },
        KEEP_TEXT_KEY: {
            "type": "boolean",
            "default": True,
            "description": "是否同时保留文字",
        },
    }


def prepare_result() -> dict[str, str]:
    """``prepare`` 阶段的返回载荷。"""

    return {"extra_prompt": PREPARE_EXTRA_PROMPT}


def build_voice_message(
    *,
    text: str,
    audio_base64: str,
    quote_previous: bool = False,
) -> dict[str, Any]:
    """构造一条只含语音段的消息（Host 要求恰好两个键）。"""

    return {
        "segments": [
            {
                "type": VOICE_SEGMENT_TYPE,
                "data": str(text),
                VOICE_BASE64_KEY: str(audio_base64),
            }
        ],
        "quote_previous": bool(quote_previous),
    }


def drop_text_segments(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """去掉所有 ``text`` 段；去空了整条丢弃（Host 不接受空 ``segments``）。"""

    result: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        raw_segments = message.get("segments")
        if not isinstance(raw_segments, list):
            continue
        segments = [
            dict(segment)
            for segment in raw_segments
            if isinstance(segment, dict) and segment.get("type") != TEXT_SEGMENT_TYPE
        ]
        if not segments:
            continue
        result.append(
            {
                "segments": segments,
                "quote_previous": bool(message.get("quote_previous", False)),
            }
        )
    return result


def build_messages(
    messages: list[dict[str, Any]],
    *,
    text: str,
    audio_base64: str,
    keep_text: bool,
    quote_previous: bool = False,
) -> list[dict[str, Any]]:
    """按 ``keep_text`` 改写原消息组，并把语音消息追加到最后。

    - ``keep_text=True``：保留每条消息原有的段（含顺序）与 ``quote_previous``；
    - ``keep_text=False``：去掉 ``text`` 段，去掉后为空的消息整条丢弃；
    - 语音消息固定 ``quote_previous=False``，``data`` 为实际送 TTS 的文本。
    """

    source = messages if isinstance(messages, list) else []
    if keep_text:
        result: list[dict[str, Any]] = []
        for message in source:
            if not isinstance(message, dict):
                continue
            raw_segments = message.get("segments")
            if not isinstance(raw_segments, list):
                continue
            segments = [dict(segment) for segment in raw_segments if isinstance(segment, dict)]
            if not segments:
                # Host 不接受空 segments，整条丢弃
                continue
            result.append(
                {
                    "segments": segments,
                    "quote_previous": bool(message.get("quote_previous", False)),
                }
            )
    else:
        result = drop_text_segments(source)

    result.append(
        build_voice_message(
            text=text,
            audio_base64=audio_base64,
            quote_previous=quote_previous,
        )
    )
    return result
