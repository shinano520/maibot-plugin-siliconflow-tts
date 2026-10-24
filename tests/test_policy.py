"""语音开关（群/私聊 × all/whitelist/blacklist）的纯逻辑与插件入口测试。"""

from __future__ import annotations

import asyncio
import logging
import types

import pytest


def _chat(*, is_group, group="", user=""):
    """构造 Host 侧 ``context["chat"]`` 的同形字典（asdict(ToolAvailabilityContext)）。"""

    return {
        "session_id": "qq_group_1" if is_group else "qq_private_1",
        "stream_id": "stream",
        "is_group_chat": is_group,
        "group_id": group,
        "user_id": user,
        "platform": "qq",
    }


def _policy_kwargs(*, is_group, policy, ids):
    kwargs = {
        "group_policy": "whitelist",
        "group_list": [],
        "private_policy": "whitelist",
        "private_list": [],
    }
    if is_group:
        kwargs.update(group_policy=policy, group_list=ids)
    else:
        kwargs.update(private_policy=policy, private_list=ids)
    return kwargs


# ── 归一化 ────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (" 123 ", "123"),
        ("qq:123", "123"),
        ("QQ：123", "123"),
        ("qq : 123", "123"),
        ("１２３", "123"),
        ("1 2 3", "123"),
        (123, "123"),
        (None, ""),
        ("", ""),
        ("   ", ""),
    ],
)
def test_normalize_peer_id(reply_ext_module, raw, expected):
    assert reply_ext_module.normalize_peer_id(raw) == expected


def test_parse_peer_list_tolerates_dirty_items(reply_ext_module):
    assert reply_ext_module.parse_peer_list(
        [" 123 ", "", "qq:456", "７８９", "12,34", None]
    ) == {"123", "456", "789", "12", "34"}
    assert reply_ext_module.parse_peer_list(None) == set()
    assert reply_ext_module.parse_peer_list(["", "  "]) == set()


# ── is_allowed 矩阵：两种维度 × 三种策略 × 命中/未命中 ─────────
@pytest.mark.parametrize(
    ("is_group", "policy", "ids", "peer", "expected"),
    [
        (True, "all", [], "100", True),
        (True, "whitelist", ["100"], "100", True),
        (True, "whitelist", ["200"], "100", False),
        (True, "whitelist", [], "100", False),
        (True, "blacklist", ["100"], "100", False),
        (True, "blacklist", ["200"], "100", True),
        (False, "all", [], "100", True),
        (False, "whitelist", ["100"], "100", True),
        (False, "whitelist", ["200"], "100", False),
        (False, "whitelist", [], "100", False),
        (False, "blacklist", ["100"], "100", False),
        (False, "blacklist", ["200"], "100", True),
    ],
)
def test_is_allowed_matrix(reply_ext_module, is_group, policy, ids, peer, expected):
    assert (
        reply_ext_module.is_allowed(
            is_group=is_group,
            peer_id=peer,
            **_policy_kwargs(is_group=is_group, policy=policy, ids=ids),
        )
        is expected
    )


@pytest.mark.parametrize("is_group", [True, False])
def test_dirty_whitelist_hits_after_normalize(reply_ext_module, is_group):
    kwargs = _policy_kwargs(is_group=is_group, policy="whitelist", ids=[" 123 ", "qq:456", "７８９"])
    assert reply_ext_module.is_allowed(is_group=is_group, peer_id="123", **kwargs) is True
    assert reply_ext_module.is_allowed(is_group=is_group, peer_id="qq:456", **kwargs) is True
    assert reply_ext_module.is_allowed(is_group=is_group, peer_id="789", **kwargs) is True
    assert reply_ext_module.is_allowed(is_group=is_group, peer_id="999", **kwargs) is False


def test_policy_is_case_insensitive(reply_ext_module):
    kwargs = _policy_kwargs(is_group=True, policy="ALL", ids=[])
    assert reply_ext_module.is_allowed(is_group=True, peer_id="100", **kwargs) is True


@pytest.mark.parametrize("policy", [None, "", "allow", "启用", 1])
def test_unknown_policy_denies(reply_ext_module, policy):
    kwargs = _policy_kwargs(is_group=False, policy=policy, ids=["100"])
    assert reply_ext_module.is_allowed(is_group=False, peer_id="100", **kwargs) is False


