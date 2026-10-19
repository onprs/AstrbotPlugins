# 戳一戳回应

`astrbot_plugin_poke_reply` 让机器人在被戳一戳时按配置回应。

## 功能

- 只响应目标为机器人的戳一戳，别人之间互戳不会触发。
- 两种回应方式可在 AstrBot 插件配置页面切换：
  - **随机回复文本**：从列表中随机发送一条文本。
  - **LLM 回复**：用配置的提示词触发 AstrBot 原有 LLM 回复，使用当前人设、对话和上下文。
- 可选的同用户冷却时间，避免连续戳一戳造成刷屏。

## 安装

将插件目录放入 AstrBot 的 `data/plugins/`，在 WebUI 的插件页启用。

## 配置

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `enable` | bool | `true` | 总开关 |
| `response_mode` | string | `random` | 回应方式：`random` 为随机回复文本，`llm` 为 LLM 回复 |
| `reply_texts` | list | 见配置页 | 随机回复文本列表 |
| `llm_prompt` | text | 见配置页 | LLM 回复提示词，支持 `{username}`、`{user_id}`、`{scene}` 占位符 |
| `cooldown_seconds` | int | `0` | 同一会话内同一用户的冷却时间，`0` 表示不限制 |

## 说明

- 仅支持 `aiocqhttp` 平台（NapCat、go-cqhttp 等）。
- LLM 回复会记录到当前对话，后续对话可以继续引用这次互动。
- 回复文本默认为通用短句，建议按机器人的实际人设调整。
