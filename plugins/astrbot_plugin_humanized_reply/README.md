# 拟人化群聊回复

`astrbot_plugin_humanized_reply`：让机器人在群聊里像真人一样选择表达形式与表达风格。

## 功能

- **混合使用 @、引用和普通消息**：原本 AstrBot 的「回复时 @ 发送者」「回复时引用消息」是全局开关，开启后每条回复都带着相同的指向。插件接管这两个开关，改由模型按需表达指向，模型没有表态时再按场景概率兜底，因此群里会自然混合出现点名回应、引用较早消息和纯文本接话。
- **有观点的主动回复**：概率触发的主动回复不再只盯着那一条触发消息，而是结合整段群聊上下文形成自己的判断再开口。
- **减少附和式复读**：在提示词层面禁止把对方的话换一种说法再说一遍，并可按配置在回复生成后检测和处理复读。

## 安装与使用

1. 将插件目录放入 AstrBot 的 `data/plugins/`，在 WebUI 的插件页启用。
2. 插件启动时自动接管全局的「回复时 @ 发送者」「回复时引用消息」开关，无需手动关闭。

## 说明

- 仅在群聊生效，私聊保持普通回复。
- 使用流式输出时，AstrBot 会跳过发送前的装饰阶段，此时插件无法添加 @ 或引用。建议关闭流式输出。
- 在 WebUI 里重新加载插件不会重建消息流水线，「禁用插件后恢复原开关」需要在 WebUI 保存一次配置或重启 AstrBot 后生效。
- 如果自行开启了全局的「回复时 @ 发送者」「回复时引用消息」，会与本插件叠加。插件检测到回复里已经有指向时会跳过一次并记录日志。
- 指向参考表只列出模型本轮能看到内容的群消息。模型指向更早、它看不到内容的消息时，插件会丢弃该指向并按普通消息发送。
- 配置中的概率为 0 到 1 的小数，`0` 表示从不使用该形式。

## 配置

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `enable` | bool | `true` | 总开关 |
| `mention.passive_probability` | float | `0.10` | 被动回复的 @ 概率 |
| `mention.active_probability` | float | `0.08` | 主动回复的 @ 概率 |
| `quote.passive_probability` | float | `0.03` | 被动回复的引用概率 |
| `quote.active_probability` | float | `0.05` | 主动回复的引用概率 |
| `quote.max_age_seconds` | int | `600` | 引用目标的时效上限 |
| `pointing_protocol.enable` | bool | `true` | 允许模型用标记表达指向 |
| `pointing_protocol.max_recent_messages` | int | `30` | 参考表最多列出的消息数 |
| `active_reply_style.enable` | bool | `true` | 为主动回复注入表达风格指令 |
| `anti_echo.mode` | string | `observe` | 附和检测的处理方式，可选 `observe`、`strip`、`rewrite` |
| `anti_echo.similarity_threshold` | float | `0.6` | 附和判定相似度阈值 |
| `anti_echo.rewrite_rate_limit_per_session` | int | `2` | `rewrite` 模式下的重写次数上限 |
| `ledger.persist` | bool | `true` | 持久化群消息台账 |
| `ledger.max_messages_per_group` | int | `200` | 每群保留的消息数 |
| `ledger.flush_interval_seconds` | int | `30` | 台账落盘间隔 |
| `scope.groups` | list | `[]` | 生效的群号，留空表示所有群 |
| `scope.platforms` | list | `["aiocqhttp"]` | 生效的平台，留空表示所有平台 |
| `apply_to_non_llm` | bool | `false` | 对指令、工具等非模型回复也添加指向 |
