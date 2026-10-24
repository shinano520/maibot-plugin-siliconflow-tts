"""硅基流动 TTS 插件配置模型（唯一配置真源）。

结构说明（**实测订正 CONTRACT §0/§3.1/§4 的「扁平单层」写法**）：

- Runner / SDK 2.16 的 ``extract_plugin_config_version()`` 要求配置字典里存在
  ``{"plugin": {"config_version": "..."}}``，缺失时抛
  ``PluginConfigVersionError``，``_apply_plugin_config()``（``suppress_errors=False``）
  会直接把异常抛出 → 插件加载失败。因此 ``config.toml`` 必须是标准嵌套写法：

  .. code-block:: toml

      [plugin]
      config_version = "1.0.0"
      enabled = true
      api_key = ""

- ``[plugin].enabled`` 同时也是 Runner 的宿主保留键（``plugin_enabled.is_plugin_disabled``），
  语义与契约里的「总开关」一致，合并为同一字段。
- 为了让契约冻结的 ``self.config.speed`` / ``self.config.enabled`` 等访问方式继续成立，
  外层模型用只读属性逐项转发到 ``self.plugin``；下游（provider / reply_ext / commands）
  不用感知嵌套结构。
"""

from __future__ import annotations

import os
from typing import Literal

from maibot_sdk import Field, PluginConfigBase

#: ``api_key`` 留空时回落的环境变量名（PLAN §5）
API_KEY_ENV_VAR = "SILICONFLOW_API_KEY"


class SiliconFlowTTSSection(PluginConfigBase):
    """``config.toml`` 的 ``[plugin]`` 段：19 个字段，顺序即 WebUI 展示顺序。"""

    __ui_label__ = "硅基流动 TTS"
    __ui_icon__ = "audio-lines"
    __ui_order__ = 0

    config_version: str = Field(
        default="1.0.0",
        description="配置版本，由 Runner 维护，插件不要读写",
        json_schema_extra={"hidden": True},
    )
    enabled: bool = Field(default=True, description="总开关；false 时回复扩展直接放行原消息")
    api_key: str = Field(
        default="",
        description="硅基流动 API Key；留空则回落到环境变量 SILICONFLOW_API_KEY",
    )
    base_url: str = Field(default="https://api.siliconflow.cn/v1", description="API 根地址，可指向自建反代")
    model: str = Field(
        default="fnlp/MOSS-TTSD-v0.5",
        description=(
            "TTS 模型名；FunAudioLLM/CosyVoice2-0.5B 当前上游不可用（实测会 100% 超时），"
            "请保持 fnlp/MOSS-TTSD-v0.5"
        ),
    )
    voice: str = Field(
        default="fnlp/MOSS-TTSD-v0.5:anna",
        description=(
            "音色：模型:音色名，也可只写音色名；CosyVoice2 的音色当前上游不可用（换回会超时），"
            "默认用 MOSS 内置音色 anna"
        ),
    )
    speed: float = Field(default=1.0, ge=0.25, le=4.0, description="语速 [0.25, 4.0]")
    gain: float = Field(default=0.0, ge=-10.0, le=10.0, description="增益 dB [-10, 10]")
    response_format: Literal["mp3", "wav", "opus"] = Field(
        default="mp3",
        description="音频格式",
        json_schema_extra={"enum": ["mp3", "wav", "opus"], "enum_titles": ["MP3", "WAV", "Opus"]},
    )
    sample_rate: int = Field(
        default=44100,
        description="采样率；合法性随 response_format 变化（mp3: 32000/44100，wav: 8000/16000/24000/32000/44100，opus: 48000）",
    )
    keep_text: bool = Field(default=True, description="语音之外是否保留原文字（回复扩展参数缺省时的默认值）")
    auto_voice: bool = Field(default=False, description="true 时默认给回复加语音（当前版本尚未接入 Hook，仅占位）")
    max_chars: int = Field(default=300, ge=1, description="单次合成文本上限（字符数），按句边界截断")
    timeout_ms: int = Field(
        default=60000,
        ge=1000,
        description="单次回复扩展超时，与 @ReplyExtension(timeout_ms=) 对齐",
    )
    retry: int = Field(default=2, ge=0, le=5, description="429/5xx 重试次数")
    cache_enabled: bool = Field(default=True, description="音频缓存开关")
    cache_ttl_hours: int = Field(default=72, ge=0, description="缓存过期小时数；0 表示不过期")
    cache_max_mb: int = Field(default=200, ge=0, description="缓存上限 MB，超出按 LRU 淘汰；0 表示不限制")
    fallback_on_error: Literal["text", "fail"] = Field(
        default="text",
        description="失败策略：text=降级为纯文字；fail=让本次 reply 失败（仅联调）",
        json_schema_extra={"enum": ["text", "fail"], "enum_titles": ["降级为文字", "让回复失败"]},
    )

    @property
    def effective_api_key(self) -> str:
        """返回实际使用的 API Key：配置优先，其次环境变量，最后空串。"""

        explicit = (self.api_key or "").strip()
        if explicit:
            return explicit
        return (os.environ.get(API_KEY_ENV_VAR) or "").strip()

    @property
    def effective_voice(self) -> str:
        """返回可直接下发给硅基流动的音色标识。

        只写音色名（``alex``）时补成 ``模型:音色名``；已含 ``模型:音色`` 或
        ``speech:`` 动态音色前缀时原样返回。
        """

        voice = (self.voice or "").strip()
        if not voice:
            return ""
        if voice.startswith("speech:"):
            return voice
        if ":" in voice:
            return voice
        model = (self.model or "").strip()
        return f"{model}:{voice}" if model else voice

    @property
    def timeout_seconds(self) -> float:
        """返回以秒为单位的超时时间。"""

        return self.timeout_ms / 1000.0


