# 插件契约（v1.2 · 冻结）

> 适用版本：`local.siliconflow-tts` 0.1.x
> 状态：**冻结**。`provider-dev` 与 `plugin-dev` 按本文实现，公开名称、参数名、返回类型一律照抄；
> 需要改动先找 architect 改本文并升版本，不得在代码里自行改名。
>
> 依据：`docs/PLAN.md` + 官方文档（[插件开发指南](https://docs.mai-mai.org/plugin/)、[Manifest](https://docs.mai-mai.org/plugin/manifest)、
> [配置](https://docs.mai-mai.org/plugin/config)、[回复扩展](https://docs.mai-mai.org/plugin/reply-extensions)、
> [硅基流动 TTS](https://docs.siliconflow.cn/docs/userguide/capabilities/text-to-speech)、
> [上传参考音频](https://docs.siliconflow.cn/docs/api/uploads-audio-voice-post)、
> [音色列表](https://docs.siliconflow.cn/docs/api/audio-voice-list-get)、
> [删除参考音频](https://docs.siliconflow.cn/docs/api/audio-voice-deletions-post)）
> + 运行实例实测（见 §1.4）。

## 修订记录

| 版本 | 变更 |
|---|---|
| v1.0 | 初版冻结：manifest、模块签名、19 字段配置表、消息协议 |
| v1.1 | **配置结构订正**（t3 实测）：`config.toml` 必须用 `[plugin]` 段，扁平顶层会让插件加载失败；外层用转发属性维持 `self.config.speed` 访问面（§3.1/§4）。`auto_voice` 语义按 PLAN R8 修正 |
| v1.2 | **本期范围扩展**：音色克隆（克隆/列表/删除）新增 `voice.py`；`voice` 配置接受克隆 uri；`provider.list_voices` 降级为兼容薄壳；新增 §12 密钥安全红线，`.gitignore` 扩容，同步 PLAN §7/§10/§11 |
| v1.3 | **配置表补齐**：`[plugin]` 段新增 4 个语音开关字段（`group_policy`/`group_list`/`private_policy`/`private_list`，默认 `whitelist` + 空列表 = 全关），字段总数 19 → 23；`reply_ext` 新增 `normalize_peer_id` / `resolve_chat_peer` / `is_allowed` 三个纯函数签名；明确 `prepare` 与 `before_send` 两个 phase 都先过闸门（§3.7） |

---

## 0. 冻结清单（TL;DR）

| 项 | 冻结值 |
|---|---|
| 插件 ID | `local.siliconflow-tts` |
| 插件版本 | `0.1.0`（严格三段式） |
| Manifest | `manifest_version=2`，见 [_manifest.json](../_manifest.json) |
| SDK 区间 | `min_version=2.14.0` / `max_version=999.999.999` |
| 能力声明 | `send.hybrid`、`config.get`（**音色克隆不新增能力**，理由见 §2.2） |
| 回复扩展组件名 | **`voice`**，完整命名空间 `local.siliconflow-tts.voice`（Host 拼接） |
| 配置模型类 | `config.SiliconFlowTTSConfig`（外层）+ `config.SiliconFlowTTSSection`（`[plugin]` 段，23 字段）；对外仍暴露 `self.config.speed` 等转发属性 —— **不可用扁平顶层写法**，见 §3.1 订正 |
| 插件类 | `plugin.SiliconFlowTTSPlugin`（`MaiBotPlugin` 子类），入口 `create_plugin()` |
| 必实现生命周期 | `on_load()` / `on_unload()` / `on_config_update(scope, config_data, version)` |
| 数据目录 | `self.ctx.paths.data_dir`（= `<MaiBot>/data/plugins/local.siliconflow-tts`），**不自己拼路径** |
| 音色管理模块 | **`voice.py`**（v1.2）：克隆 / 列表 / 删除，低频运维路径，不进入合成热路径 |
| 参考音频目录 | `<data_dir>/voices/`（只读单层文件名，见 §3.6） |
| HTTP 客户端 | `httpx`（容器内实测 0.28.1，manifest 已声明 `>=0.27.0`） |
| 密钥来源 | `config.toml` 的 `api_key` 或环境变量 `SILICONFLOW_API_KEY`，**红线见 §12** |

### 0.1 「契约写法」→「实际定义」对照

本文用简写描述公开 API，落地时按此表对应，**名字一个字符都不要改**：

| 契约写法 | 实际定义 |
|---|---|
| `provider.synthesize(text, *, timeout)` | `SiliconFlowTTSProvider.synthesize(self, text, *, timeout=None)`，`async`，返回 `bytes` |
| `provider.request_json(method, path, *, payload, timeout)` | `SiliconFlowTTSProvider.request_json(...)`，`async`，返回解析后的 JSON 对象（v1.2 新增，`voice.py` 复用） |
| `provider.list_voices(*, timeout)` | `list[str]`，**v1.2 起仅作兼容薄壳**，内部委托 `voice.list_voices`（下个版本移除） |
| `voice.clone_voice(provider, *, name, reference_text, ...)` | `voice.clone_voice(provider, *, name, reference_text, audio_path=None, audio_bytes=None, mime="audio/mpeg", timeout=None) -> VoiceProfile` |
| `voice.list_voices(provider, *, timeout)` | `voice.list_voices(provider, *, timeout=None) -> list[VoiceProfile]`（**唯一实现**） |
| `voice.delete_voice(provider, *, uri, timeout)` | `voice.delete_voice(provider, *, uri, timeout=None) -> bool` |
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
├── config.py          # 配置模型（唯一配置真源；[plugin] 段 + 转发属性）
├── provider.py        # 硅基流动 TTS HTTP 客户端（合成热路径 + 共用 request_json）
├── voice.py           # 音色管理：克隆 / 列表 / 删除（v1.2 新增，低频运维）
├── cache.py           # 音频缓存
├── text.py            # 文本准备
├── audio.py           # 音频格式决策
├── reply_ext.py       # ReplyExtension 处理逻辑（被 plugin.py 引用）
├── commands.py        # /tts 命令组
├── docs/
└── tests/
```

- **禁止** `__init__.py`；**禁止**嵌套子包（`maibot_plugin_siliconflow_tts/` 这种目录不存在）。
- 模块之间一律**相对导入**：`from .config import SiliconFlowTTSConfig`、`from .provider import create_provider`、
  `from . import voice`。
- 依赖方向（单向，禁止反向）：`plugin` → 其余；`reply_ext` → `provider/cache/text/audio`；
  `voice` → `provider`（拿 `settings` / `request_json` / `ProviderError`）；
  `provider` **只在 `list_voices` 兼容薄壳里懒导入** `voice`（函数内 `from . import voice`），模块顶层不依赖 voice。
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
| 运行时配置 | 插件根 `config.toml`（Runner 生成/维护，仓库 `.gitignore` 已忽略；**含 `[plugin]` 段**） |
| 缓存/状态 | `self.ctx.paths.data_dir`（`<MaiBot>/data/plugins/local.siliconflow-tts`，Runner 保证存在） |
| 音色参考音频 | `<data_dir>/voices/`（插件在 `on_load` 里 `mkdir(parents=True, exist_ok=True)`；只读单层文件名） |
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
| 配置版本校验 | `extract_plugin_config_version()` 只认 `{"plugin": {"config_version": ...}}`，缺失 → 加载失败（见 §3.1 订正） |
| 出站语音 | Host 把 `voice` 段 + `binary_data_base64` 交给适配器，适配器编码成 OneBot v11 `record` |
| 能力写接口 | **没有** `config.set` 一类写能力 → 运行期不能改 `voice` 配置，改音色走 WebUI（见 §3.9） |

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

### 2.2 为什么音色克隆不新增能力（v1.2）

三条链路全部是**插件自己的出站 HTTP + 本地文件读取**，不碰宿主数据：

| 动作 | 依赖 | 需要新能力吗 |
|---|---|---|
| 克隆（上传参考音频） | 读 `<data_dir>/voices/<file>` + `POST /uploads/audio/voice` | **不需要**（`data_dir` 由 Runner 授予，不是「读别人文件」） |
| 列表 | `GET /audio/voice/list` | 不需要 |
| 删除 | `POST /audio/voice/deletions` | 不需要 |

**只有**将来要实现「直接读 QQ 消息里的语音附件当参考音频」才需要 `message.*`（如 `message.get_by_id`）与语音二进制取用能力；
本期不做这条路——宿主 `message.*` 对语音附件不保证给二进制，且会多一层隐私与权限风险。因此 `capabilities` **保持两项不变**。

### 2.3 校验规则（Host `ManifestValidator`，Pydantic 严格模式）

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
> `provider.py`、`voice.py`、`cache.py`、`text.py`、`audio.py` 必须能被 pytest 在无 SDK 环境导入。

### 3.1 `config.py`

> **⚠️ v1.1 订正（t3 实测，SDK/Runner 2.16.0）**
>
> 本节 v1.0 原写「扁平单层、`config_version` 放顶层」，**这是错的**。Runner 的
> `extract_plugin_config_version()` 只认 `{"plugin": {"config_version": ...}}`；
> 缺失时抛 `PluginConfigVersionError`，而 `_apply_plugin_config(suppress_errors=False)`
> 会把它直接抛出 → **插件加载失败**。
>
> 正确写法：`config.toml` 用标准 `[plugin]` 段（23 字段全在段内，`config_version` 必填），
> 外层模型用**只读转发属性**维持契约冻结的 `self.config.speed` / `self.config.enabled` 访问面，
> 下游模块无需感知嵌套。实际实现见 `config.py`。

```python
from typing import Literal

from maibot_sdk import Field, PluginConfigBase


class SiliconFlowTTSSection(PluginConfigBase):
    """``config.toml`` 的 ``[plugin]`` 段（字段见 §4）。"""

    __ui_label__ = "硅基流动 TTS"
    __ui_icon__ = "audio-lines"
    __ui_order__ = 0

    config_version: str = Field(default="1.0.0", description="配置版本，由 Runner 维护，插件不要读写")
    enabled: bool = Field(default=True, description="总开关；false 时回复扩展直接放行原消息")
    api_key: str = Field(default="", description="硅基流动 API Key；留空则回落到环境变量 SILICONFLOW_API_KEY")
    base_url: str = Field(default="https://api.siliconflow.cn/v1", description="API 根地址，可指向自建反代")
    model: str = Field(default="FunAudioLLM/CosyVoice2-0.5B", description="TTS 模型名")
    voice: str = Field(
        default="FunAudioLLM/CosyVoice2-0.5B:alex",
        description="音色：模型:音色名，或克隆音色 uri（speech:...）；只写音色名时自动补模型前缀",
    )
    speed: float = Field(default=1.0, ge=0.25, le=4.0, description="语速 [0.25, 4.0]")
    gain: float = Field(default=0.0, ge=-10.0, le=10.0, description="增益 dB [-10, 10]")
    response_format: Literal["mp3", "wav", "opus"] = Field(
        default="mp3",
        description="音频格式",
        json_schema_extra={"enum": ["mp3", "wav", "opus"], "enum_titles": ["MP3", "WAV", "Opus"]},
    )
    sample_rate: int = Field(default=44100, description="采样率；合法性随 response_format 变化（见 §8.3）")
    keep_text: bool = Field(default=True, description="语音之外是否保留原文字（回复扩展参数缺省时的默认值）")
    auto_voice: bool = Field(default=False, description="true 时默认给回复加语音（当前版本尚未接入 Hook，仅占位）")
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


class SiliconFlowTTSConfig(PluginConfigBase):
    """外层模型：只声明 ``plugin`` 一个字段，其余用只读转发属性。"""

    plugin: SiliconFlowTTSSection = Field(default_factory=SiliconFlowTTSSection)
```

派生只读属性（模块内提供，名字冻结，外层逐项转发）：

```python
@property
def effective_api_key(self) -> str: ...   # api_key or os.environ["SILICONFLOW_API_KEY"] or ""
@property
def effective_voice(self) -> str: ...     # 规则见下
@property
def timeout_seconds(self) -> float: ...   # timeout_ms / 1000
```

**`effective_voice` 语义（v1.2 明确，冻结）**：

```text
raw = voice.strip()
若 raw 为空                → 返回 ""（由调用方决定默认音色）
若 raw.startswith("speech:") → 原样返回
若 ":" in raw             → 原样返回（模型:音色、其它克隆 uri scheme 都走这条）
否则（纯音色名）           → 返回 f"{model}:{raw}"
```

即：**克隆 uri 原样放过，绝不补模型前缀**；判据是「以 `speech:` 开头或含冒号」，不做其他白名单。
现有实现（`config.py`）已按此行为落地，v1.2 只是把它写进契约冻结。

### 3.2 `provider.py`

```python
from dataclasses import dataclass

class ProviderError(RuntimeError):
    """统一的 TTS / 音色管理错误。"""
    kind: str          # "config" | "auth" | "bad_request" | "rate_limit" | "server" | "network" | "timeout" | "empty"
    status: int | None
    def __init__(self, message: str, *, kind: str, status: int | None = None) -> None: ...


def redact_secret(text: str) -> str:
    """把形如 sk-xxxxxxxx 的密钥片段替换为 sk-****；用于任何可能进日志/异常的字符串。"""


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
    def from_config(cls, cfg: object) -> "TTSSettings": ...


class SiliconFlowTTSProvider:
    settings: TTSSettings                      # 公开只读属性，voice.py 用它拿 model / base_url

    def __init__(
        self,
        settings: TTSSettings,
        *,
        transport: "httpx.AsyncBaseTransport | None" = None,   # 仅单测注入
        client: "httpx.AsyncClient | None" = None,             # 仅单测注入
    ) -> None: ...

    async def synthesize(self, text: str, *, timeout: float | None = None) -> bytes: ...
    async def request_json(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, object] | None = None,
        timeout: float | None = None,
    ) -> object: ...
    async def list_voices(self, *, timeout: float | None = None) -> list[str]: ...  # 兼容薄壳，v1.3 移除
    async def aclose(self) -> None: ...


def create_provider(cfg: object) -> SiliconFlowTTSProvider: ...
```

语义（冻结）：

- `synthesize`：输入**已清洗**的文本（`text.prepare` 之后），返回**原始音频字节**（未转码、未 base64）。
  空文本 → 抛 `ProviderError(kind="empty")`；`api_key` 为空 → `ProviderError(kind="config")`。
- `timeout=None` 时用 `settings.timeout`（秒）。
- 重试只针对 `429` 与 `5xx`，次数 `settings.retry`，退避 `0.5 * 2**n` 秒；`4xx` 不重试。
- 错误归一化：`401/403 → auth`、`400/422 → bad_request`、`429 → rate_limit`、`5xx → server`、
  连接/超时 → `network` / `timeout`；HTTP 200 但响应体为空或长度为 0 → `empty`。
- **`request_json`（v1.2 新增）**：`path` 以 `/` 开头，拼 `{base_url}{path}`；带 `Authorization: Bearer`、
  `Accept: application/json`；有 `payload` 时带 `Content-Type: application/json` 并 `json=`；2xx 解析 JSON 返回
  原生 `dict`/`list`（空体返回 `None`），非 2xx 按上面同样规则抛 `ProviderError`。
  **`request_json` 不套用重试**（音色管理是低频运维，失败让用户重试即可），也不用于二进制合成。
- **`list_voices` 兼容薄壳（v1.2 降级）**：

```python
async def list_voices(self, *, timeout: float | None = None) -> list[str]:
    from . import voice                      # 函数内懒导入，避免与 voice.py 形成模块级环
    return [profile.uri for profile in await voice.list_voices(self, timeout=timeout)]
```

  保留它只为不打断已落地的 `plugin.py` 与 `tests/test_provider.py`；**新代码一律用 `voice.list_voices`**，
  v1.3 移除该薄壳。
- 错误消息必须过 `redact_secret()`，且不含请求头原文。
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

### 3.6 `voice.py`（音色管理，v1.2 新增）

**决策（v1.2）**：克隆 / 列表 / 删除三个动作**单开 `voice.py`**，不塞进 `provider.py`。
理由：合成是回复热路径（每次 reply 都跑），音色管理是低频运维（一个月几次）；
分开后 `provider.py` 的职责与测试面保持稳定，`voice.py` 可独立用 `MockTransport` 测。
两者共享的连接与鉴权通过 `provider.request_json` 复用，**不重复实现 HTTP**。

```python
from dataclasses import dataclass
from pathlib import Path

from .provider import ProviderError, SiliconFlowTTSProvider


@dataclass(frozen=True, slots=True)
class VoiceProfile:
    uri: str
    name: str
    model: str


AUDIO_SUFFIXES: tuple[str, ...] = (".mp3", ".wav", ".pcm", ".opus")
AUDIO_MIME: dict[str, str] = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".pcm": "audio/L16",
    ".opus": "audio/ogg",
}
MAX_AUDIO_BYTES: int = 10 * 1024 * 1024          # 10MB；官方建议参考音频 8–10s、<30s
VOICE_NAME_PATTERN: str = r"^[A-Za-z0-9_-]{1,64}$"


def read_reference_audio(voice_dir: Path, filename: str) -> tuple[bytes, str]: ...
async def clone_voice(
    provider: SiliconFlowTTSProvider,
    *,
    name: str,
    reference_text: str,
    audio_path: Path | None = None,
    audio_bytes: bytes | None = None,
    mime: str = "audio/mpeg",
    timeout: float | None = None,
) -> VoiceProfile: ...
async def list_voices(
    provider: SiliconFlowTTSProvider,
    *,
    timeout: float | None = None,
) -> list[VoiceProfile]: ...
async def delete_voice(
    provider: SiliconFlowTTSProvider,
    *,
    uri: str,
    timeout: float | None = None,
) -> bool: ...
```

语义（冻结）：

- `read_reference_audio(voice_dir, filename)`：只接受 `<data_dir>/voices/` 下的**单层文件名**
  （`filename` 非空、不以 `.` 开头、不含 `/`、`\`、`..`），后缀必须 ∈ `AUDIO_SUFFIXES`，大小 ≤ `MAX_AUDIO_BYTES`；
  返回 `(bytes, mime)`，mime 按 `AUDIO_MIME` 映射。越界/非法一律抛 `ValueError`（**不做路径拼接宽容**）。
  文件不存在 → `FileNotFoundError`（调用方转成「请先把参考音频放到 voices/ 目录」）。
- `clone_voice`：
  - `name` 必须匹配 `VOICE_NAME_PATTERN`，否则 `ValueError`（避免空格/冒号污染 uri）。
  - `reference_text` **必填非空**（API 必填，且直接影响克隆质量）；空则 `ValueError`。
  - `audio_bytes` 优先；否则读 `audio_path`（必须存在且 ≤ `MAX_AUDIO_BYTES`）。
  - 请求体：`{"model": provider.settings.model, "customName": name, "text": reference_text,
    "audio": f"data:{mime};base64,{b64}"}`；`POST /uploads/audio/voice`（见 §8.5）。
  - 响应 `{"uri": "speech:..."}` → `VoiceProfile(uri=..., name=name, model=provider.settings.model)`；
    缺 `uri` 或非字符串 → `ProviderError(kind="bad_request")`。
- `list_voices`：`GET /audio/voice/list`（见 §8.6）。容错解析：裸 `list`、`{"results": [...]}`、`{"data": [...]}`、
  `{"voices": [...]}` 都接受；单项缺 `uri` 跳过；结构无法识别 → 返回 `[]` 并记 WARNING
  （命令展示用，不该因解析失败让回复崩）。排序：按 `name` 升序。
- `delete_voice`：`POST /audio/voice/deletions`（见 §8.7），体 `{"uri": uri}`；2xx → `True`；404 → `False`；其余错误走 `ProviderError`。
- 参考音频「时长 < 30s」不做程序校验（不引入 ffprobe 依赖），靠文档约束；>2MB 时记一条 WARNING 提示可能过长。
- **不依赖 SDK、不读 `ctx`、不读配置**：`voice_dir` 由调用方（`plugin.py`）传入。

### 3.7 `reply_ext.py` + `voice` 组件

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

# v1.3 新增：语音开关纯函数（与 config 的 group/private 策略配合）
def normalize_peer_id(value: Any) -> str: ...                      # qq:/QQ： 前缀、全角数字、空白 → 纯数字串
def resolve_chat_peer(chat: Any) -> tuple[bool, str] | None: ...   # (是否群聊, 群号/QQ号)；解不出返回 None
def is_allowed(
    *,
    is_group: bool,
    peer_id: Any,
    group_policy: Any,
    group_list: Any,
    private_policy: Any,
    private_list: Any,
) -> bool: ...                                                     # all/whitelist/blacklist；未知策略 → False
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
- **两个 phase 都先过闸门（v1.3）**：`prepare` 与 `before_send` 均先过 `_voice_allowed(context)`
  （实现 = `is_chat_allowed(...)`；`chat` 解不出、`is_group_chat` 非布尔、策略未知/异常一律拒绝）；
  不通过则返回 `{}` **完全放行**——不注入 `extra_prompt`、不改消息。
- 处理器内**禁止**调用 `self.ctx.send.*`（主程序统一发送，见 PLAN §4.3）。

### 3.8 `plugin.py` 装配面

```python
class SiliconFlowTTSPlugin(MaiBotPlugin):
    config_model = SiliconFlowTTSConfig

    # on_load 建立：
    self.provider: SiliconFlowTTSProvider
    self.audio_cache: AudioCache
    # on_config_update("self") 重建 provider 配置（缓存实例可复用）

    async def _synthesize(self, text: str) -> bytes: ...
    def _make_messages(self, messages, *, text, audio, keep_text) -> list[dict]: ...
    @property
    def voice_dir(self) -> Path: ...   # self.ctx.paths.data_dir / "voices"（v1.2）
```

- `_synthesize` 是唯一「文本 → 音频」入口，顺序：`text.prepare` → `cache.get` → `provider.synthesize` →
  `audio.ensure_compatible` → `cache.put`，任意一步失败按 §5.3 处理。
- `voice_dir` 在 `on_load` 里 `mkdir(parents=True, exist_ok=True)`；音色管理统一走 `voice.*`，
  **不在 `plugin.py` 里拼 HTTP**。
- 日志统一 `self.ctx.logger`；缓存命中/失败降级各打一条（INFO/WARNING），不打印 `api_key`。

### 3.9 `commands.py`（v1.2 目标命令集）

命令组前缀 `/tts`。下表是 v1.2 的**目标集**；已落地的 `test` / `switch` / `help` 保留，不删除。

| 命令 | 行为 | 副作用 |
|---|---|---|
| `/tts test [文本]` | 合成一段样例语音（默认短句），回显耗时与字节数 | 出站 HTTP（计费） |
| `/tts voices` | `voice.list_voices(self.provider)`，按 `name` 升序列出 `name — uri`（最多 20 条） | 只读 |
| `/tts clone <名称> <文件名> <参考文本>` | 三个位置参数：名称/`voices/` 下文件名/参考音频对应文本 → `voice.clone_voice(...)` → 回显 `uri`，提示「把它填进 WebUI 的 voice 字段」 | 出站 HTTP + 读本地文件 |
| `/tts voice rm <uri>` | `voice.delete_voice(...)`：`True` → 已删除（提示需重新克隆才能恢复）；`False` → 未找到 | 出站 HTTP |
| `/tts status` | 打印 `enabled / model / voice / format / cache 命中数 / 最近一次错误（已脱敏）` | 只读 |
| `/tts on` / `/tts off` | 运行期开关（写 `data_dir/state.json`，不写配置） | 写 data_dir |

- 命令处理器**不得改变**本文任何冻结签名；任何失败只回一条文字提示（**不抛异常**，避免整条命令失败）。
- `voices` / `clone` / `voice rm` 的错误文案必须可执行：`auth` → 「Key 无效或未实名」、`bad_request` → 服务端原文前 200 字、
  `FileNotFoundError` → 「请先把参考音频放到 `<data_dir>/voices/`」。
- 命令回显里的 uri 不是秘密，可以原样展示；**`api_key` 一律 `sk-****`**（见 §12）。

---

## 4. 配置模型字段表（= PLAN 第 5 节，逐项对齐）

`config.py` 声明两个类：外层 `SiliconFlowTTSConfig(PluginConfigBase)` + 内部 `[plugin]` 段 `SiliconFlowTTSSection(PluginConfigBase)`。
下方 23 个字段**全部位于 `[plugin]` 段内**；外层用只读转发属性维持 `self.config.speed` 的访问面（不是 `self.config.plugin.speed`，也**不是**扁平顶层——扁平顶层会让插件加载失败，见 §3.1 订正）。
Runner 依据它生成 `config.toml` 与 WebUI Schema，并自动补齐缺失字段。

| # | 键 | 类型 | 默认 | 约束 | 说明（PLAN §5） |
|---|---|---|---|---|---|
| 0 | `config_version` | `str` | `"1.0.0"` | — | Runner 维护，插件不读写 |
| 1 | `enabled` | `bool` | `true` | — | 组件总开关 |
| 2 | `api_key` | `str` | `""` | 见 §12 | 空则回落环境变量 `SILICONFLOW_API_KEY` |
| 3 | `base_url` | `str` | `https://api.siliconflow.cn/v1` | 去尾 `/` | 便于指向自建反代 |
| 4 | `model` | `str` | `fnlp/MOSS-TTSD-v0.5` | — | TTS 模型（克隆也用这个）；`FunAudioLLM/CosyVoice2-0.5B` 当前上游不可用（会 100% 超时），勿作默认 |
| 5 | `voice` | `str` | `fnlp/MOSS-TTSD-v0.5:anna` | — | 音色：`模型:音色名` **或克隆 uri `speech:...`**；规则见 §3.1 `effective_voice`；CosyVoice2 音色当前不可用 |
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
| 19 | `group_policy` | `Literal` | `"whitelist"` | `all`/`whitelist`/`blacklist` | 群聊开关：`all`=全开、`whitelist`=仅群名单内、`blacklist`=名单外；**`whitelist` + 空列表 = 全关（刻意的安全默认）** |
| 20 | `group_list` | `list[str]` | `[]` | 每项群号 | 群号名单；允许脏输入，入库前归一化（`qq:` 前缀 / 全角数字 / 空白 / 逗号），一行一条 |
| 21 | `private_policy` | `Literal` | `"whitelist"` | `all`/`whitelist`/`blacklist` | 私聊开关：语义同 `group_policy`；**`whitelist` + 空列表 = 全关** |
| 22 | `private_list` | `list[str]` | `[]` | 每项 QQ 号 | 用户 QQ 号名单；同样支持脏输入归一化（`qq:` 前缀 / 全角数字 / 空白 / 逗号） |

字段顺序即 WebUI 展示顺序（与上表一致）。`Field` 只用 `default` / `ge` / `le` / `description` / `json_schema_extra`，
不要写自定义 validator（Runner 的 Schema 生成器只认 Pydantic 标准元数据）。

`config.toml` 形态（Runner 生成，供排错对照）——**必须有 `[plugin]` 段**，平铺会加载失败：

```toml
[plugin]
config_version = "1.0.0"
enabled = true
api_key = ""
base_url = "https://api.siliconflow.cn/v1"
model = "fnlp/MOSS-TTSD-v0.5"
voice = "fnlp/MOSS-TTSD-v0.5:anna"
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
# v1.3 语音开关：默认 whitelist + 空列表 = 全关
group_policy = "whitelist"
group_list = []
private_policy = "whitelist"
private_list = []
```

使用克隆音色时只改一行：`voice = "speech:my-voice:xxx:yyyy"`（不需要动其他字段）。

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
| 音色管理 | 命令内同步读文件用 `asyncio.to_thread`；HTTP 不重试，超时用 `timeout_seconds` |

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
返回 `{}` 表示「保留原样」，这是唯一正确的降级动作。命令处理器同理：只回文字，不外抛。

### 5.4 `auto_voice`（M2 后续）

`ReplyExtension` 只在 Planner 填了 `plugin_options` 时被调用，**扩展自身无法强制参与**（PLAN R8）。
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
- `voice` 传 **`effective_voice`**（`speech:` uri 与 `模型:音色` 直接参与哈希）→ 换克隆音色自动换 key，不会放旧声音。
- 语速、音色、模型、格式、采样率任一变化 → 不同 key。
- 过期：`mtime + ttl_hours*3600 < now` → 命中时删除并返回 `None`。
- 容量：`put` 后按 `mtime` 升序删除到 `max_mb` 以内；单文件上限 `min(20MB, max_mb)`，超限不落盘（只记 WARNING）。
- 并发：同一 key 同时写时用 `os.replace` 保证原子性；读失败（不存在/损坏）一律当未命中。

---

## 8. 硅基流动 HTTP 契约

### 8.1 合成请求

```
POST {base_url}/audio/speech
Authorization: Bearer {api_key}
Content-Type: application/json

{
  "model": "FunAudioLLM/CosyVoice2-0.5B",
  "voice": "FunAudioLLM/CosyVoice2-0.5B:alex",   // 或 "speech:..."
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

错误消息里包含状态码与响应体前 300 字符，**必须脱敏 `Authorization` 与 `api_key`**（`redact_secret()`，见 §12）。

### 8.3 采样率合法值（官方文档）

| `response_format` | 合法 `sample_rate` | 默认 |
|---|---|---|
| `mp3` | 32000、44100 | 44100 |
| `wav` | 8000、16000、24000、32000、44100 | 44100 |
| `opus` | 48000（唯一） | 48000 |

配置值非法时由 `audio.resolve_sample_rate()` 回落默认并记 WARNING，不阻断合成。

### 8.4 音色

- `voice` 支持 `模型:音色名`（如 `FunAudioLLM/CosyVoice2-0.5B:alex`）或克隆 uri（`speech:...`）。
- 只写音色名（`alex`）时补成 `f"{model}:{voice}"`；**`speech:` 开头或含 `:` 一律原样使用**（见 §3.1 `effective_voice`）。
- `/tts voices` 用 `voice.list_voices()`（v1.2 起），失败时返回 `[]` 并提示配置问题。

### 8.5 克隆音色：上传参考音频（v1.2）

```
POST {base_url}/uploads/audio/voice
Authorization: Bearer {api_key}
Content-Type: application/json

{
  "model": "FunAudioLLM/CosyVoice2-0.5B",
  "customName": "my-voice",              // ^[A-Za-z0-9_-]{1,64}$
  "text": "<参考音频里说的那句话>",       // 必填，直接影响克隆质量
  "audio": "data:audio/mpeg;base64,...." // ≤ 10MB，建议 8–10s、<30s
}
```

- 200 响应：`{"uri": "speech:my-voice:xxx:yyyy"}` → `VoiceProfile`。
- 400：常见为「音频不合规 / text 缺失 / 未实名」→ `ProviderError(kind="bad_request")`，命令回显服务端原文前 200 字。
- 401/403 → `auth`；403 也可能是**未实名认证**，命令提示里要写「Key 无效或账号未实名」。

### 8.6 音色列表（v1.2）

```
GET {base_url}/audio/voice/list
```

200 响应：

```json
{
  "results": [
    {
      "model": "fishaudio/fish-speech-1.4",
      "customName": "your-voice-name",
      "text": "在一无所知中, 梦里的一天结束了，一个新的轮回便会开始",
      "uri": "speech:your-voice-name:xxx:xxx"
    }
  ]
}
```

解析容错见 §3.6；结果按 `customName` 升序返回。`provider.list_voices` 兼容薄壳只取其中的 `uri` 字符串。

### 8.7 删除音色（v1.2）

```
POST {base_url}/audio/voice/deletions
Authorization: Bearer {api_key}
Content-Type: application/json

{"uri": "speech:your-voice-name:xxx:xxx"}
```

2xx → `True`；404 → `False`（已经不存在）；其余按 §8.2 映射。
命令层提示「已删除；如需恢复请重新克隆」，不做二次确认（低频操作，误删成本可控）。

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

- 必须覆盖（PLAN §10）：
  - provider：错误分支（auth/rate_limit/server/empty/network）、`redact_secret` 不泄漏、`request_json` 的 2xx/非 2xx；
  - voice（v1.2 新增）：`read_reference_audio` 的路径越界/后缀/大小拒绝，`clone_voice` 的 payload 形状与 uri 解析
    （缺 uri → `bad_request`），`list_voices` 四种响应结构容错，`delete_voice` 的 404 → `False`；
  - cache：命中/过期/淘汰、`enabled=False` 直通；
  - text：切分与截断边界（0/1/正好 max_chars/超长）、无空格输出；
  - audio：采样率回落、格式白名单；
  - reply_ext：`build_messages` 的 `keep_text` 两种分支与「去文字后空 segments 丢弃整条」。
- 不联网：provider/voice 测试用 `httpx.MockTransport` 或本地 `http.server`；**不得真实调用硅基流动**。
- 密钥：测试只从 `SILICONFLOW_API_KEY` 取，未设置时 `pytest.skip`；夹具里只允许 `sk-xxxx`（见 §12）。

---

## 10. 交付自检清单

`provider-dev`（`provider.py` / `voice.py` / `cache.py` / `text.py` / `tests/`）：

- [ ] `create_provider(cfg)` 返回可复用实例；`synthesize` 返回 `bytes`
- [ ] `provider.request_json` 可用，`voice.py` 通过它发请求（不重复实现 HTTP）
- [ ] `provider.list_voices` 已改为委托 `voice.list_voices` 的兼容薄壳（现有测试仍绿）
- [ ] `voice.clone_voice` 的 `model`/`customName`/`text`/`audio` 四字段与 §8.5 一致；uri 缺失按 `bad_request` 处理
- [ ] `voice.list_voices` 能解析 `{"results": [...]}` 与裸 list 等四种结构；异常结构返回 `[]`
- [ ] `voice.delete_voice` 404 → `False`，不抛异常
- [ ] `read_reference_audio` 拒绝 `../`、绝对路径、非白名单后缀、>10MB、隐藏文件
- [ ] `make_key` 七个字段全参与，`text` 用清洗后文本，`voice` 用 `effective_voice`
- [ ] `Cache.get/put` 原子写、TTL、LRU、`enabled=False` 直通
- [ ] `text.prepare` 无空格输出、句边界截断、空输入返回 `""`
- [ ] `redact_secret()` 存在且被测；五个模块顶层无 `maibot_sdk` 导入；`pytest` 全绿

`plugin-dev`（`plugin.py` / `config.py` / `reply_ext.py` / `audio.py` / `commands.py`）：

- [ ] `config_model = SiliconFlowTTSConfig`，23 字段全在 `[plugin]` 段，外层转发属性可用
- [ ] `effective_voice`：纯音色名补前缀，`speech:` 或含 `:` 原样返回
- [ ] 三个生命周期方法均覆盖且签名一致；`create_plugin()` 存在；`on_unload` 关闭 provider
- [ ] `@ReplyExtension("voice", ..., platforms="qq")` 参数 Schema 与 §3.7 一致
- [ ] `before_send` 返回的 `messages` 通过 §6 全部校验（键集合、Base64、非空 segments）
- [ ] `fallback_on_error="text"` 下任何异常都不外抛
- [ ] `voice_dir` 自动创建；`/tts voices`、`/tts clone`、`/tts voice rm`、`/tts status` 按 §3.9 实现
- [ ] 命令错误文案可执行（auth/bad_request/FileNotFoundError 三种），且只回文字
- [ ] 插件根无 `__init__.py`，全部相对导入
- [ ] `python -m json.tool _manifest.json` 通过；空 `api_key` 时降级不崩；仓库内无真实 key（§12）

---

## 11. 变更流程

1. 本文任何**冻结名称/签名/默认值**的改动，先在 `docs/CONTRACT.md` 提 PR 并 @architect，升版本号（v1.x）。
2. 只改内部实现、不改公开面的，无需改本文；但要在 PR 描述里写明「不影响契约」。
3. `_manifest.json` 的 `id`、`capabilities`、`sdk.min_version` 属于冻结项，改动必须同步本文与 PLAN。
4. 本期范围变更（音色克隆）已同步到 `docs/PLAN.md` §7/§10/§11；再改范围要先改 PLAN 再改本文。
5. 兼容承诺：`provider.list_voices` 薄壳保留到 v1.3；`voice.*` 一旦发布即视为冻结面。

---

## 12. 密钥安全红线（硬约束，不可协商）

> 目标：**API key 绝不进源码、绝不进 git 历史、绝不进日志与截图**。

| 编号 | 规则 | 检查方式 |
|---|---|---|
| **R1** | 仓库内任何文件（源码/文档/示例/测试夹具/README/issue 模板/提交信息）不得出现真实 key；示例一律 `sk-xxxx` 或空串 `""` | `git grep -nIE 'sk-[A-Za-z0-9]{16,}'` 必须无输出 |
| **R2** | 运行期唯一来源：`config.toml` 的 `[plugin].api_key`（Runner 生成，`.gitignore` 覆盖）或环境变量 `SILICONFLOW_API_KEY`；代码中不得有默认 key、不得把 key 拼进 URL | 代码评审 + `grep -n "api_key"` |
| **R3** | 日志红线：禁止打印 `api_key` / `Authorization` / 完整请求头。错误信息只留状态码 + 响应体前 300 字，且必须过 `redact_secret()` | 单测：构造含 `sk-` 的错误串，断言只剩 `sk-****` |
| **R4** | 测试与 CI 只从环境变量取 key；未设置时跳过联网用例（`pytest.skip`）。**禁止**读本地 `config.toml` 里的 key 做测试，禁止把 key 写进 `conftest.py` | `grep -rn "SILICONFLOW_API_KEY" tests/`；CI 日志无 key |
| **R5** | `.gitignore` 必须覆盖：`config.toml`、`.env`、`.env.*`、`*.key`、`*.pem`、`secrets/`、`voices/` | `git check-ignore -v config.toml .env` |
| **R6** | 交付前自检（architect 执行，命中即阻断交付）：见下方命令块 | 见下方 |
| **R7** | 若误提交：先到控制台 revoke 该 key，再清理历史（`git filter-repo`）并强推，最后在报告里记录事件与轮换时间 | 事件记录 |

**R6 交付前扫描（逐条跑，必须全绿）：**

```bash
# 1) 工作区 + 全部被跟踪文件
git grep -nIE 'sk-[A-Za-z0-9]{16,}' -- . || echo "CLEAN: worktree"
# 2) 完整 git 历史（含已删除文件）
git log -p --all | grep -nE 'sk-[A-Za-z0-9]{16,}' || echo "CLEAN: history"
# 3) 敏感文件不得被跟踪
git ls-files | grep -E '(^|/)(config\.toml|\.env(\..*)?|.*\.(key|pem))$' || echo "CLEAN: no secret files tracked"
# 4) 忽略规则生效
git check-ignore -v config.toml .env
```

**`redact_secret()` 冻结签名与行为**（`provider.py` 提供，`voice.py` / `plugin.py` / `commands.py` 共用）：

```python
def redact_secret(text: str) -> str:
    """将 sk- 开头的长串替换为 sk-****；不改变其他内容。"""

# 例：redact_secret("Authorization: Bearer sk-abcdefgh") -> "Authorization: Bearer sk-****"
```

规则：匹配 `sk-[A-Za-z0-9]{8,}` → `sk-****`；对 `ProviderError.message`、所有 `logger.*` 入参、
命令回显内容**统一先过一遍**。展示用掩码统一 `sk-****`，不显示前后缀。

**文档与示例写法**：

```toml
# ✅ 正确：留空，用环境变量
api_key = ""
# ✅ 正确：占位符
api_key = "sk-xxxx"
# ❌ 禁止：任何真实 key（哪怕只写片段）
api_key = "sk-<真实字符串>"
```

**CI / 本地钩子建议**（非强制）：`pre-commit` 里加 `git grep -nIE 'sk-[A-Za-z0-9]{16,}'`，命中即拒绝提交。
