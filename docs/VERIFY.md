# 真机验证报告（R1–R7）

> 验证对象：`maibot-plugin-siliconflow-tts` v0.1.0（commit `bda6d3a`）
> 验证环境：真实 MaiBot 实例 `160.202.47.128` / 容器 `maim-bot-core`
> 验证时间：2026-10-25 00:48–01:40（Asia/Shanghai）
> 验证人：verifier
>
> **红线**：硅基流动 API Key 全程只通过容器环境变量 `SILICONFLOW_API_KEY` 注入，
> `config.toml` 里 `api_key = ""`，本报告不含 key 明文。

---

## 0. 结论速览

| # | 风险 | 结论 | 关键证据 |
|---|---|---|---|
| **R1** | QQ 对 mp3 语音的兼容性 | ✅ **兼容**：SnowLuma/QQ 接收 mp3，服务端自动转成 SILK；**插件不需要自己做 ffmpeg/silk 转码**。真机试听按指挥暂停，等新素材 | `retcode:0` + `message_id`；回读 `record` 变 `*.amr`；下载文件头 `\x02#!SILK_`（16,836 B） |
| **R2** | `can_send_record` 在 SnowLuma 下的返回 | ✅ 支持 | `{"status":"ok","retcode":0,"data":{"yes":true}}` |
| **R3** | 容器内 `ffmpeg` / `pysilk` | `ffmpeg` ❌ 无；`pysilk` ✅ 有（模块可导入） | `which ffmpeg` 空；`pysilk.__file__` 存在；插件日志 `转码能力={'ffmpeg': False, 'pysilk': True}` |
| **R4** | 硅基流动单次文本长度上限 | ✅ 300/800/2000 字全部 HTTP 200；但**音频输出在 ~163.3 s 饱和**，约 600 字以上尾部不再朗读。插件默认 `max_chars=300`（≈97 s）是安全值 | 见 §4 表格与 mp3 帧解析时长 |
| **R5** | 语音段与文字段顺序 / 引用 | ✅ 文字段保持原顺序在前，语音段**固定追加在最后**且 `quote_previous=False`；不会出现「先语音后文字」 | `build_messages` 实测输出 + Host 校验规则 + 适配器编码器 |
| **R6** | ReplyExtension 在 8.15.37 + SDK 2.16.0 是否生效 | ✅ 生效 | Planner 工具 schema 中 `reply.plugin_options` 恰为 `["local.siliconflow-tts.voice"]`，149 份 prompt 日志含该键 |
| **R7** | 计费失控 / 缓存 | ✅ 同一句连发 3 次只打 **1 次** HTTP，第 2/3 次缓存命中 | harness 输出 `total http_requests=1`，缓存文件 `audio/35/358a….mp3` |

> 补充（环境前提，非本插件结论）：当前账号 **CosyVoice2 上游不可用**（请求 60–180 s 挂起、0 字节）；
> **`fnlp/MOSS-TTSD-v0.5` 正常**（0.5–1.2 s，200）。`/v1/audio/voice/list` 200，账号内已有克隆音色，
> 说明实名与克隆能力已开通。R1–R7 的合成测试全部使用 MOSS。

---

## 1. 环境与部署

### 1.1 版本矩阵（实测）

| 项 | 值 | 来源 |
|---|---|---|
| 容器 | `maim-bot-core`（`sengokucola/maibot:latest`） | `docker ps` |
| MaiCore | `1.3.14` | 启动日志 `MaiCore 当前版本: 1.3.14` |
| 配置版本（“麦麦 8.15.37”） | `CONFIG_VERSION = "8.15.37"` | `src/config/config.py:75`、`config/bot_config.toml` |
| `maibot-plugin-sdk` | `2.16.0` | `pip` / `import maibot_sdk` |
| Python | `3.13.16` | `python --version` |
| `httpx` / `pydantic` | `0.28.1` / `2.13.3` | `importlib.metadata` |
| QQ 适配器 | SnowLuma `2.1.1-node`，OneBot v11 | `get_version_info` |
| Bot QQ | `3692557270`（蓝色不是大肥鱼） | `get_login_info` |
| 插件目录（宿主） | `/opt/maibot/data/MaiMBot/plugins/local_siliconflow-tts` | 挂载 |
| 插件目录（容器） | `/MaiMBot/plugins/local_siliconflow-tts` | `docker inspect` mount |

### 1.2 部署与加载

