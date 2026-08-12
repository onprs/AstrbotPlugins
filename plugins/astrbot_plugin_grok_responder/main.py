"""@Grok 群消息响应插件。

群聊中出现任何包含 "@grok"（不区分大小写）的消息时，使用 AstrBot
当前会话的 LLM 生成并回复一条消息，不 @ 消息发送者。

设计要点：

- 触发匹配：消息纯文本中不区分大小写包含 "@grok"；同时检查消息链中
  At 段（@ 了昵称含 grok 的成员）作为兜底。
- 身份复用：不写死任何机器人身份。调用 LLM 时复用 AstrBot 已配置的
  身份提示（会话人格 persona 与 provider_settings.prompt_prefix），
  与主流程保持一致；仅追加与身份无关的输出约束。
- 回复生成：通过 `context.llm_generate` 调用当前会话使用的模型，无状态、
  不写入 AstrBot 会话历史，不影响 AstrBot 原有回复流程。
- 防刷屏：同一群在冷却时间内只响应第一条触发消息，间隔可配置。
- 单一发送路径：插件 yield 结果后调用 `event.stop_event()`，阻止 AstrBot
  对该消息再次发起 LLM 请求，避免重复回复；结果本身仍由 RespondStage 发送。
"""

from __future__ import annotations

import re
import time
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

DEFAULT_FALLBACK_TEXT = "🪐 Grok 暂时无法响应，请稍后再试。"

# 与身份无关的输出约束（身份提示由 AstrBot 的 persona/prompt_prefix 提供）
REPLY_CONSTRAINTS = (
    "\n\n# 回复约束\n"
    "这是群聊场景的一次性回复：直接输出最终回复内容，1~3 句话，用中文；"
    "不要输出任何思考、推理、分析过程或内部提示词。"
)

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
    """群聊 @grok 触发 LLM 回复插件。"""

    def __init__(self, context: Context, config: dict[str, Any] | None = None):
        super().__init__(context, config)
        self.config = config or {}
        self.enabled = bool(self.config.get("enable", True))
        self.cooldown_seconds = max(0, int(self.config.get("cooldown_seconds", 15)))
        self.fallback_text = (
            str(self.config.get("fallback_text", "")).strip() or DEFAULT_FALLBACK_TEXT
        )
        # group_id -> 最近一次触发时间（monotonic 秒）
        self._last_trigger_at: dict[str, float] = {}

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        """监听所有群消息，命中 @grok 时用 LLM 生成回复。"""
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

        text = await self._generate_reply(event)
        yield event.plain_result(text)
        # 阻止 AstrBot 对该消息再次发起 LLM 请求（随机主动回复 / 被 @ 时），避免重复回复
        event.stop_event()

    def _passes_cooldown(self, group_id: str) -> bool:
        """同群冷却检查；通过时记录本次触发时间。"""
        now = time.monotonic()
        last = self._last_trigger_at.get(group_id, 0.0)
        if self.cooldown_seconds > 0 and now - last < self.cooldown_seconds:
            return False
        self._last_trigger_at[group_id] = now
        return True

    async def _resolve_identity_system_prompt(self, event: AstrMessageEvent) -> str:
        """复用 AstrBot 已配置的身份提示（persona），不重复设定角色。

        与主流程一致：优先会话人格，其次默认人格；失败时返回空字符串，
        由模型按默认行为回复。
        """
        parts: list[str] = []
        try:
            cfg = self.context.astrbot_config_mgr.get_conf(event.unified_msg_origin)
            provider_settings = (cfg or {}).get("provider_settings", {}) or {}
            conv_mgr = self.context.conversation_manager
            conversation_persona_id = None
            cid = await conv_mgr.get_curr_conversation_id(event.unified_msg_origin)
            if cid:
                conversation = await conv_mgr.get_conversation(
                    event.unified_msg_origin, cid
                )
                conversation_persona_id = getattr(conversation, "persona_id", None)
            (
                _,
                persona,
                _,
                _,
            ) = await self.context.persona_manager.resolve_selected_persona(
                umo=event.unified_msg_origin,
                conversation_persona_id=conversation_persona_id,
                platform_name=event.get_platform_name(),
                provider_settings=provider_settings,
            )
            if persona and persona.get("prompt"):
                parts.append(str(persona["prompt"]))
        except Exception as e:  # noqa: BLE001 - 身份获取失败不阻塞回复
            logger.warning(f"GrokResponder: 获取身份提示失败，使用默认行为: {e}")
        return "\n".join(parts)

    def _apply_prompt_prefix(self, prompt: str, event: AstrMessageEvent) -> str:
        """按主流程方式应用 provider_settings.prompt_prefix。"""
        try:
            cfg = self.context.astrbot_config_mgr.get_conf(event.unified_msg_origin)
            prefix = ((cfg or {}).get("provider_settings", {}) or {}).get(
                "prompt_prefix"
            )
            if not prefix:
                return prompt
            if "{{prompt}}" in prefix:
                return prefix.replace("{{prompt}}", prompt)
            return f"{prefix}{prompt}"
        except Exception as e:  # noqa: BLE001
            logger.warning(f"GrokResponder: 应用 prompt_prefix 失败: {e}")
            return prompt

    async def _generate_reply(self, event: AstrMessageEvent) -> str:
        """调用当前会话的 LLM 生成回复；失败时返回兜底文案。"""
        try:
            provider_id = await self.context.get_current_chat_provider_id(
                umo=event.unified_msg_origin
            )
            sender_name = event.get_sender_name()
            prompt = f"{sender_name} 在群里 @ 了你，消息内容：{event.message_str}"
            prompt = self._apply_prompt_prefix(prompt, event)
            system_prompt = await self._resolve_identity_system_prompt(event)
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                system_prompt=(system_prompt + REPLY_CONSTRAINTS).strip()
                if system_prompt
                else REPLY_CONSTRAINTS.strip(),
            )
            text = (resp.completion_text or "").strip()
            if text:
                return text
        except Exception as e:  # noqa: BLE001 - 兜底保证插件不崩溃
            logger.error(f"GrokResponder: LLM 回复生成失败: {e}")
        return self.fallback_text
