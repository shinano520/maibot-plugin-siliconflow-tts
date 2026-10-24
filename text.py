"""文本准备：把回复正文清洗成适合 TTS 朗读的纯文本。

公开签名（冻结，见 ``docs/CONTRACT.md`` §3.4）：

- ``prepare(text, max_chars) -> str``
- ``strip_markdown(text) -> str``
- ``strip_mentions(text) -> str``
- ``split_sentences(text) -> list[str]``
- ``truncate_sentences(text, max_chars) -> str``

约束：**不依赖 maibot_sdk**，不做网络、不做 IO，可离线单测。

清洗流水线（顺序冻结）：``strip_markdown`` → ``strip_mentions`` →
压缩空白 → ``truncate_sentences``。

空白策略：硅基流动官方要求 ``input`` 不要带空格。收尾阶段会把**中文语境**里的
空白全部压掉（汉字之间、汉字与中文标点之间）；英文单词之间的空格保留，
否则 "hello world" 会被读成一个词。纯中文输入的结果里不会残留任何空格。
"""

from __future__ import annotations

import re
import unicodedata

__all__ = [
    "ELLIPSIS",
    "prepare",
    "strip_markdown",
    "strip_mentions",
    "split_sentences",
    "truncate_sentences",
]

#: 截断时首段与末段之间的连接符（契约冻结）。
ELLIPSIS = "……"

# --------------------------------------------------------------------------- #
# 字符类
# --------------------------------------------------------------------------- #

#: 中日韩文字（含扩展 A 与兼容区）。
_CJK_CLASS = "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
#: 中文标点（含全角符号、破折号、省略号、间隔号）。
_CJK_PUNCT_CLASS = (
    "\u3000-\u303f"  # 。，、；：？！「」『』【】〜
    "\uff01-\uff65"  # ！＂＃…％，全角 ASCII 与半角片假名
    "\u2018-\u201f"  # ‘’“”
    "\u2026\u2014\u00b7\u30fb"
)
_CJK_ANY = _CJK_CLASS + _CJK_PUNCT_CLASS

#: 句末终止符（``.`` 另算，见 :func:`split_sentences`）。
_TERMINATORS = "。！？!?…\n"
#: 紧跟句末终止符、也属于上一句的收尾符号。
_TAIL_CHARS = "”’\"'」』）)】》〉>"

#: 颜文字 / 表情装饰用的符号区（成串出现时整段删除）。
_KAOMOJI_RUN_RE = re.compile(
    "[｡ﾟ︵╯╰╮╭ノつ∀ω・‿´`｀ᵕᴗヽゝ⌒ε゛゜丿彡]{2,}"
)
#: 常见 emoji / 装饰符号区（So、Sk 也由 :func:`_drop_symbol_chars` 处理）。
_EMOJI_RE = re.compile(
    "[\U0001f000-\U0001faff\u2600-\u27bf\u2b00-\u2bff\ufe0f\u200d]"
)

# ``prepare`` 收尾用：中文语境内的空格压到零。
_TIGHTEN_RE = re.compile(rf"(?<=[{_CJK_ANY}]) +(?=[{_CJK_ANY}])")
# 注意：``_CJK_CLASS`` 里的 ``xx-yy`` 只有放在正则字符类里才是区间，
# 用 ``"你" in _CJK_ANY`` 判断会永远为 False —— 所以单字符判断统一走这个正则。
_CJK_CHAR_RE = re.compile(rf"[{_CJK_ANY}]")

_SPACE_RUN_RE = re.compile(r"[ \t\u3000\x0b\f\xa0\u2000-\u200a]+")
_SPACE_AROUND_NEWLINE_RE = re.compile(r" *\n *")
_NEWLINE_RUN_RE = re.compile(r"\n{2,}")
_INVISIBLE_RE = re.compile("[\u200b-\u200f\u2060\ufeff]")

