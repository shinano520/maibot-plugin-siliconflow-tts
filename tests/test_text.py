"""``text.py`` 单测：清洗、断句、截断边界。"""

from __future__ import annotations

import pytest

MAX = 300


# --------------------------------------------------------------------------- #
# strip_markdown
# --------------------------------------------------------------------------- #
def test_strip_markdown_removes_blocks_and_emphasis(text_module):
    raw = (
        "## 标题\n"
        "```python\nprint('x')\n```\n"
        "- 第一项\n"
        "1. 第二项\n"
        "> 引用\n"
        "---\n"
        "**粗体** 与 *斜体* 与 ~~删除~~ 与 `行内`\n"
        "[链接文字](https://example.com/a)\n"
        "![图片](https://example.com/a.png)\n"
        "裸链接 https://example.com/b 结束\n"
    )
    out = text_module.strip_markdown(raw)
    for marker in ("#", "```", "**", "*斜体*", "~~", "`", "](http"):
        assert marker not in out
    assert "标题" in out
    assert "粗体" in out
    assert "斜体" in out
    assert "链接文字" in out
    assert "print" not in out, "代码块内容不应朗读"
    assert "https://" not in out


# --------------------------------------------------------------------------- #
# strip_mentions
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("[CQ:at,qq=123456]你好", "你好"),
        ("@张三 你好", "你好"),
        ("你好（挥手）呀", "你好呀"),
        ("你好(挥手)呀", "你好呀"),
        ("【动作】你好", "你好"),
        ("你好😊", "你好"),
        ("你好(╯°□°）╯︵ ┻━┻", "你好"),
    ],
)
def test_strip_mentions(text_module, raw, expected):
    out = text_module.prepare(raw, MAX)
    assert out == expected
    assert " " not in out


# --------------------------------------------------------------------------- #
# split_sentences
# --------------------------------------------------------------------------- #
def test_split_sentences_chinese(text_module):
    assert text_module.split_sentences("你好。世界！在吗？") == [
        "你好。",
        "世界！",
        "在吗？",
    ]


def test_split_sentences_english_and_newline(text_module):
    assert text_module.split_sentences("First one. Second one!\nThird?") == [
        "First one.",
        "Second one!",
        "Third?",
    ]


def test_split_sentences_keeps_decimal_and_closing_quote(text_module):
    assert text_module.split_sentences("圆周率是3.14倍。") == ["圆周率是3.14倍。"]
    assert text_module.split_sentences("他说：“你好。”") == ["他说：“你好。”"]


def test_split_sentences_empty(text_module):
    assert text_module.split_sentences("") == []
    assert text_module.split_sentences("   ") == []


# --------------------------------------------------------------------------- #
# truncate_sentences：边界
# --------------------------------------------------------------------------- #
TEXT_15 = "第一句话。第二句话。第三句话。"  # 15 字


def test_truncate_exactly_at_limit_is_unchanged(text_module):
    assert len(TEXT_15) == 15
    assert text_module.truncate_sentences(TEXT_15, 15) == TEXT_15


def test_truncate_one_char_over_keeps_first_and_last(text_module):
    out = text_module.truncate_sentences(TEXT_15, 14)  # 超 1 个字
    assert out == "第一句话。……第三句话。"
    assert len(out) <= 14


def test_truncate_tight_budget_falls_back_to_first_prefix(text_module):
    out = text_module.truncate_sentences(TEXT_15, 10)
    assert out == "第一句话。"
    assert len(out) <= 10


def test_truncate_single_long_sentence(text_module):
    assert text_module.truncate_sentences("一二三四五六七八九十", 4) == "一二三四"


def test_truncate_zero_means_unlimited(text_module):
    assert text_module.truncate_sentences(TEXT_15, 0) == TEXT_15


# --------------------------------------------------------------------------- #
# prepare：整体行为
# --------------------------------------------------------------------------- #
def test_prepare_simple_markdown(text_module):
    assert text_module.prepare("**你好**，世界！", MAX) == "你好，世界！"


def test_prepare_removes_all_spaces_between_chinese(text_module):
    out = text_module.prepare("今天 天气 真好", MAX)
    assert out == "今天天气真好"
    assert " " not in out


def test_prepare_keeps_english_word_spacing(text_module):
    out = text_module.prepare("hello world 你好", MAX)
    assert "hello world" in out


def test_prepare_flattens_newlines(text_module):
    out = text_module.prepare("## 标题\n\n正文第一句。\n正文第二句。", MAX)
    assert "\n" not in out
    assert out.startswith("标题")
    assert out.endswith("正文第二句。")
    assert " " not in out, "换行拼回一行时不许插入空格"


def test_prepare_join_does_not_insert_space_between_chinese_lines(text_module):
    """回归：``_join_sentences`` 曾用 ``in`` 判断区间字符串，导致拼接出空格。"""
    out = text_module.prepare("麦麦来啦\n你好呀\n今天天气不错", MAX)
    assert out == "麦麦来啦你好呀今天天气不错"
    assert " " not in out


def test_prepare_empty_inputs(text_module):
    assert text_module.prepare("", MAX) == ""
    assert text_module.prepare("   \n\t ", MAX) == ""
    assert text_module.prepare("😊😊", MAX) == "", "清洗后为空应返回空串"


def test_prepare_respects_max_chars(text_module):
    assert text_module.prepare(TEXT_15, 15) == TEXT_15
    assert text_module.prepare(TEXT_15, 14) == "第一句话。……第三句话。"


def test_prepare_output_is_tts_safe(text_module):
    """模拟一条真实回复：不应残留 Markdown、CQ 码、括号动作和多余空白。"""
    raw = (
        "麦麦来啦~\n"
        "[CQ:at,qq=10001] 看这里（挥手）\n"
        "- **重点**：`config.toml` 里填 api_key\n"
        "详见 [文档](https://example.com/doc)。"
    )
    out = text_module.prepare(raw, MAX)
    assert "\n" not in out
    assert "[CQ:" not in out
    assert "**" not in out
    assert "`" not in out
    assert "（挥手）" not in out
    assert "https://" not in out
    assert "麦麦来啦" in out
    # 中文之间不留空格
    assert "看这里" in out
