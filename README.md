# maibot-plugin-siliconflow-tts

让 [MaiBot（麦麦）](https://github.com/Mai-with-u/MaiBot) 用 [硅基流动](https://siliconflow.cn) 的语音合成说话。

> **状态：立项中。** 目前只有方案书，代码尚未开始。设计细节见 [docs/PLAN.md](docs/PLAN.md)。

## 这是什么

麦麦本体只有语音识别（`[voice] enable_asr`），**没有语音合成**——官方文档也写明「音频合成由插件自行完成（SDK 不内置 TTS）」。本插件补上这一块：

- 麦麦需要语音回复时，Planner 在 `reply` 工具里选中本插件
- 插件把已生成的文字交给硅基流动 TTS 合成音频
- 作为 QQ 语音消息发出，可选择是否同时保留文字

## 支持的音色

由硅基流动提供，系统预置 8 种：

| 男声 | 女声 |
|---|---|
| `alex` 沉稳 · `benjamin` 低沉 · `charles` 磁性 · `david` 欢快 | `anna` 沉稳 · `bella` 激情 · `claire` 温柔 · `diana` 欢快 |

也支持自己上传的克隆音色（需实名认证）。

## 安装（待实现）

```bash
cd MaiBot/plugins
git clone https://github.com/shinano520/maibot-plugin-siliconflow-tts.git
```

然后在 WebUI 插件页填入硅基流动 API Key 并启用。

## 要求

- MaiBot ≥ 8.15
- `maibot-plugin-sdk` ≥ 2.14.0（回复扩展的 `platforms` 需要）
- 硅基流动 API Key（[获取](https://cloud.siliconflow.cn/account/ak)）

## 设计要点

| 关注点 | 做法 |
|---|---|
| 不拖垮正常回复 | TTS 失败自动降级为纯文字，绝不让一次语音合成搞崩整条回复 |
| 不重复计费 | 按 `模型+音色+语速+文本` 哈希缓存音频 |
| 念得自然 | `prepare` 阶段给 Replyer 注入「适合朗读」的约束 |
| 不越界 | 只改写待发消息，发送/历史/监控全交给主程序 |

完整设计、风险清单、里程碑：**[docs/PLAN.md](docs/PLAN.md)**

## 许可证

MIT
