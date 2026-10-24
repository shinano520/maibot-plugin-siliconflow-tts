# maibot-plugin-siliconflow-tts

让 [MaiBot（麦麦）](https://github.com/Mai-with-u/MaiBot) 用 [硅基流动](https://siliconflow.cn) 的语音合成「说话」：
把麦麦已生成的文字回复合成为 QQ 语音消息，可选保留或替换原文字。

> **状态：v0.1.0，已在真机跑通。**
> 实测报告：[docs/VERIFY.md](docs/VERIFY.md)（R1–R7 全绿）· 接口契约：[docs/CONTRACT.md](docs/CONTRACT.md) · 方案书：[docs/PLAN.md](docs/PLAN.md)
>
> **你不需要会 Python，也不用 `pip install` 任何东西。** 插件依赖的 `httpx` 主程序已自带（缺失时 MaiBot 会按 `_manifest.json` 自动安装）。

---

## 目录

- [兼容性（实测）](#兼容性实测)
- [安装](#安装)
- [配置](#配置)
- [语音开关（默认全关）](#语音开关默认全关)
- [发出第一条语音](#发出第一条语音)
- [命令一览](#命令一览)
- [音色：系统音色与克隆](#音色系统音色与克隆)
- [音频格式与 QQ 兼容性](#音频格式与-qq-兼容性)
- [常见问题](#常见问题)
- [已知限制](#已知限制)
- [安全须知：API Key](#安全须知api-key)
- [文档与许可证](#文档与许可证)

---

## 兼容性（实测）

| 项 | 实测值 | 说明 |
|---|---|---|
| MaiBot / MaiCore | **1.3.14**（配置版本 `8.15.37`） | 插件加载、命令、回复扩展均正常 |
| `maibot-plugin-sdk` | **2.16.0** | manifest 要求 ≥ **2.14.0**（回复扩展的 `platforms` 需要） |
| Python | 3.13（主程序自带） | 插件本身零额外依赖 |
| QQ 适配器 | **SnowLuma 2.1.1-node**，OneBot v11 | `can_send_record` 返回 `yes: true` |
| 音频格式 | **mp3 / 44100 Hz** | QQ 服务端自动转 SILK；插件无需 ffmpeg（详见[下文](#音频格式与-qq-兼容性)） |
| 其他格式 | `wav` / `opus` | 代码支持并会按格式校验采样率，**尚未做真机投递验证** |
| 平台 | **QQ**（`platforms="qq"`） | 其他平台不会触发本插件 |

> ⚠️ **模型可用性**：验证账号下 `FunAudioLLM/CosyVoice2-0.5B` 会挂起 60–180 秒、返回 0 字节；`fnlp/MOSS-TTSD-v0.5` 正常（0.5–1.2 秒）。
> 所以[示例配置](config.example.toml)默认用 **MOSS**。若你的账号 CosyVoice2 可用，可自行改回。

---

## 安装

插件就是一个目录：**把仓库放进 MaiBot 的 `plugins/` 目录即可**，目录名随意（内部分子名由插件 ID 生成）。

### 方式 A：Docker 部署（最常见）

先确认 compose 里 `plugins` 的卷映射，例如：

```yaml
services:
  core:
    volumes:
      - ./data/MaiMBot/plugins:/MaiMBot/plugins   # ← 宿主目录 : 容器目录
```

那么**在宿主**上执行（路径按你的 compose 实际位置替换）：

```bash
cd /opt/maibot/data/MaiMBot/plugins
git clone https://github.com/shinano520/maibot-plugin-siliconflow-tts.git
docker restart maim-bot-core
```

### 方式 B：裸机 / 直接跑 Python 的 MaiBot

```bash
cd /path/to/MaiBot/plugins
git clone https://github.com/shinano520/maibot-plugin-siliconflow-tts.git
# 然后重启 MaiBot，或到 WebUI 插件页点「重载」
```

### 方式 C：没有 git（下载 ZIP）

1. 打开 <https://github.com/shinano520/maibot-plugin-siliconflow-tts> → **Code → Download ZIP**；
2. 解压到 `plugins/` 目录；
3. **确认层级**：`plugins/<任意目录名>/_manifest.json` 必须直接存在。
   如果变成 `plugins/maibot-plugin-siliconflow-tts-main/maibot-plugin-siliconflow-tts-main/_manifest.json`，把里层目录整体上移一层。

### 怎么确认装好了

1. **WebUI → 插件**：出现「**硅基流动语音合成**」（id `local.siliconflow-tts`），状态为已加载；把它**启用**。
2. 日志里能看到（Docker：`docker logs --tail 200 maim-bot-core 2>&1 | grep -i siliconflow`）：

```text
插件 local.siliconflow-tts v0.1.0 加载成功
硅基流动 TTS 已加载：model=... voice=... format=mp3/44100Hz 缓存=开 运行开关=开
```

3. 部署后 `plugins/` 下会多出一个**数据目录** **`local.siliconflow-tts/`**（不是代码目录），里面是：
   `audio/`（音频缓存）、`voices/`（克隆参考音频）、`state.json`（`/tts on|off` 运行期开关）。
   而 **`config.toml` 在插件代码目录根**（Runner 首次加载时生成，与 `plugin.py` 同级）。
   **不要删数据目录**；代码目录与它互不影响。

> 装在 Docker 里时，上面这个数据目录在宿主上的位置同样是 `.../data/MaiMBot/plugins/local.siliconflow-tts/`。

> ⚠️ **装好 ≠ 会说话**：语音开关默认全关（群聊/私聊各自 `whitelist` + 空名单 = 谁都不发），
> 必须先填名单，见[语音开关（默认全关）](#语音开关默认全关)。

---

## 配置

两种等价方式，改完保存即热更新（日志出现 `TTS 配置已热更新`）。

### 方式 1：WebUI（推荐）

**WebUI → 插件 → 硅基流动语音合成 → 配置**，23 个字段都带中文说明。至少确认这三项：

| 字段 | 建议值 | 说明 |
|---|---|---|
| `api_key` | 留空 或 `sk-...` | 留空 = 读环境变量 `SILICONFLOW_API_KEY`（见下） |
| `model` | `fnlp/MOSS-TTSD-v0.5` | 实测可用；CosyVoice2 在验证账号下会挂起 |
| `voice` | `fnlp/MOSS-TTSD-v0.5:anna` | 也可只写 `anna`（自动补模型前缀）；克隆音色填 `speech:...` |

### 方式 2：手动 `config.toml`

把 [`config.example.toml`](config.example.toml) 复制到**插件根目录**并改名：

```bash
cp config.example.toml config.toml
```

文件必须包含 `[plugin]` 段（扁平写法会让插件加载失败，这是 SDK 2.16 的硬要求）。全量字段与默认值见
[CONTRACT §4](docs/CONTRACT.md)（配置模型字段表），常用项：

| 键 | 默认 | 说明 |
|---|---|---|
| `enabled` | `true` | 总开关；`false` 时回复保持纯文字 |
| `base_url` | `https://api.siliconflow.cn/v1` | 自建反代改这里 |
| `speed` / `gain` | `1.0` / `0.0` | 语速 `[0.25,4.0]`、增益 `[-10,10]` dB |
| `keep_text` | `true` | 语音之外是否保留原文字 |
| `max_chars` | `300` | 单次文本上限；**不建议超过 500**（见[已知限制](#已知限制)） |
| `cache_enabled` / `cache_ttl_hours` / `cache_max_mb` | `true` / `72` / `200` | 缓存开关、过期、容量 |
| `fallback_on_error` | `text` | 失败降级为纯文字；`fail` 仅联调用 |
| `group_policy` / `private_policy` | `whitelist` | 群/私聊开关，默认全关，见[语音开关](#语音开关默认全关) |
| `group_list` / `private_list` | `[]` | 群号 / QQ 号名单 |

### API Key 的两种放法

**A. 环境变量（Docker 推荐，key 不落盘）**

```yaml
services:
  core:
    environment:
      SILICONFLOW_API_KEY: "${SILICONFLOW_API_KEY}"
```

同目录 `.env` 写 `SILICONFLOW_API_KEY=sk-...`（`.env` 已在 `.gitignore`，**不要提交**），然后 `docker compose up -d core`。
裸机则 `export SILICONFLOW_API_KEY=sk-...`（systemd 用 `Environment=`）。

**B. WebUI 的 `api_key` 字段**

会写入 `plugins/local.siliconflow-tts/config.toml`，该文件已被 `.gitignore` 忽略。

> 两者都配时以 `api_key` 为准；留空则用环境变量。查看是否生效：`/tts status` 显示「API Key：已配置」。

---

## 语音开关（默认全关）

**装好后插件默认不会自动发声。** 群聊和私聊各有一套「策略 + 名单」，默认都是 `whitelist` + 空列表 ——
等于**谁都不发语音**。这是刻意的安全默认：避免插件误装到群后突然念稿、白烧 API 额度。

> `/tts test` 是例外：它直接发一条测试语音，**不受群/私聊开关限制**（仍受 `/tts off` 与 `enabled` 影响），用来验证链路。

### 三种模式

| 策略 | 含义 | 名单的作用 |
|---|---|---|
| `all` | 全部开启 | 名单被忽略 |
| `whitelist` | **仅**名单内的会话发声（默认） | 只有名单里的群号/QQ 会发声 |
| `blacklist` | 名单**外**的会话发声 | 名单里的群号/QQ 被静音 |

群聊看 `group_policy` + `group_list`，私聊看 `private_policy` + `private_list`，两组互不影响：

| 场景 | 用哪组配置 | 名单里填什么 |
|---|---|---|
| 群聊 | `group_policy` / `group_list` | 群号，如 `652468225` |
| 私聊 | `private_policy` / `private_list` | 对方 QQ 号 |

### WebUI 怎么填

**WebUI → 插件 → 硅基流动语音合成 → 配置**：

1. `群聊策略` / `私聊策略`：下拉框，选 `all` / `whitelist` / `blacklist`；
2. `群名单` / `私聊名单`：列表编辑器（ListFieldEditor）——**一行一条**，点 **➕** 新增一行，行右侧可删除；
3. 保存即热更新，下一条回复就按新开关判定。

### 手写 `config.toml` 也支持

```toml
[plugin]
group_policy = "whitelist"
group_list = ["652468225", "123456789"]

private_policy = "blacklist"
private_list = ["10001"]          # 这个人私聊不发声，其他人正常
```

### 脏输入会自动归一化

名单不用手动清洗，下面这些写法都认：

| 写法 | 归一化结果 |
|---|---|
| `qq:652468225` / `QQ：652468225` | `652468225` |
| `６５２４６８２２５`（全角数字） | `652468225` |
| `652468225, 123456789` 或空格分隔 | 拆成两条 |
| 一行写一个（列表形式） | 逐条归一化 |

### 没声音怎么查

1. **先看开关**（最常见原因）：WebUI 里群聊看 `group_list` 是否包含当前群号，私聊看 `private_list` 是否包含对方 QQ；
   默认空名单 = 全拒。
2. `/tts status`：确认「运行开关：开启」「API Key：已配置」、模型/音色正常。
3. 看日志有没有合成记录：容器内 `/MaiMBot/logs/app_*.jsonl` 搜 `local.siliconflow-tts`。

> ⚠️ **当前版本的开关拒绝是静默的**：被挡下时 `reply_voice` 直接返回空结果，**不写日志**。
> 所以「日志里一条合成记录都没有」通常就是被开关拒绝了，不是模型故障。
> 只有 `chat` 上下文异常时才会看到 `语音开关判定失败，按默认白名单拒绝` 的 WARN。
>
> 想绕开开关单独验证链路：发 `/tts test`（不受群/私聊开关限制）。

---

## 发出第一条语音

在 QQ 里**私聊或群里 @ 麦麦**，依次发：

> 前提：上一节的语音开关要先放行（把当前群号/QQ 加进名单），否则自动语音永远不触发；
> 只有 `/tts test` 不受开关限制。

```text
/tts status
```
确认「API Key：已配置」「模型 / 音色」「格式 / 采样率」都正确。

```text
/tts test
```
麦麦会立刻发一条测试语音（默认「你好，我是麦麦，这是一条语音链路测试。」，也可 `/tts test 自定义文本`）。
**收到能播放的语音 = 链路通了。**

之后在开关放行的会话里，只要 Planner 选中本插件（例如说「**用语音回我**」），麦麦的回复就会带语音：

```text
用语音回我：今天辛苦了
```

- 默认 `keep_text=true`：先文字、后语音，两段都在；
- Planner 传 `keep_text=false` 时只发语音（纯文字段被去掉，图片/@ 等段保留）。

不想用时发 `/tts off`，需要时 `/tts on`；彻底关闭请在 WebUI 里把 `enabled` 设为 `false`。

---

## 命令一览

| 命令 | 作用 | 权限 |
|---|---|---|
| `/tts` 或 `/tts help` | 命令帮助 | 所有人 |
| `/tts test [文本]` | 合成并**直接发送**一条语音（不走 Planner，验证用） | 所有人 |
| `/tts status` | 查看开关、模型、音色、格式、缓存、失败策略 | 所有人 |
| `/tts voices` / `/tts voice list` | 列出账号下可用音色（名称 — uri） | 所有人 |
| `/tts clone <名称> <文件名> <参考文本>` | 用 `voices/` 目录里的参考音频克隆音色 | **operator** |
| `/tts voice del <名称\|uri> [--force]`（同义 `rm`） | 删除克隆音色；正在使用的需 `--force` | **operator** |
| `/tts on` / `/tts off` | 运行期开关（写 `state.json`，不改配置） | **operator** |

---

## 音色：系统音色与克隆

### 系统音色（8 个）

| 男声 | 女声 |
|---|---|
| `alex` 沉稳 · `benjamin` 低沉 · `charles` 磁性 · `david` 欢快 | `anna` 沉稳 · `bella` 激情 · `claire` 温柔 · `diana` 欢快 |

`voice` 三种写法：

```toml
voice = "fnlp/MOSS-TTSD-v0.5:anna"   # ① 模型:音色名（推荐，明确）
voice = "anna"                        # ② 只写音色名 → 自动补成 "<当前 model>:anna"
voice = "speech:my-voice:xxx:yyyy"   # ③ 克隆音色 uri → 原样使用，不补模型前缀
```

### 克隆自己的音色（需要硅基流动实名认证）

1. **放参考音频**到数据目录的 `voices/`：

   ```bash
   # Docker：宿主上的位置
   cp 我的声音.mp3 /opt/maibot/data/MaiMBot/plugins/local.siliconflow-tts/voices/
   ```

   硬规则：**单层文件名**（不能带路径/空格、不能以 `.` 开头、不能 `..`）、后缀 `.mp3/.wav/.pcm/.opus`、**≤10MB**；
   建议 **8–10 秒**、<30 秒，内容与参考文本**逐字一致**。

2. **发命令**（operator 权限）：

   ```text
   /tts clone 我的声音 我的声音.mp3 这是参考音频里说的那句话
   ```

   三个参数：`名称`（`A-Za-z0-9_-`，≤64 字）/ `voices/` 下的文件名 / 参考音频里说的文本。

3. **把返回的 uri 填进配置** `voice`（形如 `speech:我的声音:xxx:yyyy`），保存后 `/tts status` 确认，再 `/tts test` 试听。

4. 管理：`/tts voices` 查看；`/tts voice del 我的声音` 删除。**正在使用的音色会被拒绝删除**，需先切走或加 `--force`。

> 克隆音色绑定账号；换账号或删掉后需要重新克隆。

---

## 音频格式与 QQ 兼容性

| 格式 | 合法采样率 | 状态 |
|---|---|---|
| **mp3** | 32000、44100 | ✅ **真机验证**（默认） |
| wav | 8000、16000、24000、32000、44100 | 代码支持，未真机投递验证 |
| opus | 48000（唯一） | 代码支持，未真机投递验证 |

实测结论（[VERIFY R1](docs/VERIFY.md)）：

- OneBot 发送 mp3 语音 → `retcode:0`，消息成功入库；
- 回读同一消息，`record` 段文件名变成 `*.amr`，但下载后文件头是 **`\x02#!SILK_`** —— 即 **QQ 服务端把 mp3 转成了 SILK**；
- 所以**插件不需要自己做 ffmpeg / silk 转码**，容器里没有 `ffmpeg` 也能正常发语音；
- ⚠️ **设备联系人**（「我的手机」「我的电脑」）会拒绝 `record` 段（`retcode 1400`），这是 QQ 限制，请用**好友私聊或群聊**测试。

---

## 常见问题

| 现象 | 先查 | 处理 |
|---|---|---|
| 只有文字、没有语音 | 先查**语音开关**（`group_list` / `private_list`），再看 `/tts status` 与日志 | 默认 `whitelist` + 空名单 = 全拒，且被开关挡下**不写日志**；开关放行后仍无语音，再查 key / 模型 / 降级原因（`fallback_on_error=text` 会降级为文字并记 WARN） |
| 等 60–180 秒后没有语音 | 模型 | CosyVoice2 在验证账号下会挂起；把 `model` 换成 `fnlp/MOSS-TTSD-v0.5`（`voice` 同步换） |
| 返回 401 / 403 | key / 实名 | key 错误或账号未实名（克隆音色也必须实名） |
| 语音只念了前半段 | `max_chars` | 实测 >约 500 字时音频在 ~163 秒饱和；保持 `max_chars=300` |
| 同一句每次都调 API | `cache_enabled` | 确认缓存为「开」；换音色/语速/格式/文本都会换缓存键，属预期 |
| 命令发了没反应 | 平台/权限 | 仅 QQ 生效；`clone/del/on/off` 需 operator 权限；前缀是 `/tts` 不是 `/TTS` |
| 想确认插件在跑 | 日志 | 容器内 `/MaiMBot/logs/app_*.jsonl` 搜 `local.siliconflow-tts`；宿主挂载在 `data/MaiMBot/logs/` |
| 重装后配置丢失 | 数据目录 | 配置与缓存都在 `plugins/local.siliconflow-tts/`，只删代码目录不会丢配置 |

---

## 已知限制

1. **音质有天花板**：`MOSS-TTSD` 是对话式 TTS（TTSD = Text-to-Speech **Dialogue**），音色克隆是副产品、不是主业。
   实测对比过两组参考音频（9.95 秒快语速 / 15.94 秒慢语速），**用户盲听判定短参考反而更自然**。
   换更长的参考音频不会更好——别再折腾素材了。
2. **`max_chars` 不要调高**：实测输出在 **163.3 秒**处饱和——

   ```text
   300 字  → 1,560,266 B / 97.4s
   800 字  → 2,613,523 B / 163.3s
   2000 字 → 2,613,523 B / 163.3s   ← 和 800 字完全一样
   ```

   约 500 字以上尾部就不朗读了。默认 300 是安全值，调高只是白花钱。
3. **QQ 会把 mp3 转成 SILK**：插件发送 mp3（44.1kHz），QQ 服务端转成 SILK（约 1/5 体积）。
   这是平台行为，不是插件在转码——所以**插件不依赖 ffmpeg，也不需要出站转码**，`audio.py` 的转码分支是有意不启用的。
   音质在 QQ 里会进一步下降，这是语音消息这个载体的固有上限（证据见[音频格式与 QQ 兼容性](#音频格式与-qq-兼容性)）。
4. **`auto_voice` 仍是占位**：`ReplyExtension` 无法强制参与，必须 Planner 主动选择 `voice` 扩展才会触发；
   `auto_voice=true` 目前不生效（后续版本才用 Hook 注入，见 PLAN R8）。

> 其他运行期注意：仅 QQ 生效；`/tts on|off` 只写 `state.json`、不改 `config.toml`；
> 克隆参考音频要手动放进 `voices/`；`wav` / `opus` 未做真机投递验证。

---

## 安全须知：API Key

- **绝不把真实 key 写进源码、README、issue、截图或提交历史**；示例一律 `sk-xxxx` 或留空。
- 推荐环境变量 `SILICONFLOW_API_KEY`；写进 WebUI 时落在 `config.toml`，该文件已被 `.gitignore` 忽略。
- `.gitignore` 已覆盖：`config.toml`、`.env*`、`*.key`、`*.pem`、`secrets/`、`voices/`、生成的音频。
- 插件日志与错误信息里的 key 会被脱敏成 `sk-****`；`/tts status` 只显示「已配置/未配置」。
- 若曾误提交：先到硅基流动控制台 **revoke** 该 key，再清理 git 历史，见 [CONTRACT §12](docs/CONTRACT.md#12-密钥安全红线硬约束不可协商)。

---

## 文档与许可证

| 文档 | 内容 |
|---|---|
| [docs/PLAN.md](docs/PLAN.md) | 方案书、里程碑、风险清单（R1–R8） |
| [docs/CONTRACT.md](docs/CONTRACT.md) | 冻结接口：manifest、模块签名、配置字段表、消息协议、密钥红线 |
| [docs/VERIFY.md](docs/VERIFY.md) | 真机验证报告（R1–R7，含原始返回与复现命令） |
| [config.example.toml](config.example.toml) | 可直接复制的配置示例 |

MIT License。MaiBot 与本插件无隶属关系；硅基流动的模型、音色与计费以其官方文档为准。
