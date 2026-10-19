"""戳一戳回应插件：机器人被戳时随机回复文本或触发原有 LLM 回复。"""

from __future__ import annotations

import random
import time
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Poke, Reply
from astrbot.api.star import Context, Star

SUPPORTED_PLATFORM = "aiocqhttp"

MODE_RANDOM = "random"
MODE_LLM = "llm"

DEFAULT_REPLY_TEXTS = [
    "在呢，怎么啦？",
    "别戳啦，有事直说。",
    "戳我做什么？",
    "我在这儿。",
]
DEFAULT_LLM_PROMPT = (
    "用户 {username}（ID：{user_id}）在{scene}里戳了你一下。"
    "请保持当前人设，结合最近的对话上下文，自然地回应这次戳一戳，"
    "只输出要发送给用户的内容。"
)

# 标记当前事件由戳一戳插件处理，供发送前钩子识别。
POKE_REPLY_EXTRA = "_poke_reply_triggered"
POKE_QUOTE_FILTER_EXTRA = "_poke_reply_quote_filter_installed"

# 冷却记录清理阈值，避免长期运行时字典无限增长。
COOLDOWN_CACHE_LIMIT = 500
COOLDOWN_RETENTION_SECONDS = 600


def _as_non_negative_float(value: Any, default: float) -> float:
    """把配置值收敛为不小于 0 的浮点数。"""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if parsed < 0:
        return default
    return parsed


def _as_text_list(value: Any) -> list[str]:
    """把配置中的列表整理为非空字符串列表。"""
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        text = str(item).strip()
        if text:
            result.append(text)
    return result


def _poke_target(component: Poke) -> str | None:
    """读取 Poke 消息段的目标 QQ，兼容新旧字段。"""
    for value in (getattr(component, "id", None), getattr(component, "qq", None)):
        if value in (None, "", 0, "0"):
            continue
        target = str(value).strip()
        if target and target != "0":
            return target
    return None


class PokeToBotFilter(filter.CustomFilter):
    """只放行目标为机器人的戳一戳，避免唤醒普通消息。"""

    def filter(self, event: AstrMessageEvent, cfg) -> bool:
        if event.get_platform_name() != SUPPORTED_PLATFORM:
            return False
        if str(event.get_sender_id()) == str(event.get_self_id()):
            return False

        bot_id = str(event.get_self_id())
        for component in event.get_messages():
            if isinstance(component, Poke) and _poke_target(component) == bot_id:
                return True
        return False