@pytest.mark.parametrize("policy", ["all", "whitelist", "blacklist"])
def test_empty_peer_denies_even_with_all(reply_ext_module, policy):
    kwargs = _policy_kwargs(is_group=True, policy=policy, ids=["100"])
    assert reply_ext_module.is_allowed(is_group=True, peer_id="", **kwargs) is False
    assert reply_ext_module.is_allowed(is_group=True, peer_id=None, **kwargs) is False


def test_group_and_private_lists_are_independent(reply_ext_module):
    kwargs = {
        "group_policy": "whitelist",
        "group_list": ["100"],
        "private_policy": "whitelist",
        "private_list": ["200"],
    }
    assert reply_ext_module.is_allowed(is_group=True, peer_id="100", **kwargs) is True
    assert reply_ext_module.is_allowed(is_group=False, peer_id="100", **kwargs) is False
    assert reply_ext_module.is_allowed(is_group=False, peer_id="200", **kwargs) is True
    assert reply_ext_module.is_allowed(is_group=True, peer_id="200", **kwargs) is False


# ── chat 入口：缺字段 / None / 类型安全 ───────────────────────
def test_chat_none_or_missing_denies(reply_ext_module):
    kwargs = _policy_kwargs(is_group=True, policy="all", ids=[])
    assert reply_ext_module.is_chat_allowed(None, **kwargs) is False
    assert reply_ext_module.is_chat_allowed({}, **kwargs) is False
    assert reply_ext_module.is_chat_allowed({"is_group_chat": None}, **kwargs) is False
    assert reply_ext_module.is_chat_allowed({"is_group_chat": "true"}, **kwargs) is False


def test_group_without_group_id_denies(reply_ext_module):
    kwargs = _policy_kwargs(is_group=True, policy="all", ids=[])
    chat = _chat(is_group=True, group="")
    assert reply_ext_module.is_chat_allowed(chat, **kwargs) is False


def test_private_without_user_id_denies(reply_ext_module):
    kwargs = _policy_kwargs(is_group=False, policy="all", ids=[])
    chat = _chat(is_group=False, user="")
    assert reply_ext_module.is_chat_allowed(chat, **kwargs) is False


def test_int_ids_from_host_are_supported(reply_ext_module):
    """Host 的 group_id/user_id 若是 int，也能正确归一化并命中。"""

    kwargs = _policy_kwargs(is_group=True, policy="whitelist", ids=["qq:123456"])
    chat = _chat(is_group=True)
    chat["group_id"] = 123456
    assert reply_ext_module.is_chat_allowed(chat, **kwargs) is True

    kwargs = _policy_kwargs(is_group=False, policy="whitelist", ids=["１２３４５６"])
    chat = _chat(is_group=False)
    chat["user_id"] = 123456
    assert reply_ext_module.is_chat_allowed(chat, **kwargs) is True


# ── 默认配置：whitelist + 空名单 = 谁都不触发 ──────────────────
def test_default_config_denies_everyone(config_module, reply_ext_module):
    cfg = config_module.SiliconFlowTTSConfig()
    assert cfg.group_policy == "whitelist"
    assert cfg.group_list == []
    assert cfg.private_policy == "whitelist"
    assert cfg.private_list == []

    kwargs = {
        "group_policy": cfg.group_policy,
        "group_list": cfg.group_list,
        "private_policy": cfg.private_policy,
        "private_list": cfg.private_list,
    }
    assert reply_ext_module.is_chat_allowed(
        _chat(is_group=True, group="652468225"), **kwargs
    ) is False
    assert reply_ext_module.is_chat_allowed(
        _chat(is_group=False, user="3430500996"), **kwargs
    ) is False


def test_config_accepts_int_items_in_toml(config_module):
    """config.toml 里手写 `group_list = [123]` 也能加载（校验器转字符串）。"""

    cfg = config_module.SiliconFlowTTSConfig(plugin={"group_list": [123], "private_list": "456"})
    assert cfg.group_list == ["123"]
    assert cfg.private_list == ["456"]


# ── 插件入口：默认拒绝、异常拒绝、两个 phase 都返回 {} ─────────
def _fake_plugin(plugin_module, config_module):
    fake = types.SimpleNamespace(
        config=config_module.SiliconFlowTTSConfig(),
        _logger=lambda: logging.getLogger("tts-policy-test"),
    )
    fake._voice_allowed = types.MethodType(plugin_module.SiliconFlowTTSPlugin._voice_allowed, fake)
    fake._log_policy_denied = types.MethodType(
        plugin_module.SiliconFlowTTSPlugin._log_policy_denied, fake
    )
    return fake


