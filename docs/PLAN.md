# 方案书：maibot-plugin-siliconflow-tts

> MaiBot（麦麦）接入硅基流动语音合成（TTS）的第三方插件
> 版本 0.1（立项稿） · 2026-10-25

---

## 1. 项目定位

给 MaiBot 增加一种新的回复形态：**麦麦说话**。

Planner 在需要语音时选中本插件，插件把已生成的文字回复通过硅基流动 TTS 合成为音频，作为 QQ 语音消息发出，可选保留或替换原文字。

**不是**改麦麦本体，**不是**改适配器，是**一个纯插件**：装到 `plugins/` 即用，卸载无残留。

---

## 2. 为什么必须做成插件（现状核查）

动手前已在运行实例上实测，三条硬事实：

| 核查项 | 实测结果 | 结论 |
|---|---|---|
| `bot_config.toml` 的 `[voice]` 段 | 只有 `enable_asr = false` | 麦麦本体**只有语音识别，没有语音合成** |
| 内置插件目录 `src/plugins/built_in/` | 只有 `plugin_management` | 官方没有内置 TTS |
| 已装第三方插件 | 10 个，无 TTS 相关 | 生态里也没有可复用的 |

结合官方文档的明确表述：

> [回复扩展文档](https://docs.mai-mai.org/plugin/reply-extensions)：**音频合成由插件自行完成（SDK 不内置 TTS）**，语音段必须携带有效非空的 Base64 音频。

所以「文字转语音」这条路，**插件是唯一入口**，本项目的成立前提是硬的。

---

## 3. 技术依据（全部来自官方文档，非推测）

### 3.1 MaiBot 侧

| 文档 | 用到的结论 |
|---|---|
| [插件开发指南](https://docs.mai-mai.org/plugin/) | Host/Runner IPC 架构；`MaiBotPlugin` 基类；**必须实现 `on_load` / `on_unload` / `on_config_update` 三个生命周期方法**，否则 Runner 拒绝加载；用 `PluginConfigBase` + `Field` 声明强类型配置；运行时数据写 `self.ctx.paths.data_dir` |
| [回复扩展](https://docs.mai-mai.org/plugin/reply-extensions) | `@ReplyExtension` 两个阶段：`prepare` 返回 `extra_prompt`、`before_send` 返回改写后的 `messages`；消息段支持 `voice`；**官方给了「语音回复」完整示例**，本项目直接以其为骨架 |
| [回复扩展 · 约束](https://docs.mai-mai.org/plugin/reply-extensions#约束与失败行为) | 插件里**不要替主程序发送**；空消息组/无效返回值/插件报错/超时都会让**整次 reply 失败** —— 这条决定了第 6 节的降级策略 |
| [回复扩展 · 平台作用域](https://docs.mai-mai.org/plugin/reply-extensions#平台作用域) | `platforms` 需要 `maibot-plugin-sdk>=2.14.0`；本插件限定 `"qq"` |
| [Manifest](https://docs.mai-mai.org/plugin/manifest) | `_manifest.json` 字段与校验规则 |

### 3.2 硅基流动侧

依据 [语音合成使用指南](https://docs.siliconflow.cn/docs/userguide/capabilities/text-to-speech) 与 API 手册 `POST /v1/audio/speech`：

| 参数 | 取值 | 备注 |
|---|---|---|
| `model` | `FunAudioLLM/CosyVoice2-0.5B`、`fnlp/MOSS-TTSD-v0.5`、fishaudio / GPT-SoVITS 系列 | 以「模型广场 · 语音」标签为准 |
| `voice` | `模型名:音色名`，如 `FunAudioLLM/CosyVoice2-0.5B:alex` | 8 个系统音色：男 `alex/benjamin/charles/david`，女 `anna/bella/claire/diana` |
| `input` | 待合成文本 | **不要加空格**（官方明确提示） |
| `speed` | `[0.25, 4.0]`，默认 1.0 | |
| `gain` | `[-10, 10]` dB，默认 0 | |
| `response_format` | `mp3` / `opus` / `wav` / `pcm` | mp3 采样率 `32000` / `44100` |
| 自定义音色 | `POST /v1/uploads/audio/voice` → 返回 `uri` | 需实名；参考音频 <30s，建议 8–10s |
| 音色列表 | `GET /v1/audio/voice/list` | 用于 `/tts voices` 命令 |
| 计费 | 按输入文本 **UTF-8 字节数** | 直接影响缓存与长度截断策略 |

情感控制可用 `<|endofprompt|>` 分隔指令与正文。

### 3.3 出站链路（在运行实例上读源码确认）

读 `maibot-team_snowluma-adapter` 的 `codecs/outbound/segment_encoder.py`：

```python
def _build_voice_segment(self, item):
    ...
    return {"type": "record", "data": {"file": f"base64://{binary_base64}"}}
```

即：Host 侧给 `voice` 段 + `binary_data_base64` → 适配器编码成 OneBot v11 `record` 段。**音频格式由插件提供**，适配器的 `record_out_format`（snowluma=`mp3`、napcat=`wav`）是**入站**转码用的，不影响出站。

适配器还提供 `can_send_record` 动作，可先探测再决定是否发语音。

---

## 4. 架构设计

### 4.1 分层与模块边界

刻意把「网络」「文本」「缓存」「组件」分开，任何一层都能独立单测，不放进一个巨型 `plugin.py`。

```
maibot_plugin_siliconflow_tts/
├── __init__.py
├── plugin.py          # 入口：MaiBotPlugin 子类 + create_plugin()，只做装配
├── config.py          # PluginConfigBase 配置模型（唯一配置真源）
├── provider.py        # 硅基流动 TTS 客户端：HTTP、重试、错误归一化
├── cache.py           # 音频缓存：键 = (text, voice, model, speed, gain, format)
├── text.py            # 文本准备：去 markdown/表情/@、去多余空白、按字节切分
├── audio.py           # 音频格式决策与可选转码（silk/wav 兜底）
├── reply_ext.py       # @ReplyExtension 组件（prepare / before_send）
└── commands.py        # /tts 命令组：试听、音色切换、开关、状态
```

**依赖方向**：`plugin.py` → 其余全部；`reply_ext.py` → `provider/cache/text/audio`；**provider 不依赖 SDK**（纯 HTTP，可用 pytest 直接测）。

### 4.2 数据流

```
用户消息
   ↓
麦麦 Planner 决定回复，调用 reply 工具
   ↓（plugin_options 里带上 siliconflow_tts.voice）
reply_ext.prepare      → 返回 extra_prompt：「本次回复会转成语音，请用适合朗读的口语、不要用列表和颜文字」
   ↓
Replyer 生成正文 → Hook 处理
   ↓
reply_ext.before_send  → text 转音频：
                          text.prepare() → 命中 cache? → provider.synthesize()
                          → audio.ensure_compatible() → base64
   ↓
返回 messages：原消息（按 keep_text 决定是否去文本段）+ 一条 voice 段
   ↓
主程序统一发送 / 记历史 / 监控
```

### 4.3 与主程序的边界

| 属于插件 | 属于主程序 |
|---|---|
| 文本清洗、切分 | 消息发送 |
| 调 API、重试 | 历史记录 |
| 缓存读写 | 监控时间线 |
| 音频格式决策 | 失败后的整体回滚 |
| 构造 `messages` | `plugin_options` 的 Schema 暴露 |

**不越界**：插件绝不自己 `send.text`，避免消息重复或顺序错乱。

---

## 5. 配置项设计

`config.py` 用 `PluginConfigBase` 声明，Runner 自动生成 `config.toml` 与 WebUI Schema。

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `enabled` | bool | `true` | 组件总开关 |
| `api_key` | str | `""` | 硅基流动 key；**空则回落到环境变量 `SILICONFLOW_API_KEY`** |
| `base_url` | str | `https://api.siliconflow.cn/v1` | 便于指向自建反代 |
| `model` | str | `FunAudioLLM/CosyVoice2-0.5B` | |
| `voice` | str | `FunAudioLLM/CosyVoice2-0.5B:alex` | 完整 `模型:音色`；也允许只写音色名，插件自动补齐 |
| `speed` | float | `1.0` | `[0.25, 4.0]` 校验 |
| `gain` | float | `0.0` | `[-10, 10]` |
| `response_format` | enum | `mp3` | `mp3`/`wav`/`opus` |
| `sample_rate` | int | `44100` | 按格式校验合法值 |
| `keep_text` | bool | `true` | 语音之外是否保留原文字 |
| `auto_voice` | bool | `false` | **尽力而为，非强制**。ReplyExtension 只有在 Planner 主动填 `plugin_options` 时才会被调用，扩展自身无法让自己「总是参与」。故此项 M1 仅作配置占位，M2 由 Hook 在 reply 调用前注入 `plugin_options` 才真正生效 |
| `max_chars` | int | `300` | 超长截断（按 UTF-8 字节另算，见 6.3） |
| `timeout_ms` | int | `60000` | 与 `@ReplyExtension(timeout_ms=)` 对齐 |
| `retry` | int | `2` | 429/5xx 重试次数 |
| `cache_enabled` | bool | `true` | |
| `cache_ttl_hours` | int | `72` | 过期清理 |
| `cache_max_mb` | int | `200` | 超限按 LRU 淘汰 |
| `fallback_on_error` | enum | `text` | `text`=降级为纯文字；`fail`=让本次 reply 失败（调试用） |

---

## 6. 关键设计决策

### 6.1 为什么用 ReplyExtension 而不是 Hook

官方文档对 ReplyExtension 的定位就是「只做随回复注入参数 / 改写回复内容」，并明确「发送、历史记录、监控与失败判断都由主程序统一负责」。Hook 更底层、要自己处理全局改写，对本需求属于过度设计。

### 6.2 为什么 prepare 阶段要返回 extra_prompt

TTS 的文本和聊天文本诉求不同：聊天可以有颜文字、括号动作、列表、代码块，这些念出来是灾难。在 `prepare` 阶段注入约束，比在 `before_send` 阶段事后清洗更省事，也更自然。

### 6.3 长文本：切分而非硬截断

硅基流动按 UTF-8 字节计费，且单次请求有长度上限。策略：

1. 先按句子边界切（中文 `。！？\n`，英文 `. ! ?`）
2. 逐句累计，单段控制在 `max_chars` 之内
3. 超长时优先保留**首段 + 末段**（语气完整），中间省略并记日志
4. 多段音频**不做拼接**（拼接需要重编码，收益低），改为只发首段语音 + 完整文字

### 6.4 缓存键与失效

```
sha256(model | voice | speed | gain | format | sample_rate | normalized_text)
```

语速、音色、模型任一变化即视为不同音频，避免「换了音色还在放旧声音」。

### 6.5 失败降级：绝不让一次 TTS 搞崩整次回复

文档明确「任一扩展在发送前失败都会让整次回复失败」。所以默认 `fallback_on_error = text`：

- 超时 / 4xx / 5xx / 音频为空 → 记 WARN，返回 `{}`（保留原始文字消息）
- 只有显式配 `fail` 才向上抛（联调阶段用）
- `can_send_record` 返回不支持 → 直接降级，不浪费一次 API 调用

### 6.6 音频格式：先 mp3，留好转码接口

硅基流动可直接给 mp3，OneBot `record` 段接受 `base64://`。但 QQ 客户端对 mp3 语音的兼容性需要在真实环境实测（见第 8 节）。`audio.py` 预留 `ensure_compatible()`：

- `mp3` 直出（默认）
- 需要时降级 `wav`（体积大但兼容性最好）
- 若实测确认必须要 silk，则在 `audio.py` 内接 `pysilk` + `ffmpeg`（适配器里已有同款实现可参考），**不污染其他模块**

---

## 7. 里程碑

| 阶段 | 交付 | 验收 |
|---|---|---|
| **M0 骨架** | `_manifest.json`、`plugin.py`、`config.py`、`reply_ext.py` 空实现 | 插件能被 Runner 加载，`plugin_options` 里出现 `siliconflow_tts.voice` |
| **M1 打通** | `provider.py` + `reply_ext.py` 真实合成 | 聊天里触发一次，收到能播放的语音消息 |
| **M2 工程化** | `cache.py`、`text.py`、降级、重试 | 同句第二次 0 API 调用；API 挂掉时文字照常发出 |
| **M3 可运维** | `/tts` 命令组、README、i18n、单测 | 不装 Python 也能按 README 配置成功；`pytest` 全绿 |

---

## 8. 风险与待验证项（P0 必须先验）

| # | 风险 | 影响 | 验证方式 |
|---|---|---|---|
| **R1** | **QQ 语音消息对 mp3 的兼容性** | 语音发不出去或对方听不到 | 装 M1 后真机发一条，手机端试听 |
| **R2** | `can_send_record` 在 SnowLuma 下的实际返回 | 可能不支持 → 永远降级 | 直接调适配器 API 看返回 |
| **R3** | 容器内是否有 `ffmpeg` / `pysilk` | 影响 silk 转码兜底 | `docker exec` 里 `which ffmpeg` |
| **R4** | 硅基流动单次文本长度上限 | 长回复失败 | 用 300 / 800 / 2000 字各打一次 |
| **R5** | 语音段与文字段的顺序/引用关系 | 可能出现「先语音后文字」 | 观察 `quote_previous` 与首条引用行为 |
| **R6** | `@ReplyExtension` 在麦麦 8.15.37 的实际支持 | 版本不够则组件不生效 | 查 `maibot-plugin-sdk` 版本 ≥ 2.14.0 |
| **R7** | 计费失控 | 群聊高频触发烧钱 | 缓存 + `max_chars` + 全局限流（M2 加） |
| **R8** | **扩展无法自动参与**：`@ReplyExtension` 只在 Planner 主动填 `plugin_options` 时被调用，扩展自身无法强制生效 | 麦麦可能一次都不用语音，功能形同虚设 | M1 用 `/tts test` 与 Prompt 引导验证；M2 用 Hook 在 reply 前注入 `plugin_options` 兜底 |

---

## 9. 开发团队分工建议（AgentTeams）

| 角色 | 职责 | 写作用域 |
|---|---|---|
| **architect** | 复核方案、定接口签名、review 所有 PR 级改动 | `docs/`、接口定义 |
| **provider-dev** | `provider.py` + `cache.py` + `text.py`，含单测 | 上述三文件 + `tests/` |
| **plugin-dev** | `plugin.py` + `config.py` + `reply_ext.py` + `audio.py` | 上述四文件 |
| **verifier** | 真实环境装插件、跑 R1–R7、出验证报告 | 只读 + 服务器操作 |
| **docs-dev** | README、配置示例、i18n | `README.md`、`i18n/` |

**串行约束**：接口定义（architect）→ provider/plugin 并行开发 → verifier 联调 → docs 收尾。

---

## 10. 验收标准

- [ ] 群里 @ 麦麦说「用语音回我」，收到可播放的语音
- [ ] 同一句话连发 3 次，第 2、3 次无 API 调用（看日志与计费）
- [ ] 把 `api_key` 改错，文字回复仍正常发出，只有语音缺失
- [ ] `/tts voices` 能列出当前账号可用音色
- [ ] 卸载插件后麦麦行为完全恢复原状，无报错
- [ ] `pytest` 覆盖 provider 的错误分支、cache 命中/过期、text 切分边界

---

## 11. 明确不做（本期范围外）

- 语音识别（ASR）——麦麦已有 `enable_asr`，不重复造
- 声音克隆（上传参考音频）——需要实名 + 文件上传，放到 v0.2
- 语音消息转文字再回复（双向语音对话）——v0.3 再议
- 修改 MaiBot 本体或适配器

---

## 附：文档依据清单

1. https://docs.mai-mai.org/plugin/ — 插件架构、生命周期、配置模型
2. https://docs.mai-mai.org/plugin/reply-extensions — 回复扩展协议与语音示例
3. https://docs.mai-mai.org/plugin/manifest — Manifest 字段
4. https://docs.siliconflow.cn/docs/userguide/capabilities/text-to-speech — TTS 参数、音色、计费
5. `maibot-team_snowluma-adapter` 源码（运行实例）— 出站 `record` 段构造、`can_send_record`