class SiliconFlowTTSConfig(PluginConfigBase):
    """插件配置模型：唯一 ``[plugin]`` 段 + 契约访问面。

    外层只声明 ``plugin`` 一个字段，其余全部通过只读属性转发，因此：

    - ``self.config.speed`` / ``self.config.enabled``（契约冻结写法）照常可用；
    - ``self.config.plugin.speed``（SDK 标准写法）同样可用；
    - ``build_plugin_default_config()`` 产出 ``{"plugin": {...}}``，满足 Runner 的
      ``plugin.config_version`` 版本检查。
    """

    plugin: SiliconFlowTTSSection = Field(
        default_factory=SiliconFlowTTSSection,
        description="硅基流动 TTS 配置",
    )

    # ── 契约访问面（只读转发；写入请走 self.config.plugin.<field>）──
    @property
    def config_version(self) -> str:
        return self.plugin.config_version

    @property
    def enabled(self) -> bool:
        return self.plugin.enabled

    @property
    def api_key(self) -> str:
        return self.plugin.api_key

    @property
    def base_url(self) -> str:
        return self.plugin.base_url

    @property
    def model(self) -> str:
        return self.plugin.model

    @property
    def voice(self) -> str:
        return self.plugin.voice

    @property
    def speed(self) -> float:
        return self.plugin.speed

    @property
    def gain(self) -> float:
        return self.plugin.gain

    @property
    def response_format(self) -> Literal["mp3", "wav", "opus"]:
        return self.plugin.response_format

    @property
    def sample_rate(self) -> int:
        return self.plugin.sample_rate

    @property
    def keep_text(self) -> bool:
        return self.plugin.keep_text

    @property
    def auto_voice(self) -> bool:
        return self.plugin.auto_voice

    @property
    def max_chars(self) -> int:
        return self.plugin.max_chars

    @property
    def timeout_ms(self) -> int:
        return self.plugin.timeout_ms

    @property
    def retry(self) -> int:
        return self.plugin.retry

    @property
    def cache_enabled(self) -> bool:
        return self.plugin.cache_enabled

    @property
    def cache_ttl_hours(self) -> int:
        return self.plugin.cache_ttl_hours

    @property
    def cache_max_mb(self) -> int:
        return self.plugin.cache_max_mb

    @property
    def fallback_on_error(self) -> Literal["text", "fail"]:
        return self.plugin.fallback_on_error

    @property
    def effective_api_key(self) -> str:
        return self.plugin.effective_api_key

    @property
    def effective_voice(self) -> str:
        return self.plugin.effective_voice

    @property
    def timeout_seconds(self) -> float:
        return self.plugin.timeout_seconds