def test_plugin_gate_default_denies(plugin_module, config_module):
    fake = _fake_plugin(plugin_module, config_module)
    context = {"chat": _chat(is_group=True, group="652468225")}
    assert plugin_module.SiliconFlowTTSPlugin._voice_allowed(fake, context) is False


def test_plugin_gate_denies_on_exception(plugin_module, config_module, monkeypatch, caplog):
    def boom(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(plugin_module, "is_chat_allowed", boom)
    fake = _fake_plugin(plugin_module, config_module)

    with caplog.at_level(logging.WARNING, logger="tts-policy-test"):
        assert plugin_module.SiliconFlowTTSPlugin._voice_allowed(fake, {"chat": {}}) is False
    assert "按默认白名单拒绝" in caplog.text


def test_reply_voice_returns_empty_for_both_phases_when_denied(plugin_module, config_module):
    fake = _fake_plugin(plugin_module, config_module)
    context = {"chat": _chat(is_group=False, user="3430500996")}
    messages = [{"segments": [{"type": "text", "data": "hi"}], "quote_previous": False}]

    for phase in ("prepare", "before_send"):
        result = asyncio.run(
            plugin_module.SiliconFlowTTSPlugin.reply_voice(
                fake,
                phase=phase,
                parameters={},
                text="你好",
                messages=messages,
                **context,
            )
        )
        assert result == {}


# ── t13：拒绝日志 + /tts status 语音开关 ───────────────────────
def test_denied_logs_reason(plugin_module, config_module, caplog):
    fake = _fake_plugin(plugin_module, config_module)
    context = {"chat": _chat(is_group=True, group="652468225")}

    with caplog.at_level(logging.INFO, logger="tts-policy-test"):
        assert (
            plugin_module.SiliconFlowTTSPlugin._voice_allowed(
                fake, context, phase="before_send"
            )
            is False
        )

    text = caplog.text
    assert "语音开关拒绝" in text
    assert "is_group=True" in text
    assert "peer=652468225" in text
    assert "policy=whitelist" in text
    assert "hit=False" in text


def test_denied_logs_only_once_for_two_phases(plugin_module, config_module, caplog):
    fake = _fake_plugin(plugin_module, config_module)
    context = {"chat": _chat(is_group=False, user="3430500996")}

    with caplog.at_level(logging.INFO, logger="tts-policy-test"):
        assert (
            plugin_module.SiliconFlowTTSPlugin._voice_allowed(fake, context, phase="prepare")
            is False
        )
        assert (
            plugin_module.SiliconFlowTTSPlugin._voice_allowed(
                fake, context, phase="before_send"
            )
            is False
        )

    assert caplog.text.count("语音开关拒绝") == 1


def test_policy_summary_shows_policy_and_counts_without_numbers(commands_module):
    text = commands_module.format_policy_summary(
        group_policy="whitelist",
        group_list=["652468225", "100"],
        private_policy="whitelist",
        private_list=["3430500996"],
    )

    assert "群聊" in text and "whitelist" in text
    assert "已配置 2 个群" in text
    assert "私聊" in text and "已配置 1 个用户" in text
    assert "652468225" not in text
    assert "3430500996" not in text


def test_policy_summary_all_and_blacklist_hints(commands_module):
    text = commands_module.format_policy_summary(
        group_policy="all",
        group_list=[],
        private_policy="blacklist",
        private_list=["1", "2"],
    )

    assert "不限会话" in text
    assert "blacklist" in text
    assert "名单 2 个用户" in text


def test_policy_summary_empty_whitelist_warns_no_voice(commands_module):
    text = commands_module.format_policy_summary(
        group_policy="whitelist",
        group_list=[],
        private_policy="whitelist",
        private_list=[],
    )

    assert text.count("当前不会发语音") == 2


def test_status_renders_policy_block(commands_module):
    block = commands_module.format_policy_summary(
        group_policy="whitelist",
        group_list=["1", "2"],
        private_policy="all",
        private_list=[],
    )
    out = commands_module.render_status({"运行开关": "开启", "语音开关": "\n" + block})

    assert out.startswith("🎙️")
    assert "语音开关" in out
    assert "已配置 2 个群" in out
    assert "不限会话" in out
