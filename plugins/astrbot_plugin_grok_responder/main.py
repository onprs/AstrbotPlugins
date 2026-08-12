"""@Grok 群消息响应插件。

群聊中出现任何包含 "@grok"（不区分大小写）的消息时，将事件标记为
"唤醒"，其余处理全部交给 AstrBot 主流程：由主流程调用 LLM 回复，
完整共享会话上下文、身份提示、知识库等能力；插件仅承担触发作用。

设计要点：

- 触发匹配：消息纯文本中不区分大小写包含 "@grok"；同时检查消息链中
  At 段（@ 了昵称含 grok 的成员）作为兜底。
- 触发方式：命中后仅设置 `event.is_at_or_wake_command = True`（与
  @ 机器人、唤醒词等效）。不产生结果、不发送消息、不直接调用 LLM，
  主流程在插件 handler 之后检查该标志并自动发起 LLM 请求。
- 上下文共享：回复由主流程生成，会话历史正常记录，与 @ 机器人的
  对话完全一致。
- 防刷屏：同一群在冷却时间内只触发第一条命中消息，间隔可配置。
- 无副作用：未命中的消息完全不受影响，AstrBot 原有流程保持不变。
"""

from __future__ import annotations

import re
import time
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

# 匹配 "@grok"，@ 与 grok 必须紧邻，大小写不敏感
_AT_GROK_RE = re.compile(r"@grok", re.IGNORECASE)


def contains_at_grok(message_str: str, at_names: list[str] | None = None) -> bool:
    """判断消息文本或 @ 消息段昵称中是否包含 @grok（不区分大小写）。"""
    if _AT_GROK_RE.search(message_str or ""):
        return True
    for name in at_names or []:
        if "grok" in (name or "").lower():
            return True
    return False


class GrokResponder(Star):
    """群聊 @grok 触发 AstrBot 主流程回复的插件。"""

    def __init__(self, context: Context, config: dict[str, Any] | None = None):
        super().__init__(context, config)
        self.config = config or {}
        self.enabled = bool(self.config.get("enable", True))
        self.cooldown_seconds = max(0, int(self.config.get("cooldown_seconds", 15)))
        # group_id -> 最近一次触发时间（monotonic 秒）
        self._last_trigger_at: dict[str, float] = {}

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent) -> None:
        """监听所有群消息；命中 @grok 时标记唤醒，交由主流程处理。"""
        if not self.enabled:
            return
        message_obj = event.message_obj
        if message_obj is None:
            return
        # 忽略机器人自己发出的消息
        if message_obj.sender and message_obj.sender.user_id == message_obj.self_id:
            return
        group_id = message_obj.group_id or ""

        # 兜底：消息链中 At 段的昵称（OneBot 下昵称也会进 message_str，这里双保险）
        at_names = [
            str(getattr(comp, "name", "") or "")
            for comp in (message_obj.message or [])
            if "at" in str(getattr(comp, "type", "")).lower()
        ]
        if not contains_at_grok(message_obj.message_str or event.message_str, at_names):
            return

        if not self._passes_cooldown(group_id):
            logger.debug(f"GrokResponder: 群 {group_id} 处于冷却期，跳过本次触发")
            return

        # 标记为唤醒，与 @ 机器人等效；主流程随后自动调用 LLM 回复
        event.is_at_or_wake_command = True
        event.is_wake = True
        logger.info(f"GrokResponder: 群 {group_id} 命中 @grok，已唤醒主流程处理")

    def _passes_cooldown(self, group_id: str) -> bool:
        """同群冷却检查；通过时记录本次触发时间。"""
        now = time.monotonic()
        last = self._last_trigger_at.get(group_id, 0.0)
        if self.cooldown_seconds > 0 and now - last < self.cooldown_seconds:
            return False
        self._last_trigger_at[group_id] = now
        return True
