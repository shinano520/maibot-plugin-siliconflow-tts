"""``voice`` 回复扩展的纯逻辑（不导入 ``maibot_sdk``，便于单测与本地校验）。

装饰器声明在 ``plugin.py``；本模块只负责：

- 组件名、参数名与 ``prepare`` 阶段注入的朗读要求常量；
- 参数 Schema 构造；
- 把 ``before_send`` 阶段的结果消息组装配成 Host 强校验能通过的形状。
"""

from __future__ import annotations

from typing import Any

#: 回复扩展组件名；Host 会拼成 ``local.siliconflow-tts.voice``
VOICE_EXTENSION_NAME: str = "voice"

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