_URL_RE = re.compile(r"https?://[^\s\u3000<>()（）【】「」]+", re.IGNORECASE)
_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_REF_LINK_RE = re.compile(r"\[([^\]]*)\]\[[^\]]*\]")
_FENCE_RE = re.compile(r"```.*?(?:```|\Z)", re.DOTALL)
_TILDE_FENCE_RE = re.compile(r"~~~.*?(?:~~~|\Z)", re.DOTALL)
_HR_RE = re.compile(r"(?m)^[ \t]{0,3}(?:[-*_][ \t]*){3,}$")
_TABLE_SEP_RE = re.compile(
    r"(?m)^[ \t]*\|?[ \t]*:?-{2,}:?[ \t]*(?:\|[ \t]*:?-{2,}:?[ \t]*)+\|?[ \t]*$"
)
_HEADING_RE = re.compile(r"(?m)^[ \t]{0,3}#{1,6}[ \t]*")
_QUOTE_RE = re.compile(r"(?m)^[ \t]{0,3}>[ \t]?")
_LIST_RE = re.compile(r"(?m)^[ \t]{0,3}(?:[-*+\u2022]|\d{1,3}[.)])[ \t]+")
_TABLE_PIPE_RE = re.compile(r"(?m)^[ \t]*\|(.+)\|[ \t]*$")
_BOLD_RE = re.compile(r"(\*\*|__)(.+?)\1", re.DOTALL)
_STRIKE_RE = re.compile(r"~~(.+?)~~", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]*)`")
_ITALIC_STAR_RE = re.compile(r"(?<![*\w])\*(?!\s)([^*\n]+?)\*(?![*\w])")
_ITALIC_UNDER_RE = re.compile(r"(?<![_\w])_(?!\s)([^_\n]+?)_(?![_\w])")
_HTML_TAG_RE = re.compile(r"</?[A-Za-z][A-Za-z0-9-]*(?:\s[^<>]*)?/?>")

_CQ_RE = re.compile(r"\[CQ:[^\]]*\]", re.IGNORECASE)
_MENTION_RE = re.compile(
    r"(?:^|(?<=[\s\u3000]))@[^\s@\u3000]{1,24}(?=[\s\u3000，。！？、；：,.!?~～]|$)",
    re.MULTILINE,
)
_ACTION_PAREN_RE = re.compile(r"[（(][^（()）]{0,40}[）)]")
_ACTION_BRACKET_RE = re.compile(r"【[^【】]{0,20}】")


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #
def _drop_symbol_chars(text: str) -> str:
    """删掉 So / Sk / Cs / Co 类别字符（emoji、装饰符号、私用区）。"""
    out: list[str] = []
    for ch in text:
        if unicodedata.category(ch) in ("So", "Sk", "Cs", "Co"):
            continue
        out.append(ch)
    return "".join(out)


def _tighten_cjk(text: str) -> str:
    """把中文上下文里的空格逐轮压掉，直到不再变化。"""
    while True:
        tightened = _TIGHTEN_RE.sub("", text)
        if tightened == text:
            return text
        text = tightened


def _squash(text: str) -> str:
    """压缩空白：统一换行、合并空行、中文语境去空格。"""
    if not text:
        return ""
    s = text.replace("\r\n", "\n").replace("\r", "\n")
    s = _INVISIBLE_RE.sub("", s)
    s = _SPACE_RUN_RE.sub(" ", s)
    s = _SPACE_AROUND_NEWLINE_RE.sub("\n", s)
    s = _NEWLINE_RUN_RE.sub("\n", s)
    s = s.strip()
    if not s:
        return ""
    return _tighten_cjk(s)


def _is_cjk(ch: str) -> bool:
    """单个字符是否属于中文/中文标点（按正则字符类判断，不含区间误用）。"""
    return bool(ch) and _CJK_CHAR_RE.match(ch) is not None


def _join_sentences(sentences: list[str]) -> str:
    """把句子拼回一行：中文之间不加分隔，英文之间补一个空格。"""
    out = ""
    for piece in sentences:
        if not piece:
            continue
        if not out:
            out = piece
        elif out[-1] in _TERMINATORS or _is_cjk(piece[0]):
            out += piece
        else:
            out += " " + piece
    return out


# --------------------------------------------------------------------------- #
# 公开 API
# --------------------------------------------------------------------------- #
def strip_markdown(text: str) -> str:
    """去掉 Markdown 记号，保留可朗读的正文。

    处理：代码块、行内代码、图片（整段删除）、链接（保留链接文字）、裸 URL、
    标题/引用/列表符号、水平线/表格分隔行、强调符号、粗体/斜体/删除线、HTML 标签。
    """
    if not text:
        return ""
    s = _FENCE_RE.sub(" ", text)
    s = _TILDE_FENCE_RE.sub(" ", s)
    s = _IMAGE_RE.sub(" ", s)
    s = _URL_RE.sub(" ", s)
    s = _LINK_RE.sub(r"\1", s)
    s = _REF_LINK_RE.sub(r"\1", s)
    s = _HR_RE.sub(" ", s)
    s = _TABLE_SEP_RE.sub(" ", s)
    s = _TABLE_PIPE_RE.sub(r"\1", s)
    s = _HEADING_RE.sub("", s)
    s = _QUOTE_RE.sub("", s)
    s = _LIST_RE.sub("", s)
    s = _BOLD_RE.sub(r"\2", s)
    s = _STRIKE_RE.sub(r"\1", s)
    s = _INLINE_CODE_RE.sub(r"\1", s)
    s = _ITALIC_STAR_RE.sub(r"\1", s)
    s = _ITALIC_UNDER_RE.sub(r"\1", s)
    s = _HTML_TAG_RE.sub(" ", s)
    return s


def strip_mentions(text: str) -> str:
    """去掉非朗读内容：``[CQ:...]``、``@某人``、动作括号、颜文字/emoji。"""
    if not text:
        return ""
    s = _CQ_RE.sub(" ", text)
    s = _ACTION_PAREN_RE.sub(" ", s)
    s = _ACTION_PAREN_RE.sub(" ", s)  # 处理不完全嵌套的连续括号
    s = _ACTION_BRACKET_RE.sub(" ", s)
    s = _MENTION_RE.sub(" ", s)
    s = _EMOJI_RE.sub(" ", s)
    s = _KAOMOJI_RUN_RE.sub(" ", s)
    s = _drop_symbol_chars(s)
    return s


def split_sentences(text: str) -> list[str]:
    """按句边界切分，返回去掉首尾空白的句子列表（保留句末标点）。

    边界：中文 ``。！？``、英文 ``.!?``、省略号 ``…``、换行；
    ``.`` 后紧跟字母/数字（``3.14``、``v1.2``）不算句边界。
    """
    if not text:
        return []
    sentences: list[str] = []
    buf: list[str] = []
    total = len(text)
    i = 0
    while i < total:
        ch = text[i]
        buf.append(ch)
        i += 1

        boundary = ch in _TERMINATORS
        if not boundary and ch == ".":
            next_is_word = i < total and (text[i].isalnum() or text[i] == "_")
            prev_is_digit = i >= 2 and text[i - 2].isdigit()
            next_is_digit = i < total and text[i].isdigit()
            boundary = not (next_is_word or (prev_is_digit and next_is_digit))

        if boundary:
            while i < total and (text[i] in _TERMINATORS or text[i] in _TAIL_CHARS):
                buf.append(text[i])
                i += 1
            piece = "".join(buf).strip()
            if piece:
                sentences.append(piece)
            buf = []

    tail = "".join(buf).strip()
    if tail:
        sentences.append(tail)
    return sentences


def truncate_sentences(text: str, max_chars: int) -> str:
    """按句边界把文本压进 ``max_chars`` 个字符以内。

    - ``max_chars <= 0``：不限制，原样返回。
    - 未超限：原样返回。
    - 超限：保留**首句 + 末句**，中间用 :data:`ELLIPSIS` 连接；
      若首末句相加仍超限，退化成「首句的 ``max_chars`` 前缀」。
    结果长度保证 ``<= max_chars``。
    """
    if not text:
        return ""
    if max_chars is None or max_chars <= 0:
        return text
    if len(text) <= max_chars:
        return text

    sentences = split_sentences(text)
    if not sentences:
        return text[:max_chars]
    if len(sentences) == 1:
        return sentences[0][:max_chars]

    first, last = sentences[0], sentences[-1]
    if len(first) + len(ELLIPSIS) + len(last) <= max_chars:
        return first + ELLIPSIS + last
    return first[:max_chars]


def prepare(text: str, max_chars: int) -> str:
    """把回复正文整理成可直接送 TTS 的一段文本。

    空输入、或清洗后为空：返回 ``""``（调用方据此直接降级，不调 API）。
    ``max_chars <= 0`` 视为不限制长度。
    """
    if not text:
        return ""

    cleaned = strip_markdown(text)
    cleaned = strip_mentions(cleaned)
    cleaned = _squash(cleaned)
    if not cleaned:
        return ""

    # 换行是断句提示，先按句子拼成一行，避免把 "\n" 交给 TTS。
    if "\n" in cleaned:
        cleaned = _join_sentences(split_sentences(cleaned))
    if not cleaned:
        return ""

    if isinstance(max_chars, int) and max_chars > 0:
        cleaned = truncate_sentences(cleaned, max_chars)
    return cleaned.strip()