插件代码通过 `git archive` 打包后解压到插件目录，Runner 的 `watchfiles` 直接热加载；
随后为注入环境变量重建了一次 `core` 容器。加载日志（容器内 `/MaiMBot/logs/app_*.log.jsonl`）：

```json
{"event": "插件 local.siliconflow-tts v0.1.0 加载成功", "logger": "plugin_runtime.group.extension", "timestamp": "2026-10-24T17:10:32.416202Z"}
{"event": "硅基流动 TTS 已加载：model=FunAudioLLM/CosyVoice2-0.5B voice=FunAudioLLM/CosyVoice2-0.5B:alex format=mp3/44100Hz 缓存=开 运行开关=开 转码能力={'ffmpeg': False, 'pysilk': True}", "logger": "plugin.local.siliconflow-tts", "timestamp": "2026-10-24T17:10:33.969601Z"}
{"event": "TTS 配置已热更新（version=）：voice=fnlp/MOSS-TTSD-v0.5:anna format=mp3/44100Hz 缓存=开", "logger": "plugin.local.siliconflow-tts", "timestamp": "2026-10-24T17:31:04.581780Z"}
```

`config.toml`（**`[plugin]` 段 + `config_version`，api_key 留空走环境变量**）：

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
```

key 注入方式（**不落盘**）：

```bash
# 容器环境变量，长度校验 52（含换行），不打印值
docker exec maim-bot-core sh -lc 'echo -n "env_key_len="; printenv SILICONFLOW_API_KEY | wc -c'
# env_key_len=52   （key 51 字符）
```

> ⚠️ 运维注意：本次是通过临时 compose override 文件 `/tmp/sf_override.yml`
> （只含 `SILICONFLOW_API_KEY: "${SILICONFLOW_API_KEY}"` 变量引用）重建容器注入的。
> 以后若直接 `docker compose up -d` 而不带 `-f /tmp/sf_override.yml` 且进程环境里没有该变量，
> 容器会丢变量、插件日志会重新出现「未配置 api_key」。正式部署请把变量加进 compose 的
> `environment` / `.env` / secret 管理，**不要提交进仓库**。

---

## 2. R1 — QQ 对 mp3 语音的兼容性

### 2.1 状态

按指挥要求，**01:31 之后已停止一切真实投递**（`/tts test` 真发、ReplyExtension 真发均暂停），
真机试听等用户提供新的纯净参考音频后再做最后一步。
**01:31 之前**已投递过 1 条测试语音（私聊 `3430500996`），该条即为下面的证据来源，可撤回。

### 2.2 证据（OneBot → SnowLuma → QQ）

发送（MOSS 生成的 mp3，79,437 B，`ID3` 头）：

```json
{"status": "ok", "retcode": 0, "data": {"message_id": 1579510977}, "echo": "send_private_msg"}
```

回读同一 `message_id`：

```json
{"status":"ok","retcode":0,"data":{"message_id":1579510977,"message_type":"private",
 "message":[{"type":"record","data":{"file":"17923e24b27f2df502deec779036d791.amr",
 "url":"https://multimedia.nt.qq.com.cn/download?appid=1402&fileid=…（rkey 已省略）"}}]}}
```

把该 URL 下载回来做魔数校验：

```text
downloaded bytes= 16836
head= b'\x02#!SILK_'        # 实际是 SILK，不是 AMR；文件名 .amr 只是 QQ 的习惯命名
```

### 2.3 结论

1. **QQ 接受 mp3**：OneBot 层 `retcode:0`，消息成功入库并返回 `message_id`。
2. **QQ 服务端把 mp3 转成了 SILK**（`\x02#!SILK_V3`），**出站不需要插件自己做 silk/amr 转码**。
3. 设备联系人（“我的手机”/“我的电脑”）会拒绝 `record`：
   `{"status":"failed","retcode":1400,"wording":"message element \"record\" cannot be sent to this contact"}`。
   这是 QQ 对设备联系人的限制，与 mp3 格式无关。

> 证据口径：这一条是**直接调 SnowLuma OneBot**（`send_private_msg`）验证 QQ 侧兼容性；
> 插件路径在其前面还有 Host 的 `voice` 段归一化与适配器编码，已分别由 §6.2 的源码与 §7 的
> schema 证据覆盖。适配器最终发出的正是
> `{"type":"record","data":{"file":"base64://…"}}`，与本次直发的段同构，所以 QQ 侧的
> 「收 mp3、转 SILK」行为对插件路径同样成立。

### 2.4 架构决策（写死，避免后来人重复踩坑）