class PokeReply(Star):
    """按配置回应机器人收到的戳一戳。"""

    def __init__(self, context: Context, config: dict[str, Any] | None = None) -> None:
        super().__init__(context)
        config = config or {}

        self.enabled = bool(config.get("enable", True))

        mode = str(config.get("response_mode") or MODE_RANDOM).strip().lower()
        self.response_mode = mode if mode in {MODE_RANDOM, MODE_LLM} else MODE_RANDOM

        configured_reply_texts = config.get("reply_texts")
        self.reply_texts = (
            _as_text_list(configured_reply_texts)
            if configured_reply_texts is not None
            else list(DEFAULT_REPLY_TEXTS)
        )

        self.llm_prompt = str(config.get("llm_prompt") or "").strip()
        if not self.llm_prompt:
            self.llm_prompt = DEFAULT_LLM_PROMPT

        self.cooldown_seconds = _as_non_negative_float(
            config.get("cooldown_seconds"),
            0.0,
        )
        self._last_reply_at: dict[tuple[str, str], float] = {}

    @filter.custom_filter(PokeToBotFilter)
    async def handle_poke(self, event: AstrMessageEvent):
        """只处理目标为机器人的戳一戳通知。"""
        if not self.enabled:
            return
        if event.get_platform_name() != SUPPORTED_PLATFORM:
            return

        target = self._get_poke_target(event)
        if target is None or target != str(event.get_self_id()):
            return
        if str(event.get_sender_id()) == str(event.get_self_id()):
            return

        if self._cooldown_active(event):
            return

        event.set_extra(POKE_REPLY_EXTRA, True)

        if self.response_mode == MODE_LLM:
            async for result in self._handle_llm(event):
                yield result
            return

        async for result in self._handle_random_text(event):
            yield result

    @staticmethod
    def _get_poke_target(event: AstrMessageEvent) -> str | None:
        for component in event.get_messages():
            if isinstance(component, Poke):
                return _poke_target(component)
        return None

    def _cooldown_active(self, event: AstrMessageEvent) -> bool:
        if self.cooldown_seconds <= 0:
            return False

        key = (event.unified_msg_origin, str(event.get_sender_id()))
        now = time.monotonic()
        last_reply_at = self._last_reply_at.get(key)
        if last_reply_at is not None and now - last_reply_at < self.cooldown_seconds:
            return True

        self._last_reply_at[key] = now
        if len(self._last_reply_at) > COOLDOWN_CACHE_LIMIT:
            expire_before = now - max(
                self.cooldown_seconds,
                COOLDOWN_RETENTION_SECONDS,
            )
            self._last_reply_at = {
                item_key: timestamp
                for item_key, timestamp in self._last_reply_at.items()
                if timestamp >= expire_before
            }
        return False

    async def _handle_random_text(self, event: AstrMessageEvent):
        """从回复文本列表中随机发送一条。"""
        if not self.reply_texts:
            logger.warning("戳一戳回复文本列表为空，跳过本次回应。")
            event.stop_event()
            return

        yield event.plain_result(random.choice(self.reply_texts))
        event.stop_event()

    async def _handle_llm(self, event: AstrMessageEvent):
        """用配置的提示词走 AstrBot 原有 LLM 回复链路。"""
        prompt = self._format_prompt(event)
        if not prompt:
            logger.warning("戳一戳 LLM 提示词为空，跳过本次回应。")
            event.stop_event()
            return

        conversation = None
        try:
            conversation = await self._get_conversation(event)
        except Exception:  # noqa: BLE001 - 对话服务异常时仍应回复
            logger.exception("获取当前对话失败，本次戳一戳将不带对话上下文请求 LLM。")

        yield event.request_llm(prompt=prompt, conversation=conversation)
        event.stop_event()

    async def _get_conversation(self, event: AstrMessageEvent):
        """获取当前对话；没有时创建，以应用人设并记录对话历史。"""
        manager = self.context.conversation_manager
        umo = event.unified_msg_origin
        platform_id = event.get_platform_id()

        conversation_id = await manager.get_curr_conversation_id(umo)
        if not conversation_id:
            conversation_id = await manager.new_conversation(umo, platform_id)

        conversation = await manager.get_conversation(umo, conversation_id)
        if conversation is None:
            conversation_id = await manager.new_conversation(umo, platform_id)
            conversation = await manager.get_conversation(umo, conversation_id)
        return conversation

    def _format_prompt(self, event: AstrMessageEvent) -> str:
        sender_id = str(event.get_sender_id())
        username = self._get_sender_name(event) or sender_id
        scene = "私聊" if event.is_private_chat() else "群聊"
        return (
            self.llm_prompt.replace("{username}", username)
            .replace("{user_id}", sender_id)
            .replace("{scene}", scene)
        )

    @staticmethod
    def _get_sender_name(event: AstrMessageEvent) -> str:
        raw_message = getattr(event.message_obj, "raw_message", None)
        if isinstance(raw_message, dict):
            sender = raw_message.get("sender")
            if isinstance(sender, dict):
                name = sender.get("card") or sender.get("nickname")
                if name:
                    return str(name).strip()

        name = event.get_sender_name()
        if name:
            name = str(name).strip()
            if name and name not in {"N/A", str(event.get_sender_id())}:
                return name
        return ""

    @filter.on_decorating_result()
    async def strip_invalid_poke_quote(self, event: AstrMessageEvent) -> None:
        """移除通知事件合成消息 ID 产生的无效引用。

        戳一戳通知没有可供 OneBot 引用的真实消息 ID。开启全局“引用回复”后，
        AstrBot 会用事件中的合成 ID 添加 Reply 消息段，导致发送失败。这里在
        真正发送时过滤掉该引用，不影响普通消息的引用回复。
        """
        if not event.get_extra(POKE_REPLY_EXTRA):
            return
        if event.get_extra(POKE_QUOTE_FILTER_EXTRA):
            return

        synthetic_message_id = str(getattr(event.message_obj, "message_id", ""))
        if not synthetic_message_id:
            return

        original_send = event.send

        async def send_without_notice_quote(message_chain) -> None:
            message_chain.chain = [
                component
                for component in message_chain.chain
                if not (
                    isinstance(component, Reply)
                    and str(component.id) == synthetic_message_id
                )
            ]
            await original_send(message_chain)

        event.send = send_without_notice_quote
        event.set_extra(POKE_QUOTE_FILTER_EXTRA, True)
