# 表情包插件

`astrbot_plugin_sticker` 将 WebUI 管理的 QQ 收藏表情提供给 LLM，由模型结合当前群聊语境决定发送表情、表情加短文字或只发文字。

## 行为

- 仅向 `aiocqhttp` 会话注入 `send_sticker`，支持 AstrBot 主动回复和被 @ 回复。
- 发送保留 OneBot `image.sub_type=1`，并通过 AstrBot 会话路由写入群消息历史。
- 只发表情、发送冷却和机器人回推防护均按完整会话隔离。
- 上传后尝试加入机器人 QQ 收藏；NapCat 暂时不可用时仍保留本地副本并可发送。
- 管理入口位于插件详情页的“表情包管理”。

## 备注

备注直接决定模型能否选对表情。建议先写适用情绪或场景，再补充作品与角色来源，例如“被夸奖时害羞回应；角色来源……”。空备注的表情不会提供给 LLM。

## 配置

- `cooldown_seconds`：同一会话的发送冷却。
- `allow_private`：是否允许私聊使用。
- `max_inject`：单次提供给 LLM 的表情数量。
- `max_description_chars`：单张备注注入长度，默认 120 字。
- `max_stickers`：本地收藏数量上限，最高 400。
- `max_file_mb`、`max_image_pixels`、`allowed_exts`：上传校验限制。

QQ 收藏描述同步依赖 NapCat 返回有效 `emojiId`；无法同步时，本地 LLM 备注仍会正常保存和使用。