`audio.py` 的 silk/ffmpeg 分支**保持不启用**：

- `ensure_compatible()` 只做「非空 + 格式白名单」校验后**原样返回 mp3**（`audio.py:73-92`）；
- `transcode_to_silk()` 仍是 `NotImplementedError` 预留桩（`audio.py:119-126`）；
- 容器里 `ffmpeg` 本来就不存在，`pysilk` 虽有但**不需要**用来做出站转码。

> 真机试听（手机端能否听到声音）**留空待命**：等新素材定稿后按 `/tts test` 走一遍即可，
> 不需要再改代码。

---

## 3. R2 — `can_send_record` 在 SnowLuma 下的实际返回

直接调 OneBot `can_send_record`（适配器 `adapter.napcat.system.can_send_record` 的底层动作）：

```json
{"status":"ok","retcode":0,"data":{"yes":true},"echo":"can_send_record"}
{"status":"ok","retcode":0,"data":{"yes":true},"echo":"can_send_image"}
{"status":"ok","retcode":0,"data":{"app_name":"SnowLuma","app_version":"2.1.1-node","protocol_version":"v11"},"echo":"get_version_info"}
```

**结论**：SnowLuma 2.1.1-node 明确返回 `yes: true`，插件不会因为 `can_send_record=false` 永远降级。

---

## 4. R3 — 容器内 `ffmpeg` / `pysilk`

```text
$ which ffmpeg
ffmpeg: absent
$ python3 -c "import pysilk; print('pysilk:', pysilk.__file__)"
pysilk: /MaiMBot/.venv/lib/python3.13/site-packages/pysilk/__init__.py
```

插件启动日志（`probe_transcoders()` 实测）：

```json
"转码能力={'ffmpeg': False, 'pysilk': True}"
```

**结论**：容器内没有 `ffmpeg`；`pysilk` 可用，但按 R1 的结论不需要用于出站语音。
若未来某天需要「silk → mp3」的**入站**解码（例如把收到的语音转文字），可以直接用 `pysilk`，
但当前插件链路不依赖。

---

## 5. R4 — 硅基流动单次文本长度上限

测试方式：`POST https://api.siliconflow.cn/v1/audio/speech`，`model=fnlp/MOSS-TTSD-v0.5`，
`voice=…:anna`，`response_format=mp3`，`sample_rate=44100`，文本为中文句子的循环截断。
时长由 mp3 帧头解析（MPEG1 Layer III，1152 samples/frame）。

| 输入字数 | HTTP | 返回字节 | 音频时长 | 备注 |
|---|---|---|---|---|
| 300 | 200 | 1,560,266 | **97.4 s** | 正常 |
| 500 | 200 | 1,857,017 | — | 正常 |
| 800 | 200 | 2,613,523 | **163.3 s** | 已饱和 |
| 2000 | 200 | 2,613,523 | **163.3 s** | 与 800 同尺寸，尾部不再增长 |
| 600 / 900 / 1000 / 1200 | 200 | 2,613,523 | — | 全部饱和；sha 不同（TTS 非确定性），但尺寸恒定 |

**结论**：

1. 接口**不会**因 300/800/2000 字返回 4xx；单次请求长度上限不是硬报错。
2. 但 **输出音频在 ~163.3 s 饱和**（约 600 字起），800/2000 字的尾部实际没有被朗读。
   按 300 字 ≈ 97.4 s 折算，饱和点约在 **500 字左右**。
3. 插件默认 `max_chars=300` 正好落在安全区（≈97 s），既不会被截尾，也控制单次时长与费用。
   **不建议把 `max_chars` 调到 500 以上**。

### 5.1 上游模型可用性（重要）

| 请求 | 结果 |
|---|---|
| `FunAudioLLM/CosyVoice2-0.5B:alex`（非流式） | **超时 60–180 s，0 字节**（TLS 0.13 s 已通） |
| `FunAudioLLM/CosyVoice2-0.5B:alex` + `stream:true` | 同样挂起 |
| `fnlp/MOSS-TTSD-v0.5:anna` | **200，0.5–1.2 s，79 KB** |
| `/v1/models` 对照 | **200，0.24 s** |
| `/v1/audio/voice/list` | **200，1.2 KB**（账号内已有克隆音色） |

**CosyVoice2 在当前账号/网络下不可用**，任何 60 s 挂起都应先归因到上游模型，而不是插件超时/网络。
测试与部署必须使用 **MOSS-TTSD-v0.5**。

