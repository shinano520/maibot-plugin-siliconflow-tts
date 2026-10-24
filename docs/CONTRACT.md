# 插件契约（v1.0 · 冻结）

> 适用版本：`local.siliconflow-tts` 0.1.x
> 状态：**冻结**。`provider-dev` 与 `plugin-dev` 按本文实现，公开名称、参数名、返回类型一律照抄；
> 需要改动先找 architect 改本文并升版本，不得在代码里自行改名。
>
> 依据：`docs/PLAN.md` + 官方文档（[插件开发指南](https://docs.mai-mai.org/plugin/)、[Manifest](https://docs.mai-mai.org/plugin/manifest)、
> [配置](https://docs.mai-mai.org/plugin/config)、[回复扩展](https://docs.mai-mai.org/plugin/reply-extensions)、
> [硅基流动 TTS](https://docs.siliconflow.cn/docs/userguide/capabilities/text-to-speech)）
> + 运行实例实测（见 §1.4）。

---

## 0. 冻结清单（TL;DR）

| 项 | 冻结值 |
|---|---|
| 插件 ID | `local.siliconflow-tts` |
| 插件版本 | `0.1.0`（严格三段式） |
| Manifest | `manifest_version=2`，见 [_manifest.json](../_manifest.json) |
| SDK 区间 | `min_version=2.14.0` / `max_version=999.999.999` |
| 能力声明 | `send.hybrid`、`config.get` |
| 回复扩展组件名 | **`voice`**，完整命名空间 `local.siliconflow-tts.voice`（Host 拼接） |
| 配置模型类 | `config.SiliconFlowTTSConfig`（外层）+ `config.SiliconFlowTTSSection`（`[plugin]` 段，19 字段）；对外仍暴露 `self.config.speed` 等转发属性 —— **不可用扁平顶层写法**，见 §3.1 订正 |
| 插件类 | `plugin.SiliconFlowTTSPlugin`（`MaiBotPlugin` 子类），入口 `create_plugin()` |
| 必实现生命周期 | `on_load()` / `on_unload()` / `on_config_update(scope, config_data, version)` |
| 数据目录 | `self.ctx.paths.data_dir`（= `<MaiBot>/data/plugins/local.siliconflow-tts`），**不自己拼路径** |
| HTTP 客户端 | `httpx`（容器内实测 0.28.1，manifest 已声明 `>=0.27.0`） |

### 0.1 「契约写法」→「实际定义」对照

本文用简写描述公开 API，落地时按此表对应，**名字一个字符都不要改**：

| 契约写法 | 实际定义 |
|---|---|
| `provider.synthesize(text, *, timeout)` | `SiliconFlowTTSProvider.synthesize(self, text, *, timeout=None)`，`async`，返回 `bytes` |
| `cache.make_key(...)` | 模块级函数 `cache.make_key(*, text, model, voice, speed, gain, response_format, sample_rate) -> str` |
| `cache.get(key)` / `cache.put(key, data)` | 实例方法 `AudioCache.get(key) -> bytes \| None` / `AudioCache.put(key, data) -> None` |
| `text.prepare(text, max_chars)` | 模块级函数 `text.prepare(text: str, max_chars: int) -> str` |
| `audio.ensure_compatible(data, fmt)` | 模块级函数 `audio.ensure_compatible(data: bytes, fmt: str) -> bytes` |
| `reply_ext` 组件 `voice` | `@ReplyExtension("voice", ...)` 修饰的 `SiliconFlowTTSPlugin.reply_voice` 方法 |

---

## 1. 目录与加载模型（硬约束，已实测）

### 1.1 插件目录 = Python 包，模块平铺在仓库根

```
maibot-plugin-siliconflow-tts/          ← 插件根（部署时整个目录放进 <MaiBot>/plugins/）
├── _manifest.json
├── plugin.py          # 入口：装配 + MaiBotPlugin 子类 + create_plugin()
├── config.py          # 配置模型（唯一配置真源）
├── provider.py        # 硅基流动 TTS HTTP 客户端
├── cache.py           # 音频缓存
├── text.py            # 文本准备
├── audio.py           # 音频格式决策
├── reply_ext.py       # ReplyExtension 处理逻辑（被 plugin.py 引用）
├── commands.py        # /tts 命令组
├── docs/
└── tests/
```

- **禁止** `__init__.py`；**禁止**嵌套子包（`maibot_plugin_siliconflow_tts/` 这种目录不存在）。
- 模块之间一律**相对导入**：`from .config import SiliconFlowTTSConfig`、`from .provider import create_provider`。
- 依据（Runner 源码 `src/plugin_runtime/runner/plugin_loader.py`）：

```python
module_name = self._build_safe_module_name(plugin_id)   # → "_maibot_plugin_local_siliconflow_tts"
spec = importlib.util.spec_from_file_location(
    module_name, str(plugin_path), submodule_search_locations=[str(plugin_dir)],
)
```

即：**包名由插件 ID 生成**（`[^0-9A-Za-z_] → _`，前缀 `_maibot_plugin_`），`submodule_search_locations` 指向插件根。
所以 `from .config import ...` 能正常解析；目录名带连字符（`maibot-plugin-siliconflow-tts`）**不影响**导入。
参照运行实例 `/opt/maibot/data/MaiMBot/plugins/internetsb_maizone/`：同样无 `__init__.py`、同样 `from .config import ...`。

### 1.2 入口契约

```python
# plugin.py
from maibot_sdk import MaiBotPlugin

from .config import SiliconFlowTTSConfig

class SiliconFlowTTSPlugin(MaiBotPlugin):
    config_model = SiliconFlowTTSConfig

    async def on_load(self) -> None: ...
    async def on_unload(self) -> None: ...
    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None: ...

def create_plugin() -> SiliconFlowTTSPlugin:
    return SiliconFlowTTSPlugin()
```

- 三个生命周期方法**必须由子类覆盖**（不是继承默认实现）。Runner 的 `_validate_sdk_plugin_contract` 用
  `type(instance).on_load is MaiBotPlugin.on_load` 判断，未覆盖直接 `TypeError`、插件加载失败。
- `on_load` 在 `ctx` 与配置注入**之后**调用，可以安全使用 `self.ctx.*` 与 `self.config.*`。
- `on_config_update` 的 `scope == "self"` 时 `self.config` 已是最新值，**不要**手动重读 TOML。
- `create_plugin()` 必须存在且返回已实例化的插件对象（不是类）。

### 1.3 数据与配置落点

| 用途 | 位置 |
|---|---|
| 运行时配置 | 插件根 `config.toml`（Runner 生成/维护，仓库 `.gitignore` 已忽略） |
| 缓存/状态 | `self.ctx.paths.data_dir`（`<MaiBot>/data/plugins/local.siliconflow-tts`，Runner 保证存在） |
| 临时文件 | `self.ctx.paths.runtime_dir`（可选，重启清空） |

禁止：`Path(__file__).parent / "data"`、`os.getcwd()`、写插件根目录下的自建目录。

### 1.4 实测环境（2026-10-25，运行实例）

| 事实 | 值 |
|---|---|
| 容器 | `maim-bot-core`，`/MaiMBot` |
| `maibot-plugin-sdk` | **2.16.0**（≥ 2.14.0，`ReplyExtension.platforms` 可用） |
| Python | 3.13 |
| httpx / pydantic | 0.28.1 / 2.13.3 |
| 宿主能力表含 | `send.hybrid`、`config.get`、`config.get_all`、`config.get_plugin` |
| 出站语音 | Host 把 `voice` 段 + `binary_data_base64` 交给适配器，适配器编码成 OneBot v11 `record` |

---

## 2. `_manifest.json`

### 2.1 现行文件（冻结）

```json
{
  "manifest_version": 2,
  "id": "local.siliconflow-tts",
  "version": "0.1.0",
  "name": "硅基流动语音合成",
  "description": "让麦麦（MaiBot）把文字回复用硅基流动 TTS 合成 QQ 语音消息；可保留或替换原文字。",
  "author": { "name": "shinano520", "url": "https://github.com/shinano520" },
  "license": "MIT",
  "urls": {
    "repository": "https://github.com/shinano520/maibot-plugin-siliconflow-tts",
    "homepage": "https://github.com/shinano520/maibot-plugin-siliconflow-tts",
    "documentation": "https://github.com/shinano520/maibot-plugin-siliconflow-tts/blob/main/docs/CONTRACT.md",
    "issues": "https://github.com/shinano520/maibot-plugin-siliconflow-tts/issues"
  },
  "host_application": { "min_version": "1.0.0", "max_version": "999.999.999" },
  "sdk": { "min_version": "2.14.0", "max_version": "999.999.999" },
  "dependencies": [
    { "type": "python_package", "name": "httpx", "version_spec": ">=0.27.0" }
  ],
  "plugin_type": "provider",
  "capabilities": ["send.hybrid", "config.get"],
  "i18n": { "default_locale": "zh-CN", "supported_locales": ["zh-CN"] },
  "display": {
    "icon": { "type": "lucide", "value": "audio-lines", "fallback": "music", "background": "#1f2937" }
  }
}
```

### 2.2 校验规则（Host `ManifestValidator`，Pydantic 严格模式）

- **不允许多余字段**：加注释字段/临时字段会被 `extra fields not permitted` 拒掉。
- `id` 必须匹配 `^[A-Za-z0-9_]+(?:[.-][A-Za-z0-9_]+)+$`；`local.siliconflow-tts` ✅。
- `version`、`host_application.*`、`sdk.*` 必须是严格 `X.Y.Z`；`999.999.999` 是官方推荐的上界写法。
- `author` 必须是 `{name, url}` 且 `url` 以 `http(s)://` 开头；`urls.repository` 必填。
- `i18n.default_locale` 必填；`supported_locales` 非空时必须包含 `default_locale`。
- `capabilities` 逐项精确比对：声明了才能调用。本插件实际只用 `config.get`（`self.ctx.config`）；
  `send.hybrid` 为预留声明（当前不调用 `send.*`，语音由 ReplyExtension 交主程序发送）。
- `dependencies` 中的 `httpx>=0.27.0` 已在宿主 venv 满足（0.28.1），不会触发安装。
- 本地自检：

```bash
python -m json.tool _manifest.json > /dev/null && echo OK
```

---

## 3. 模块公开 API（冻结签名）

> 所有模块**顶层不得 `import maibot_sdk`**，除 `config.py` / `plugin.py` / `reply_ext.py`（组件声明需要）。
> `provider.py`、`cache.py`、`text.py`、`audio.py` 必须能被 pytest 在无 SDK 环境导入（`provider` 只用 httpx）。

### 3.1 `config.py`

> **⚠️ v1.1 订正（t3 实测，SDK/Runner 2.16.0）**
>
> 本节原写「扁平单层、`config_version` 放顶层」，**这是错的**。Runner 的
> `extract_plugin_config_version()` 只认 `{"plugin": {"config_version": ...}}`；
> 缺失时抛 `PluginConfigVersionError`，而 `_apply_plugin_config(suppress_errors=False)`
> 会把它直接抛出 → **插件加载失败**。
>
> 正确写法：`config.toml` 用标准 `[plugin]` 段（19 字段全在段内，`config_version` 必填），
> 外层模型用**只读转发属性**维持契约冻结的 `self.config.speed` / `self.config.enabled` 访问面，
> 下游模块无需感知嵌套。实际实现见 `config.py`。

```python
from typing import Literal

from maibot_sdk import Field, PluginConfigBase


class SiliconFlowTTSSection(PluginConfigBase):
    """``config.toml`` 的 ``[plugin]`` 段（字段见 §4）。"""

    __ui_label__ = "硅基流动 TTS"

    config_version: str = Field(default="1.0.0", description="配置版本，由 Runner 维护，插件不要读写")
    enabled: bool = Field(default=True, description="总开关；false 时回复扩展直接放行原消息")
    api_key: str = Field(default="", description="硅基流动 API Key；留空则回落到环境变量 SILICONFLOW_API_KEY")
    base_url: str = Field(default="https://api.siliconflow.cn/v1", description="API 根地址，可指向自建反代")
    model: str = Field(default="FunAudioLLM/CosyVoice2-0.5B", description="TTS 模型名")
    voice: str = Field(default="FunAudioLLM/CosyVoice2-0.5B:alex", description="音色：模型:音色名，也可只写音色名")
    speed: float = Field(default=1.0, ge=0.25, le=4.0, description="语速 [0.25, 4.0]")
    gain: float = Field(default=0.0, ge=-10.0, le=10.0, description="增益 dB [-10, 10]")
    response_format: Literal["mp3", "wav", "opus"] = Field(
        default="mp3",
        description="音频格式",
        json_schema_extra={"enum": ["mp3", "wav", "opus"], "enum_titles": ["MP3", "WAV", "Opus"]},
    )
    sample_rate: int = Field(default=44100, description="采样率；合法性随 response_format 变化（见 §8.3）")
    keep_text: bool = Field(default=True, description="语音之外是否保留原文字（回复扩展参数缺省时的默认值）")
    auto_voice: bool = Field(default=False, description="true 时默认给回复加语音（不依赖 Planner 选参数）")
    max_chars: int = Field(default=300, ge=1, description="单次合成文本上限（字符数），按句边界截断")
    timeout_ms: int = Field(default=60000, ge=1000, description="单次回复扩展超时，与 @ReplyExtension(timeout_ms=) 对齐")
    retry: int = Field(default=2, ge=0, le=5, description="429/5xx 重试次数")
    cache_enabled: bool = Field(default=True, description="音频缓存开关")
    cache_ttl_hours: int = Field(default=72, ge=0, description="缓存过期小时数；0 表示不过期")
    cache_max_mb: int = Field(default=200, ge=0, description="缓存上限 MB，超出按 LRU 淘汰；0 表示不限制")
    fallback_on_error: Literal["text", "fail"] = Field(
        default="text",
        description="失败策略：text=降级为纯文字；fail=让本次 reply 失败（仅联调）",
        json_schema_extra={"enum": ["text", "fail"], "enum_titles": ["降级为文字", "让回复失败"]},
    )
```

派生只读属性（同一模块内提供，名字冻结）：

```python
@property
def effective_api_key(self) -> str: ...   # api_key or os.environ["SILICONFLOW_API_KEY"] or ""
@property
def effective_voice(self) -> str: ...     # 只写音色名时自动补成 f"{model}:{voice}"
@property
def timeout_seconds(self) -> float: ...   # timeout_ms / 1000
```

### 3.2 `provider.py`

```python
from dataclasses import dataclass

class ProviderError(RuntimeError):
    """统一的 TTS 调用错误。"""
    kind: str          # "config" | "auth" | "bad_request" | "rate_limit" | "server" | "network" | "timeout" | "empty"
    status: int | None
    def __init__(self, message: str, *, kind: str, status: int | None = None) -> None: ...


@dataclass(frozen=True, slots=True)
class TTSSettings:
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
    def from_config(cls, cfg: "SiliconFlowTTSConfig") -> "TTSSettings": ...


class SiliconFlowTTSProvider:
    def __init__(self, settings: TTSSettings) -> None: ...
    async def synthesize(self, text: str, *, timeout: float | None = None) -> bytes: ...
    async def list_voices(self, *, timeout: float | None = None) -> list[str]: ...
    async def aclose(self) -> None: ...


def create_provider(cfg: "SiliconFlowTTSConfig") -> SiliconFlowTTSProvider: ...
```

语义（冻结）：

- `synthesize`：输入**已清洗**的文本（`text.prepare` 之后），返回**原始音频字节**（未转码、未 base64）。
  空文本 → 抛 `ProviderError(kind="empty")`；`api_key` 为空 → `ProviderError(kind="config")`。
- `timeout=None` 时用 `settings.timeout`（秒）。
- 重试只针对 `429` 与 `5xx`，次数 `settings.retry`，退避 `0.5 * 2**n` 秒；`4xx` 不重试。
- 错误归一化：`401/403 → auth`、`400/422 → bad_request`、`429 → rate_limit`、`5xx → server`、
  连接/超时 → `network` / `timeout`；HTTP 200 但响应体为空或长度为 0 → `empty`。
- `list_voices`：`GET {base_url}/audio/voice/list`，返回 `uri` 列表；失败按上面同样归一化。
- **不依赖 SDK**，可 `pytest` 直接测；所有 HTTP 走 `httpx.AsyncClient`（`trust_env` 保持默认）。

### 3.3 `cache.py`

```python
def make_key(
    *,
    text: str,
    model: str,
    voice: str,
    speed: float,
    gain: float,
    response_format: str,
    sample_rate: int,
) -> str: ...


class AudioCache:
    def __init__(
        self,
        root: "Path",
        *,
        ttl_hours: int = 72,
        max_mb: int = 200,
        enabled: bool = True,
    ) -> None: ...

    def make_key(self, **kwargs) -> str: ...      # 委托模块级 make_key，保证同一实现
    def get(self, key: str) -> bytes | None: ...  # 命中返回值，未命中/过期返回 None
    def put(self, key: str, data: bytes) -> None: # 原子写入 + 触发 TTL/LRU 清理
    def clear(self) -> int: ...                   # 删除全部缓存文件，返回删除数量
    def stats(self) -> dict[str, int | str]: ...  # {"count", "bytes", "root"}
```

语义（冻结）：

- `make_key` = `sha256(f"{model}|{voice}|{speed:.3f}|{gain:.3f}|{response_format}|{sample_rate}|{text}")` 的十六进制串（见 §7）。
- 文件布局：`<root>/audio/<key[:2]>/<key>.<fmt>`；扩展名取 `response_format`。
- `get`：读文件，按 `ttl_hours` 判断过期（`mtime` 超期即删并返回 `None`）；`enabled=False` 时恒返回 `None`。
- `put`：先写 `<key>.<fmt>.tmp` 再 `os.replace` 原子替换；`enabled=False` 时直接返回。
- `max_mb > 0` 时，`put` 之后按 `mtime` 升序淘汰到上限以内。
- 全部 IO 为同步实现，调用方用 `asyncio.to_thread` 包（见 §5.2）。

### 3.4 `text.py`

```python
def prepare(text: str, max_chars: int) -> str: ...
def strip_markdown(text: str) -> str: ...          # 去代码块/列表符/强调/链接
def strip_mentions(text: str) -> str: ...          # 去 @xxx、[CQ:...]、(xx) 动作括号
def split_sentences(text: str) -> list[str]: ...   # 中文 。！？\n 与英文 .!? 断句
def truncate_sentences(text: str, max_chars: int) -> str: ...  # 首段+末段策略
```

语义（冻结，对应 PLAN §6.3）：

- `prepare` 顺序：`strip_markdown` → `strip_mentions` → 压缩空白（不插入空格，官方要求 `input` 不加空格）→
  `truncate_sentences`。`max_chars <= 0` 视为不限制。
- 截断：按句边界累计到 `max_chars` 内；超长时保留**首句 + 末句**（中间以 `……` 连接），结果长度仍需 ≤ `max_chars`
  （若首末句相加超限，则只保留首句的 `max_chars` 前缀）。
- 空输入或清洗后为空：返回 `""`（调用方据此直接降级，不调 API）。
- 不做网络、不做 IO、不依赖 SDK。

### 3.5 `audio.py`

```python
SUPPORTED_FORMATS: tuple[str, ...] = ("mp3", "wav", "opus")
SAMPLE_RATES: dict[str, tuple[int, ...]] = {
    "mp3": (32000, 44100),
    "wav": (8000, 16000, 24000, 32000, 44100),
    "opus": (48000,),
}

def normalize_format(fmt: str) -> str: ...                 # 小写化 + 别名（mpeg→mp3）；未知抛 ValueError
def resolve_sample_rate(fmt: str, sample_rate: int) -> int: ...  # 不合法时回落到默认值并记录
def ensure_compatible(data: bytes, fmt: str) -> bytes: ...  # 当前实现：校验非空 + 格式白名单，原样返回
def to_base64(data: bytes) -> str: ...                      # base64.b64encode(...).decode("ascii")
```

语义（冻结，对应 PLAN §6.6）：

- `ensure_compatible` 的职责是**兜底闸门**：`data` 为空 → 抛 `ValueError("empty audio")`；`fmt` 不在白名单 → 抛 `ValueError`；
  当前直接返回 `data`（不做重编码）。未来接 silk/ffmpeg 时**只改本函数**，签名不变。
- `resolve_sample_rate` 按 §8.3 表格判断，非法值回落到该格式默认值（mp3/wav 44100，opus 48000），并把非法值写日志。
- 不依赖 SDK。

### 3.6 `reply_ext.py` + `voice` 组件

组件由 `plugin.py` 中的方法声明（`reply_ext.py` 提供纯函数，便于单测）：

```python
# reply_ext.py（无 SDK 依赖的纯逻辑）
VOICE_EXTENSION_NAME: str = "voice"
PREPARE_EXTRA_PROMPT: str = (
    "这次回复会转成语音朗读，请用适合朗读的自然口语，不要用 Markdown、列表、代码块、颜文字和括号动作。"
)
KEEP_TEXT_KEY: str = "keep_text"
ATTACH_VOICE_KEY: str = "attach_voice"

def extension_parameters() -> dict[str, dict[str, object]]: ...
def build_messages(
    messages: list[dict[str, object]],
    *,
    text: str,
    audio_base64: str,
    keep_text: bool,
    quote_previous: bool,
) -> list[dict[str, object]]: ...
```

`plugin.py` 中的声明（冻结：组件名 `voice`、平台 `qq`、超时跟配置）：

```python
@ReplyExtension(
    VOICE_EXTENSION_NAME,                      # "voice"
    description="需要语音回复时启用：把本次回复合成语音，可选择是否保留文字",
    parameters={
        ATTACH_VOICE_KEY: {"type": "boolean", "default": True, "description": "是否合成语音"},
        KEEP_TEXT_KEY: {"type": "boolean", "default": True, "description": "是否同时保留文字"},
    },
    platforms="qq",
    timeout_ms=60000,   # 运行期由 configure 用 self.config.timeout_ms 覆盖：见下方说明
)
async def reply_voice(self, *, phase, parameters, text, messages, **context) -> dict[str, object]:
    ...
```

- `timeout_ms` 是装饰器静态值，**默认 60000**；配置更大时不改装饰器（Host 已按 60s 计时），
  插件必须在 `min(config.timeout_ms/1000, 55s)` 内自设 provider 超时，避免被 Host 先掐断。
- 处理器签名固定为关键字参数：`phase`、`parameters`、`text`、`messages`、`**context`。
- `prepare` 返回 `{"extra_prompt": PREPARE_EXTRA_PROMPT}`；`before_send` 返回 `{"messages": [...]}`。
- `parameters[ATTACH_VOICE_KEY]` 为 `False` 或插件 `enabled=False` → 返回 `{}`（完全放行）。
- 处理器内**禁止**调用 `self.ctx.send.*`（主程序统一发送，见 PLAN §4.3）。

### 3.7 `plugin.py` 装配面

```python
class SiliconFlowTTSPlugin(MaiBotPlugin):
    config_model = SiliconFlowTTSConfig

    # on_load 建立：
    self.provider: SiliconFlowTTSProvider
    self.audio_cache: AudioCache
    # on_config_update("self") 重建 provider 配置（缓存实例可复用）

    async def _synthesize(self, text: str) -> bytes: ...
    def _make_messages(self, messages, *, text, audio, keep_text) -> list[dict]: ...
```

- `_synthesize` 是唯一「文本 → 音频」入口，顺序：`text.prepare` → `cache.get` → `provider.synthesize` →
  `audio.ensure_compatible` → `cache.put`，任意一步失败按 §5.3 处理。
- 日志统一 `self.ctx.logger`；缓存命中/失败降级各打一条（INFO/WARNING），不打印 `api_key`。

### 3.8 `commands.py`（可选，M3 再做）

命令组前缀 `/tts`，至少 `voices` / `status` / `on` / `off`；命令落库走 `self.ctx.config`（只读），开关状态落到
`self.ctx.paths.data_dir`。命令处理器不改变本文任何冻结签名。

---

## 4. 配置模型字段表（= PLAN 第 5 节，逐项对齐）

`config.py` 声明两个类：外层 `SiliconFlowTTSConfig(PluginConfigBase)` + 内部 `[plugin]` 段 `SiliconFlowTTSSection(PluginConfigBase)`。
下方 19 个字段**全部位于 `[plugin]` 段内**；外层用只读转发属性维持 `self.config.speed` 的访问面（不是 `self.config.plugin.speed`，也**不是**扁平顶层——扁平顶层会让插件加载失败，见 §3.1 订正）。
Runner 依据它生成 `config.toml` 与 WebUI Schema，并自动补齐缺失字段。

| # | 键 | 类型 | 默认 | 约束 | 说明（PLAN §5） |
|---|---|---|---|---|---|
| 0 | `config_version` | `str` | `"1.0.0"` | — | Runner 维护，插件不读写 |
| 1 | `enabled` | `bool` | `true` | — | 组件总开关 |
| 2 | `api_key` | `str` | `""` | — | 空则回落环境变量 `SILICONFLOW_API_KEY` |
| 3 | `base_url` | `str` | `https://api.siliconflow.cn/v1` | 去尾 `/` | 便于指向自建反代 |
| 4 | `model` | `str` | `FunAudioLLM/CosyVoice2-0.5B` | — | TTS 模型 |
| 5 | `voice` | `str` | `FunAudioLLM/CosyVoice2-0.5B:alex` | — | 完整 `模型:音色`；只写音色名时自动补齐 |
| 6 | `speed` | `float` | `1.0` | `0.25 ≤ x ≤ 4.0` | 语速 |
| 7 | `gain` | `float` | `0.0` | `-10 ≤ x ≤ 10` | 增益 dB |
| 8 | `response_format` | `Literal` | `"mp3"` | `mp3`/`wav`/`opus` | 输出格式 |
| 9 | `sample_rate` | `int` | `44100` | 见 §8.3，非法值回落默认 | 采样率 |
| 10 | `keep_text` | `bool` | `true` | — | 语音之外是否保留原文字 |
| 11 | `auto_voice` | `bool` | `false` | — | **占位**：ReplyExtension 无法强制参与，M2 由 Hook 注入 options 才生效（PLAN R8） |
| 12 | `max_chars` | `int` | `300` | `≥ 1` | 超长截断（按句边界，见 §3.4） |
| 13 | `timeout_ms` | `int` | `60000` | `≥ 1000` | 与 `@ReplyExtension(timeout_ms=)` 对齐 |
| 14 | `retry` | `int` | `2` | `0 ≤ x ≤ 5` | 429/5xx 重试次数 |
| 15 | `cache_enabled` | `bool` | `true` | — | 缓存开关 |
| 16 | `cache_ttl_hours` | `int` | `72` | `≥ 0` | 过期清理，0=不过期 |
| 17 | `cache_max_mb` | `int` | `200` | `≥ 0` | 超限 LRU，0=不限 |
| 18 | `fallback_on_error` | `Literal` | `"text"` | `text`/`fail` | 失败降级策略 |

字段顺序即 WebUI 展示顺序（与上表一致）。`Field` 只用 `default` / `ge` / `le` / `description` / `json_schema_extra`，
不要写自定义 validator（Runner 的 Schema 生成器只认 Pydantic 标准元数据）。

`config.toml` 形态（Runner 生成，供排错对照）——**必须有 `[plugin]` 段**，平铺会加载失败：

```toml
[plugin]
config_version = "1.0.0"
enabled = true
api_key = ""
base_url = "https://api.siliconflow.cn/v1"
model = "FunAudioLLM/CosyVoice2-0.5B"
voice = "FunAudioLLM/CosyVoice2-0.5B:alex"
speed = 1.0
gain = 0.0
response_format = "mp3"
sample_rate = 44100
keep_text = true
auto_voice = false
max_chars = 300
timeout_ms = 60000
retry = 2
cache_enabled = true
cache_ttl_hours = 72
cache_max_mb = 200
fallback_on_error = "text"
```

---

## 5. 数据流与失败语义

### 5.1 两阶段

```
reply 工具 plugin_options["local.siliconflow-tts.voice"] = {attach_voice: true, keep_text: true}
  ↓ prepare  → {"extra_prompt": PREPARE_EXTRA_PROMPT}
Replyer 生成正文 → Hook 处理
  ↓ before_send(text, messages)
text.prepare → cache.get → provider.synthesize → audio.ensure_compatible → cache.put → base64
  ↓ {"messages": [原消息(可能去文字段), voice 段]}
主程序发送 / 记历史 / 监控
```

### 5.2 线程与超时

| 环节 | 规则 |
|---|---|
| 缓存 IO | `await asyncio.to_thread(cache.get/put, ...)`，不阻塞事件循环 |
| provider 超时 | `min(self.config.timeout_ms / 1000, 55.0)` 秒 |
| 重试总预算 | 不超过 provider 超时；每次重试前检查剩余时间 |
| 并发 | 每个 reply 独立；provider 单实例、`httpx.AsyncClient` 复用，`on_unload` 调 `aclose()` |

### 5.3 失败矩阵（冻结）

| 情况 | `fallback_on_error="text"`（默认） | `fallback_on_error="fail"` |
|---|---|---|
| `api_key` 为空 / `enabled=false` | WARN 日志，返回 `{}`（原消息不动） | 返回 `{}`（配置问题不算联调目标） |
| `text.prepare` 结果为空 | 返回 `{}` | 返回 `{}` |
| `ProviderError`（任何 kind） | WARN 日志，返回 `{}` | **抛出**，整次 reply 失败 |
| 音频为空 / `ValueError` | 同上 | 抛出 |
| 缓存写失败 | 只记 DEBUG，不影响发送 | 同左 |
| 非预期异常 | `logger.exception`，返回 `{}` | 抛出 |

要点：**`fallback_on_error="text"` 时 `before_send` 绝不向上抛异常**——Host 里任一扩展抛错会让整次 reply 失败（含文字）。
返回 `{}` 表示「保留原样」，这是唯一正确的降级动作。

### 5.4 `auto_voice`（M2 后续）

`ReplyExtension` 只在 Planner 填了 `plugin_options` 时被调用，**扩展自身无法强制参与**。
因此 `auto_voice=true` 的落地方式是：另注册一个 Hook（`reply` 阶段）把 `plugin_options` 补上
`{"local.siliconflow-tts.voice": {"attach_voice": true}}`；Hook 不改变本契约任何签名。
M1 阶段 `auto_voice` 仅作为配置项存在，不生效（`on_load` 打一条 INFO 说明）。

---

## 6. 消息与语音段协议（Host 侧强校验）

`before_send` 返回的 `messages` 会被 Host `validate_reply_messages()` 逐条校验，规则如下（实测源码）：

1. `messages` 必须是**非空列表**；空列表 → 整次 reply 失败。
2. 每条消息必须**恰好**包含 `segments` 与 `quote_previous` 两个键（`set(message) == {"segments","quote_previous"}`），
   不允许多余键；`quote_previous` 必须是 `bool`；`segments` 必须是非空列表。
3. 消息段类型白名单：`text` / `image` / `emoji` / `voice` / `at` / `reply` / `file` / `forward` / `dict`。
4. `voice` 段要求：`data` 必须是 `str`；`binary_data_base64` 必须是**合法且非空**的 Base64
   （`base64.b64decode(x, validate=True)` 非空）。

冻结的语音段构造：

```python
voice_message = {
    "segments": [
        {
            "type": "voice",
            "data": spoken_text,                                  # 已清洗、送 TTS 的文本
            "binary_data_base64": audio.to_base64(audio_bytes),   # 非空合法 base64
        }
    ],
    "quote_previous": False,
}
```

文字段处理：

- `keep_text=True`：原消息的 `text` 段保留（**保留原顺序、原 `quote_previous`**），语音段**追加在最后**。
- `keep_text=False`：原消息中去掉 `text` 段；若某条消息去掉后 `segments` 为空，则**整条丢弃**（不能返回空 segments）。
- 原消息里的图片/表情/@/引用段一律保留，不得改写。
- 第一条消息沿用 reply 的 `set_quote`（插件不改 `quote_previous` 的既有值）；语音段固定 `quote_previous=False`。

平台：扩展声明 `platforms="qq"`，非 QQ 平台不会被调用；处理器内部不再重复判断平台（可读 `context["chat"]["platform"]` 记日志）。

---

## 7. 缓存键与文件

```
key = sha256( f"{model}|{voice}|{speed:.3f}|{gain:.3f}|{response_format}|{sample_rate}|{text}" )
path = <data_dir>/audio/<key[:2]>/<key>.<response_format>
```

- `text` 传 `text.prepare()` 之后的文本（保证「同一句话、不同原始 Markdown」命中同一缓存）。
- 语速、音色、模型、格式、采样率任一变化 → 不同 key（避免「换音色还放旧声音」）。
- 过期：`mtime + ttl_hours*3600 < now` → 命中时删除并返回 `None`。
- 容量：`put` 后按 `mtime` 升序删除到 `max_mb` 以内；单文件上限 `min(20MB, max_mb)`，超限不落盘（只记 WARNING）。
- 并发：同一 key 同时写时用 `os.replace` 保证原子性；读失败（不存在/损坏）一律当未命中。

---

## 8. 硅基流动 HTTP 契约

### 8.1 请求

```
POST {base_url}/audio/speech
Authorization: Bearer {api_key}
Content-Type: application/json

{
  "model": "FunAudioLLM/CosyVoice2-0.5B",
  "voice": "FunAudioLLM/CosyVoice2-0.5B:alex",
  "input": "<text.prepare 结果，不加空格>",
  "speed": 1.0,
  "gain": 0.0,
  "response_format": "mp3",
  "sample_rate": 44100
}
```

- 响应体即音频二进制，`Content-Type` 可能是 `audio/mpeg` / `application/octet-stream`，**按字节处理**，不解析。
- `base_url` 统一 `rstrip("/")`；不复用 `openai` SDK（避免额外依赖）。

### 8.2 错误映射

| HTTP / 异常 | `ProviderError.kind` | 是否重试 |
|---|---|---|
| 401 / 403 | `auth` | 否 |
| 400 / 404 / 422 | `bad_request` | 否 |
| 429 | `rate_limit` | 是（最多 `retry` 次，读 `Retry-After`） |
| 5xx | `server` | 是 |
| `httpx.TimeoutException` | `timeout` | 是（计入 retry） |
| `httpx.TransportError` | `network` | 是 |
| 200 且 `len(body)==0` | `empty` | 否 |

错误消息里包含状态码与响应体前 300 字符，**必须脱敏 `Authorization` 与 `api_key`**。

### 8.3 采样率合法值（官方文档）

| `response_format` | 合法 `sample_rate` | 默认 |
|---|---|---|
| `mp3` | 32000、44100 | 44100 |
| `wav` | 8000、16000、24000、32000、44100 | 44100 |
| `opus` | 48000（唯一） | 48000 |

配置值非法时由 `audio.resolve_sample_rate()` 回落默认并记 WARNING，不阻断合成。

### 8.4 音色

- `voice` 支持 `模型:音色名`（如 `FunAudioLLM/CosyVoice2-0.5B:alex`）或动态音色 `uri`（`speech:...`）。
- 只写音色名（`alex`）时补成 `f"{model}:{voice}"`；已含 `:` 或 `speech:` 前缀则原样使用。
- `/tts voices` 用 `list_voices()`，失败时返回 `[]` 并提示配置问题。

---

## 9. 测试契约

- `tests/` 用 `pytest`；被测模块按**包语义**加载，避免相对导入失败：

```python
# tests/conftest.py
import importlib.util, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = "_tts_under_test"

def load_plugin_package() -> None:
    if PKG in sys.modules:
        return
    spec = importlib.util.spec_from_file_location(
        PKG, str(ROOT / "plugin.py"), submodule_search_locations=[str(ROOT)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[PKG] = module
    spec.loader.exec_module(module)
```

- 必须覆盖（PLAN §10）：provider 的错误分支（auth/rate_limit/server/empty/network）、cache 命中/过期/淘汰、
  text 切分与截断边界（0/1/正好 max_chars/超长）、audio 采样率回落、`build_messages` 的 `keep_text` 两种分支与
  「去文字后空 segments 丢弃整条」。
- 不联网：provider 测试用 `httpx.MockTransport` 或本地 `http.server`；不得真实调用硅基流动。

---

## 10. 交付自检清单

`provider-dev`（`provider.py` / `cache.py` / `text.py` / `tests/`）：

- [ ] `create_provider(cfg)` 返回可复用实例；`synthesize` 返回 `bytes`
- [ ] `make_key` 七个字段全参与，`text` 用清洗后文本
- [ ] `Cache.get/put` 原子写、TTL、LRU、`enabled=False` 直通
- [ ] `text.prepare` 无空格输出、句边界截断、空输入返回 `""`
- [ ] 四个模块顶层无 `maibot_sdk` 导入；`pytest` 全绿

`plugin-dev`（`plugin.py` / `config.py` / `reply_ext.py` / `audio.py`）：

- [ ] `config_model = SiliconFlowTTSConfig`，字段与 §4 一一对应，顺序一致
- [ ] 三个生命周期方法均覆盖且签名一致；`create_plugin()` 存在
- [ ] `@ReplyExtension("voice", ..., platforms="qq")` 参数 Schema 与 §3.6 一致
- [ ] `before_send` 返回的 `messages` 通过 §6 全部校验（键集合、Base64、非空 segments）
- [ ] `fallback_on_error="text"` 下任何异常都不外抛
- [ ] 插件根无 `__init__.py`，全部相对导入
- [ ] `python -m json.tool _manifest.json` 通过；空 `api_key` 时降级不崩

---

## 11. 变更流程

1. 本文任何**冻结名称/签名/默认值**的改动，先在 `docs/CONTRACT.md` 提 PR 并 @architect，升版本号（v1.x）。
2. 只改内部实现、不改公开面的，无需改本文；但要在 PR 描述里写明「不影响契约」。
3. `_manifest.json` 的 `id`、`capabilities`、`sdk.min_version` 属于冻结项，改动必须同步本文与 PLAN。
