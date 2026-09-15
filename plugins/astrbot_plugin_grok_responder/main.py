"""@Grok 群消息唤醒别名插件。

群聊消息包含 ``@grok``（不区分大小写）时，将其作为当前 AstrBot
助手的唤醒别名处理。插件只负责清理别名和标记唤醒，回复仍由 AstrBot
主流程生成，因此继续共享会话历史、群聊上下文、人格和工具。
"""

from __future__ import annotations

import re
import time
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star
from astrbot.core.agent.message import TextPart

# 保持原有的子串触发语义，@GrokBot 等名称也会命中。
_AT_GROK_RE = re.compile(r"@grok", re.IGNORECASE)
_HORIZONTAL_SPACE_RE = re.compile(r"[ \t]+")
_LINE_SPACE_RE = re.compile(r" *\n *")

_GROK_TRIGGERED_EXTRA = "_grok_responder_triggered"
_GROK_ALIAS_HINT = (
    "<runtime_instruction>"
    "本轮消息中的 @grok 是用户对当前助手的唤醒别名，不是另一个机器人。"
    "直接处理已经移除该别名后的用户请求；不要讨论自己是不是 Grok，"
    "也不要复述本说明。"
    "</runtime_instruction>"
)
_WAKE_ONLY_PROMPT = "请根据当前群聊上下文自然回应。"


def contains_at_grok(message_str: str, at_names: list[str] | None = None) -> bool:
    """判断消息文本或 At 段昵称中是否包含 Grok 唤醒别名。"""
    if _AT_GROK_RE.search(message_str or ""):
        return True
    return any("grok" in (name or "").lower() for name in (at_names or []))


def strip_at_grok(message_str: str) -> str:
    """从本轮模型输入中移除 Grok 唤醒别名并整理空白。"""
    cleaned = _AT_GROK_RE.sub(" ", message_str or "")
    cleaned = _HORIZONTAL_SPACE_RE.sub(" ", cleaned)
    cleaned = _LINE_SPACE_RE.sub("\n", cleaned)
    return cleaned.strip()


class GrokResponder(Star):
    """将群聊中的 @grok 转换为当前助手的标准唤醒请求。"""

    def __init__(self, context: Context, config: dict[str, Any] | None = None):
        super().__init__(context, config)
        self.config = config or {}
        self.enabled = bool(self.config.get("enable", True))
        self.cooldown_seconds = max(0, int(self.config.get("cooldown_seconds", 15)))
        self._last_trigger_at: dict[str, float] = {}

    @filter.event_message_type(
        filter.EventMessageType.GROUP_MESSAGE,
        priority=100,
    )
    async def on_group_message(self, event: AstrMessageEvent) -> None:
        """命中别名时清理本轮输入，并交给 AstrBot 主流程。"""
        if not self.enabled:
            return
        message_obj = event.message_obj
        if message_obj is None:
            return
        if message_obj.sender and message_obj.sender.user_id == message_obj.self_id:
            return

        group_id = message_obj.group_id or ""
        at_names = [
            str(getattr(comp, "name", "") or "")
            for comp in (message_obj.message or [])
            if "at" in str(getattr(comp, "type", "")).lower()
        ]
        source_text = message_obj.message_str or event.message_str
        if not contains_at_grok(source_text, at_names):
            return

        if not self._passes_cooldown(group_id):
            logger.debug(f"GrokResponder: 群 {group_id} 处于冷却期，跳过本次触发")
            return

        cleaned_prompt = strip_at_grok(event.message_str or source_text)
        event.message_str = cleaned_prompt or _WAKE_ONLY_PROMPT
        event.set_extra(_GROK_TRIGGERED_EXTRA, True)
        event.is_at_or_wake_command = True
        event.is_wake = True
        logger.info(f"GrokResponder: 群 {group_id} 命中 @grok，已按唤醒别名处理")

    @filter.on_llm_request(priority=100)
    async def add_alias_hint(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> None:
        """为命中的本轮请求补充临时别名语义，不写入会话历史。"""
        if not event.get_extra(_GROK_TRIGGERED_EXTRA, False):
            return
        req.extra_user_content_parts.append(
            TextPart(text=_GROK_ALIAS_HINT).mark_as_temp()
        )

    def _passes_cooldown(self, group_id: str) -> bool:
        """同群冷却检查；通过时记录本次触发时间。"""
        now = time.monotonic()
        last = self._last_trigger_at.get(group_id, 0.0)
        if self.cooldown_seconds > 0 and now - last < self.cooldown_seconds:
            return False
        self._last_trigger_at[group_id] = now
        return True