---

## 6. R5 — 语音段与文字段的顺序、`quote_previous`

### 6.1 插件输出（真实模块执行，非伪代码）

输入：`segments=[text, image]`、`quote_previous=true`，`text="念出来的句子"`，`audio_base64="QUJD"`。

`keep_text=True`：

```json
[
 {"segments": [{"type":"text","data":"第一段文字"}, {"type":"image","data":{"file":"x.png"}}], "quote_previous": true},
 {"segments": [{"type":"voice","data":"念出来的句子","binary_data_base64":"QUJD"}], "quote_previous": false}
]
```

`keep_text=False`：

```json
[
 {"segments": [{"type":"image","data":{"file":"x.png"}}], "quote_previous": true},
 {"segments": [{"type":"voice","data":"念出来的句子","binary_data_base64":"QUJD"}], "quote_previous": false}
]
```

### 6.2 Host / 适配器约束（实测源码）

- Host 强校验：每条消息必须**恰好**是 `{"segments","quote_previous"}` 两键，`quote_previous` 必须是 `bool`，
  `segments` 非空（`src/plugin_runtime/host/reply_extensions.py:101-115`）。
- `voice` 段：`data` 必须是 `str`，`binary_data_base64` 必须是**合法且非空 Base64**
  （`reply_extensions.py:67-80`）。
- 适配器出站：`binary_data_base64` → `{"type":"record","data":{"file":"base64://…"}}`
  （`maibot-team_snowluma-adapter/codecs/outbound/segment_encoder.py:365-378`）。

### 6.3 结论

- **顺序**：原文字段（text/image/…）保持 Planner 给出的顺序在前；语音段**固定追加到最后**。
  不会出现「先语音后文字」。
- **引用**：语音段 `quote_previous=False`（不引用）；原文字段保留自己的 `quote_previous`，
  不受扩展影响。`keep_text=False` 时只删 `text` 段，`image` 等其他段保留，删空的消息整条丢弃。
- 本项为**载荷级验证**（未做真实投递，遵守暂停指令）；载荷形状已通过 Host 的 `validate_reply_messages`
  规则逐条核对。

---

## 7. R6 — ReplyExtension 在 8.15.37 + SDK 2.16.0 是否生效

直接证据来自 Planner 的**真实工具 schema**（`/MaiMBot/logs/maisaka_prompt/planner/qq_private_3430500996/1792863513875.json`）：

```json
"plugin_options": {
  "type": "object",
  "properties": {
    "local.siliconflow-tts.voice": {
      "type": "object",
      "properties": {
        "attach_voice": {"type": "boolean", "default": true, "description": "是否合成语音"},
        "keep_text": {"type": "boolean", "default": true, "description": "是否同时保留文字"}
      },
      "additionalProperties": false,
      "description": "需要语音回复时启用：把本次回复合成语音，可选择是否保留文字"
    }
  },
  "additionalProperties": false
}
```

统计：`/MaiMBot/logs/maisaka_prompt/planner/**/*.json` 中 **149 份**含
`local.siliconflow-tts.voice`；最新一份时间 `2026-10-25T01:38:33+08:00`。
`reply.plugin_options.properties` 的键**恰好只有** `["local.siliconflow-tts.voice"]`。

**结论**：在 MaiBot 8.15.37（MaiCore 1.3.14）+ SDK 2.16.0 上，`@ReplyExtension("voice", platforms="qq")`
真实注册并被 Planner 看到；参数默认值与描述均正确。`auto_voice` 仍是占位（R8），
Planner 不选 `voice` 时不会自动加语音，这是预期行为，不构成 R6 失败。

---

## 8. R7 — 缓存命中与计费

harness：真实 `SiliconFlowTTSProvider`（注入计数 `httpx.AsyncBaseTransport` 包住真实网络传输）+
真实 `AudioCache`（`/tmp/tts_cache_verify`），同一句连发 3 次：

```text
spoken= 缓存测试：同一句话连发三次，第二、三次应该 0 请求。
cache_key= 358ac032c987cadbe1b7ae9b3406dd8819953458b41e894a3ac7ff6b3f22d628
call 1: SYNTH (144639 bytes), http_requests=1
call 2: CACHE HIT (144639 bytes), http_requests=1
call 3: CACHE HIT (144639 bytes), http_requests=1
total http_requests= 1
cache files= ['/tmp/tts_cache_verify/audio/35/358ac032c987cadbe1b7ae9b3406dd8819953458b41e894a3ac7ff6b3f22d628.mp3']
```

**结论**：同一句连发 3 次只有 **1 次**上游调用，第 2/3 次 0 请求；缓存键 =
`sha256(model|voice|speed|gain|format|sample_rate|text)`，与该文件的单元测试一致。
再叠加 `max_chars=300`（单次 ≈97 s）与 `cache_ttl_hours=72`，群聊高频场景的计费可控。

---

## 9. 已知问题 / 后续动作

| # | 问题 | 影响 | 处理 |
|---|---|---|---|
| 1 | **CosyVoice2 上游不可用**（60–180 s 挂起，0 字节） | 用默认模型必然超时 | 配置已切 `fnlp/MOSS-TTSD-v0.5`；建议把插件默认 `model` 也改成 MOSS，或在 README 显著标注 |
| 2 | MOSS 输出 ~163.3 s 饱和，>~500 字尾部不朗读 | 长文只念前半 | 保持 `max_chars=300`；不要调高 |
| 3 | 容器 `ffmpeg` 不存在 | 无法做 silk 转码 | 不需要（R1 已证 QQ 服务端转 SILK）；silk 分支保持不启用 |
| 4 | 设备联系人（我的手机/电脑）拒绝 `record` | 不能发到设备会话 | QQ 限制，改用群/好友会话 |
| 5 | key 目前靠临时 compose override 注入 | 重建容器可能丢变量 | 正式部署写进 compose env / `.env` / secret，勿提交仓库 |
| 6 | 真机试听未做（按指令暂停） | 手机端能否出声尚未人耳确认 | 等新素材定稿后走 `/tts test`，无需改代码 |
| 7 | 01:31 已投递的 1 条测试语音（私聊 3430500996） | 可能打扰用户 | 可撤回；之后未再投递 |

### 下一步（建议）

1. 用户选定新的纯净参考音频 → 用 MOSS 重新克隆（CosyVoice2 的旧 uri 不可用）。
2. 克隆 uri 填进 `voice`（`speech:<name>:<id>:<hash>` 形式）后，`/tts test` 真发一条做最终人耳试听。
3. 试听通过后，把 `config.toml` 的 `model` 固化为 `fnlp/MOSS-TTSD-v0.5`，并把默认配置写进 README。
4. 如要让 `auto_voice` 真正生效，另开任务做 Hook 注入 `plugin_options`（PLAN R8，不在本次范围）。

---

## 10. 附：最小复现命令

```bash
# 1) 加载与版本
docker exec maim-bot-core python3 -c "import maibot_sdk; print(maibot_sdk.__version__)"
docker exec maim-bot-core sh -lc "grep -a 'TTS 已加载' /MaiMBot/logs/app_*.log.jsonl | tail -1"

# 2) R2 can_send_record（OneBot WS，token 从适配器 config.toml 读）
#    见 §3 的返回 JSON

# 3) R3
docker exec maim-bot-core sh -lc "which ffmpeg || echo ffmpeg-absent; python3 -c 'import pysilk; print(pysilk.__file__)'"

# 4) R4（key 只走环境变量）
docker exec maim-bot-core python3 - <<'PY'
import json, urllib.request, os
body = {"model":"fnlp/MOSS-TTSD-v0.5","voice":"fnlp/MOSS-TTSD-v0.5:anna",
        "input":"麦麦今天也在认真听你说话。","response_format":"mp3","sample_rate":44100,
        "speed":1.0,"gain":0.0}
req = urllib.request.Request("https://api.siliconflow.cn/v1/audio/speech",
    data=json.dumps(body).encode(), method="POST",
    headers={"Authorization":"Bearer "+os.environ["SILICONFLOW_API_KEY"],"Content-Type":"application/json"})
with urllib.request.urlopen(req, timeout=120) as r:
    print(r.status, r.headers.get("Content-Type"), len(r.read()))
PY

# 5) R6：查 Planner schema
docker exec maim-bot-core sh -lc "grep -rl 'local.siliconflow-tts.voice' /MaiMBot/logs/maisaka_prompt/planner | wc -l"

# 6) R7：见 §8 harness（真实 provider + cache，3 次调用 = 1 次 HTTP）
```

---

**结论**：R1–R7 全部有明确结论与证据；R1 的「手机端人耳试听」因用户要求更换音色素材而**有意留空**，
链路本身已由 `mp3 → QQ → SILK` 的实测闭环证明可用。插件不需要 ffmpeg/silk 出站转码。
