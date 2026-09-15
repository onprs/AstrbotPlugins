"""拟人化回复插件：接管群聊回复的表达形式与表达风格。

解决的问题：

1. 官方 ``reply_with_mention`` / ``reply_with_quote`` 是全局开关，开启后所有回复
   统一带 @ 与引用。本插件接管这两个开关，改由模型用指向标记表达指向，模型未
   表态时按场景概率兜底，从而混合出现 @、引用和普通消息。
2. 概率主动回复的 LLM 请求以触发消息为主 prompt，模型容易只回应抽样点。本插件
   为主动回复注入表达风格指令，要求先形成自己的判断。
3. 主动回复容易把被回应内容换一种说法复述。本插件在 prompt 层禁止附和，并在
   响应后按 ``anti_echo.mode`` 检测或处理。

与 ``astrbot_plugin_group_reply_guard`` 的分工：guard 负责回应目标选择与工具防
护，本插件负责表达形式与表达风格，两边注入的指令文本互不重复。
"""

from __future__ import annotations

import asyncio
import random
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.platform import MessageType
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star
from astrbot.core.agent.message import TextPart
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .modules import anti_echo, config_takeover, decorator, pointing_protocol
from .modules.message_ledger import (
    LedgerEntry,
    MessageLedger,
    latest_member,
    pick_weighted,
    summarize_chain,
)
from .modules.style_injector import ACTIVE_REPLY_STYLE, ANTI_ECHO_REWRITE_INSTRUCTION

PLUGIN_NAME = "astrbot_plugin_humanized_reply"
SUPPORTED_PLATFORM = "aiocqhttp"
DEFAULT_PROFILE = "default"

ACTIVE_REPLY_EXTRA = "_humanized_reply_active_reply"
POINTING_EXTRA = "_humanized_reply_pointing"
ECHO_SOURCES_EXTRA = "_humanized_reply_echo_sources"
REWRITE_COUNT_EXTRA = "_humanized_reply_rewrite_count"

DEFAULT_CONFIG: dict[str, Any] = {
    "enable": True,
    "mention": {"passive_probability": 0.10, "active_probability": 0.08},
    "quote": {
        "passive_probability": 0.03,
        "active_probability": 0.05,
        "max_age_seconds": 600,
    },
    "pointing_protocol": {"enable": True, "max_recent_messages": 30},
    "active_reply_style": {"enable": True},
    "anti_echo": {
        "mode": "observe",
        "similarity_threshold": 0.6,
        "rewrite_rate_limit_per_session": 2,
    },
    "ledger": {
        "persist": True,
        "max_messages_per_group": 200,
        "flush_interval_seconds": 30,
    },
    "scope": {"groups": [], "platforms": [SUPPORTED_PLATFORM]},
    "apply_to_non_llm": False,
}


def merge_config(defaults: dict[str, Any], overrides: Any) -> dict[str, Any]:
    """把插件配置合并到默认值上，缺失项与非法结构回退到默认值。"""
    merged: dict[str, Any] = {}
    for key, value in defaults.items():
        override = overrides.get(key) if isinstance(overrides, dict) else None
        if isinstance(value, dict):
            merged[key] = merge_config(
                value,
                override if isinstance(override, dict) else {},
            )
        elif override is None:
            merged[key] = value
        else:
            merged[key] = override
    return merged


def as_probability(value: Any, default: float) -> float:
    """把配置值收敛到 0-1 的概率区间。"""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return min(max(parsed, 0.0), 1.0)


def as_positive_int(value: Any, default: int, minimum: int = 1) -> int:
    """把配置值收敛为正整数。"""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= minimum else default


def is_group_message(event: AstrMessageEvent) -> bool:
    try:
        return event.get_message_type() == MessageType.GROUP_MESSAGE
    except Exception:
        return False


def is_active_group_reply(event: AstrMessageEvent) -> bool:
    """判断当前 LLM 请求是否来自概率触发的群聊主动回复。

    判定口径与 ``astrbot_plugin_group_reply_guard`` 一致：群消息、非 @/唤醒，
    且没有命中带参数的指令 handler。
    """
    if not is_group_message(event):
        return False
    if bool(getattr(event, "is_at_or_wake_command", False)):
        return False
    return not bool(event.get_extra("handlers_parsed_params", {}))


class HumanizedReply(Star):
    """群聊回复的拟人化表达。"""

    def __init__(self, context: Context, config: dict[str, Any] | None = None) -> None:
        super().__init__(context)
        self.config = merge_config(DEFAULT_CONFIG, config or {})
        self.enabled = bool(self.config.get("enable", True))

        scope = self.config.get("scope", {})
        self.scope_groups = [str(item) for item in scope.get("groups") or []]
        self.scope_platforms = [str(item) for item in scope.get("platforms") or []]

        self.mention_passive = as_probability(
            self.config["mention"].get("passive_probability"),
            DEFAULT_CONFIG["mention"]["passive_probability"],
        )
        self.mention_active = as_probability(
            self.config["mention"].get("active_probability"),
            DEFAULT_CONFIG["mention"]["active_probability"],
        )
        self.quote_passive = as_probability(
            self.config["quote"].get("passive_probability"),
            DEFAULT_CONFIG["quote"]["passive_probability"],
        )
        self.quote_active = as_probability(
            self.config["quote"].get("active_probability"),
            DEFAULT_CONFIG["quote"]["active_probability"],
        )
        self.quote_max_age = max(
            0.0,
            float(self.config["quote"].get("max_age_seconds") or 0),
        )

        self.pointing_enabled = bool(
            self.config.get("pointing_protocol", {}).get("enable", True),
        )
        self.max_recent_messages = as_positive_int(
            self.config.get("pointing_protocol", {}).get("max_recent_messages"),
            DEFAULT_CONFIG["pointing_protocol"]["max_recent_messages"],
        )
        self.style_enabled = bool(
            self.config.get("active_reply_style", {}).get("enable", True),
        )

        anti_echo_config = self.config.get("anti_echo", {})
        mode = str(anti_echo_config.get("mode") or "observe").lower()
        self.anti_echo_mode = mode if mode in {"observe", "strip", "rewrite"} else "observe"
        try:
            self.anti_echo_threshold = float(
                anti_echo_config.get("similarity_threshold") or 0,
            )
        except (TypeError, ValueError):
            self.anti_echo_threshold = anti_echo.DEFAULT_THRESHOLD
        self.rewrite_limit = as_positive_int(
            anti_echo_config.get("rewrite_rate_limit_per_session"),
            DEFAULT_CONFIG["anti_echo"]["rewrite_rate_limit_per_session"],
            minimum=0,
        )
        self.apply_to_non_llm = bool(self.config.get("apply_to_non_llm", False))

        self.data_dir = Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME
        ledger_config = self.config.get("ledger", {})
        self.ledger = MessageLedger(
            data_path=self.data_dir / "ledger.db",
            max_messages_per_group=as_positive_int(
                ledger_config.get("max_messages_per_group"),
                DEFAULT_CONFIG["ledger"]["max_messages_per_group"],
            ),
            persist=bool(ledger_config.get("persist", True)),
            flush_interval_seconds=float(
                ledger_config.get("flush_interval_seconds")
                or DEFAULT_CONFIG["ledger"]["flush_interval_seconds"],
            ),
        )
        self._flush_task: asyncio.Task | None = None
        self._rng = random.Random()

    # ------------------------------------------------------------- 生命周期

    async def initialize(self) -> None:
        """接管官方 @/引用开关并载入台账。

        启动场景下本方法在 ``ResultDecorateStage`` 创建之前执行，配置改动立即
        生效；运行时重载插件不会重建 scheduler，需要保存一次 WebUI 配置或重启。
        """
        if not self.enabled:
            logger.info("%s：插件已禁用，跳过官方开关接管", PLUGIN_NAME)
            return

        state_path = self.data_dir / config_takeover.STATE_FILENAME
        confs = getattr(self.context, "astrbot_config_mgr", None)
        confs = getattr(confs, "confs", None)
        if isinstance(confs, dict) and confs:
            result = config_takeover.takeover(
                confs,
                state_path,
                profile_ids=[DEFAULT_PROFILE],
            )
            if result.applied:
                logger.info(
                    "%s：已接管官方 @/引用开关（profile=%s，首次建立备份=%s）",
                    PLUGIN_NAME,
                    ",".join(result.profiles),
                    result.backup_created,
                )
            else:
                logger.warning(
                    "%s：未找到 default 配置，官方 @/引用开关接管未执行",
                    PLUGIN_NAME,
                )
        else:
            logger.warning("%s：无法访问配置管理器，跳过官方开关接管", PLUGIN_NAME)

        if not self.ledger.load():
            logger.warning("%s：群消息台账载入失败，将从空台账开始", PLUGIN_NAME)
        self._flush_task = asyncio.create_task(self._flush_loop())

    async def terminate(self) -> None:
        """恢复官方开关并落盘台账。"""
        if self._flush_task is not None:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
            self._flush_task = None

        self.ledger.close()

        state_path = self.data_dir / config_takeover.STATE_FILENAME
        confs = getattr(self.context, "astrbot_config_mgr", None)
        confs = getattr(confs, "confs", None)
        if isinstance(confs, dict) and confs:
            result = config_takeover.restore(
                confs,
                state_path,
                profile_ids=[DEFAULT_PROFILE],
            )
            if result.restored:
                logger.info(
                    "%s：已恢复官方 @/引用开关原值（profile=%s）。"
                    "当前进程的结果装饰阶段仍使用旧配置，"
                    "请在 WebUI 保存一次配置或重启 AstrBot 使其生效",
                    PLUGIN_NAME,
                    ",".join(result.profiles),
                )

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self.ledger.flush_interval_seconds)
            try:
                self.ledger.flush()
            except Exception:
                logger.exception("%s：群消息台账落盘异常", PLUGIN_NAME)

    # ------------------------------------------------------------- 作用域

    def in_scope(self, event: AstrMessageEvent) -> bool:
        """判断事件是否在插件生效范围内。"""
        if not self.enabled:
            return False
        try:
            platform = event.get_platform_name()
        except Exception:
            return False
        if self.scope_platforms and platform not in self.scope_platforms:
            return False
        if self.scope_groups:
            try:
                group_id = str(event.get_group_id() or "")
            except Exception:
                return False
            if group_id not in self.scope_groups:
                return False
        return True

    # ------------------------------------------------------------- 群消息台账

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def record_group_message(self, event: AstrMessageEvent) -> None:
        """记录群消息，为指向提供 message_id 与发送者信息。"""
        if not self.in_scope(event):
            return
        group_id = str(event.get_group_id() or "")
        message_id = str(getattr(event.message_obj, "message_id", "") or "")
        if not group_id or not message_id:
            return
        if str(event.get_sender_id()) == str(event.get_self_id()):
            return

        timestamp = getattr(event.message_obj, "timestamp", None)
        self.ledger.record(
            group_id=group_id,
            message_id=message_id,
            sender_id=str(event.get_sender_id()),
            nickname=event.get_sender_name() or "",
            summary=summarize_chain(event.get_messages()),
            timestamp=float(timestamp) if timestamp else None,
        )

    @filter.after_message_sent()
    async def flush_ledger_after_send(self, event: AstrMessageEvent) -> None:
        """机器人回复后落盘一次台账，避免进程异常退出丢失记录。"""
        if not self.in_scope(event):
            return
        self.ledger.flush(force=True)

    # ------------------------------------------------------------- 请求注入

    @filter.on_llm_request(priority=-200)
    async def inject_pointing_context(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> None:
        """注入指向参考表与指向协议，并记录本轮可见消息。"""
        if not self.in_scope(event) or not is_group_message(event):
            return
        group_id = str(event.get_group_id() or "")
        if not group_id:
            return

        visible = self.ledger.visible_window(group_id, limit=self.max_recent_messages)
        event.set_extra(POINTING_EXTRA, {entry.seq: entry for entry in visible})
        # 核心把窗口内的消息注入本轮上下文后即清空，本插件的可见范围同步推进，
        # 保证参考表与模型真正看到的消息范围一致。
        if visible:
            self.ledger.advance_visible_anchor(group_id, visible[-1].seq)

        if self.pointing_enabled and visible:
            table = pointing_protocol.render_reference_table(visible)
            req.extra_user_content_parts.append(
                TextPart(
                    text=(
                        f"{pointing_protocol.POINTING_INSTRUCTIONS}\n"
                        f"<message_index>\n{table}\n</message_index>"
                    ),
                ).mark_as_temp(),
            )

        trigger_id = str(getattr(event.message_obj, "message_id", "") or "")
        sources = [event.message_str]
        sources.extend(
            entry.summary
            for entry in visible
            if entry.message_id != trigger_id and entry.summary
        )
        event.set_extra(ECHO_SOURCES_EXTRA, [text for text in sources if text])

    @filter.on_llm_request(priority=-100)
    async def inject_active_reply_style(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> None:
        """为概率主动回复注入表达风格指令。"""
        if not self.in_scope(event) or not self.style_enabled:
            return
        if not is_active_group_reply(event):
            return
        if any(
            isinstance(part, TextPart) and part.text == ACTIVE_REPLY_STYLE
            for part in req.extra_user_content_parts
        ):
            return
        req.extra_user_content_parts.append(
            TextPart(text=ACTIVE_REPLY_STYLE).mark_as_temp(),
        )
        event.set_extra(ACTIVE_REPLY_EXTRA, True)

    # ------------------------------------------------------------- 响应处理

    @filter.on_llm_response()
    async def handle_echo_reply(
        self,
        event: AstrMessageEvent,
        response: LLMResponse,
    ) -> None:
        """按配置模式检测并处理附和式复读。"""
        if not self.in_scope(event):
            return

        verdict = self.evaluate_echo(event, response)
        if verdict is None or not verdict.is_echo:
            return

        if self.anti_echo_mode == "rewrite":
            await self.rewrite_echo_reply(event, response, verdict)
            return

        if self.anti_echo_mode == "strip" and pointing_protocol.MARKER_PATTERN.search(
            response.completion_text or "",
        ):
            response.completion_text = pointing_protocol.strip_markers(
                response.completion_text,
            )
            logger.info(
                "%s：检测到附和式复读，已剥离指向标记后按普通消息发送"
                "（相似度=%.3f，原因=%s）",
                PLUGIN_NAME,
                verdict.score,
                verdict.reason,
            )
            return

        logger.info(
            "%s：检测到疑似附和式复读（相似度=%.3f，原因=%s），"
            "当前为 %s 模式，不做处理",
            PLUGIN_NAME,
            verdict.score,
            verdict.reason,
            self.anti_echo_mode,
        )

    def evaluate_echo(
        self,
        event: AstrMessageEvent,
        response: LLMResponse,
    ) -> anti_echo.EchoVerdict | None:
        """对群回复执行附和检测，非群回复或无候选来源时返回 None。"""
        if not is_group_message(event):
            return None
        if not event.get_extra(ACTIVE_REPLY_EXTRA, False) and not event.get_extra(
            POINTING_EXTRA,
        ):
            return None
        text = response.completion_text or ""
        if not text.strip():
            return None
        sources = [
            str(source)
            for source in event.get_extra(ECHO_SOURCES_EXTRA, []) or []
            if str(source).strip()
        ]
        if not sources:
            return None
        return anti_echo.evaluate(
            pointing_protocol.strip_markers(text),
            sources,
            threshold=self.anti_echo_threshold,
        )

    async def rewrite_echo_reply(
        self,
        event: AstrMessageEvent,
        response: LLMResponse,
        verdict: anti_echo.EchoVerdict,
    ) -> None:
        """用一次独立生成替换附和回复，失败时保留原回复。"""
        used = int(event.get_extra(REWRITE_COUNT_EXTRA, 0) or 0)
        if self.rewrite_limit <= 0 or used >= self.rewrite_limit:
            logger.info(
                "%s：本轮重写次数已达上限，按原回复发送（相似度=%.3f）",
                PLUGIN_NAME,
                verdict.score,
            )
            return

        try:
            provider_id = await self.context.get_current_chat_provider_id(
                umo=event.unified_msg_origin,
            )
        except Exception:
            logger.warning("%s：未找到可用模型，无法重写附和回复", PLUGIN_NAME)
            return

        original = pointing_protocol.strip_markers(response.completion_text or "")
        try:
            rewritten = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=f"对方的消息：{verdict.source}\n你的回复：{original}",
                system_prompt=ANTI_ECHO_REWRITE_INSTRUCTION,
            )
        except Exception:
            logger.exception("%s：重写附和回复失败，按原回复发送", PLUGIN_NAME)
            return

        new_text = (rewritten.completion_text or "").strip()
        if not new_text:
            logger.warning("%s：重写结果为空，按原回复发送", PLUGIN_NAME)
            return

        event.set_extra(REWRITE_COUNT_EXTRA, used + 1)
        response.completion_text = new_text
        logger.info(
            "%s：检测到附和式复读并完成重写（相似度=%.3f，原因=%s）",
            PLUGIN_NAME,
            verdict.score,
            verdict.reason,
        )

    # ------------------------------------------------------------- 发送前装饰

    @filter.on_decorating_result(priority=-100)
    async def decorate_group_reply(self, event: AstrMessageEvent) -> None:
        """按模型标记或概率兜底为回复插入 @/引用组件。"""
        if not self.in_scope(event):
            return
        try:
            if event.is_private_chat():
                return
        except Exception:
            return

        result = event.get_result()
        if result is None or not result.chain:
            return
        if not self.apply_to_non_llm and not result.is_model_result():
            return
        if not decorator.can_decorate(result.chain):
            return
        if decorator.head_has_pointing(result.chain):
            logger.warning(
                "%s：消息链中已存在 At/Reply 组件（可能是用户手动开启了官方 "
                "@引用开关），跳过本插件的指向装饰",
                PLUGIN_NAME,
            )
            return

        group_id = str(event.get_group_id() or "")
        visible_map = event.get_extra(POINTING_EXTRA, {})
        visible: list[LedgerEntry] = (
            [entry for entry in visible_map.values()]
            if isinstance(visible_map, dict)
            else []
        )
        is_active = bool(event.get_extra(ACTIVE_REPLY_EXTRA, False)) or (
            is_active_group_reply(event)
        )

        decision = decorator.PointingDecision()
        text = result.get_plain_text()

        if self.pointing_enabled and visible:
            plan = pointing_protocol.resolve_plan(
                text,
                visible=visible,
                self_id=str(event.get_self_id()),
            )
            self.replace_plain_text(result.chain, plan.text)
            if plan.has_target:
                decision.quote_target = plan.quote_target
                decision.at_target = plan.at_target
                decision.reason = "模型标记"
            elif plan.dropped_reason:
                logger.info(
                    "%s：丢弃模型指向标记（%s）",
                    PLUGIN_NAME,
                    plan.dropped_reason,
                )
        else:
            cleaned = pointing_protocol.strip_markers(text)
            if cleaned != text:
                self.replace_plain_text(result.chain, cleaned)

        if not decision.has_pointing:
            self.apply_fallback_pointing(
                event,
                decision,
                group_id=group_id,
                visible=visible,
                is_active=is_active,
            )

        if decision.has_pointing:
            decorator.apply_pointing(result.chain, decision)
            logger.info(
                "%s：已为群 %s 的回复添加指向（%s）",
                PLUGIN_NAME,
                group_id,
                decision.reason,
            )

    def apply_fallback_pointing(
        self,
        event: AstrMessageEvent,
        decision: decorator.PointingDecision,
        *,
        group_id: str,
        visible: list[LedgerEntry],
        is_active: bool,
    ) -> None:
        """模型没有给出标记时，按场景概率决定是否 @ 或引用。"""
        use_mention, use_quote = decorator.decide_fallback(
            is_active=is_active,
            mention_probability=(
                self.mention_active if is_active else self.mention_passive
            ),
            quote_probability=self.quote_active if is_active else self.quote_passive,
            rng=self._rng,
        )

        if use_mention:
            target = self.mention_target(event, group_id, is_active, visible)
            if target is not None:
                decision.at_target = target
                decision.reason = "概率兜底 @"

        if use_quote:
            target = self.quote_target(event, group_id, is_active, visible)
            if target is not None:
                decision.quote_target = target
                decision.reason = (
                    f"{decision.reason}、概率兜底引用"
                    if decision.reason
                    else "概率兜底引用"
                )

    def mention_target(
        self,
        event: AstrMessageEvent,
        group_id: str,
        is_active: bool,
        visible: list[LedgerEntry],
    ) -> LedgerEntry | None:
        """@ 目标：被动场景指向触发消息发送者，主动场景指向最近活跃成员。"""
        self_id = str(event.get_self_id())
        if not is_active:
            sender_id = str(event.get_sender_id())
            if not sender_id or sender_id == self_id:
                return None
            return next(
                (
                    entry
                    for entry in reversed(visible)
                    if str(entry.sender_id) == sender_id
                ),
                None,
            )
        return latest_member(visible, exclude=self_id)

    def quote_target(
        self,
        event: AstrMessageEvent,
        group_id: str,
        is_active: bool,
        visible: list[LedgerEntry],
    ) -> LedgerEntry | None:
        """引用目标：被动场景引用触发消息，主动场景在时效范围内加权抽取。"""
        trigger_id = str(getattr(event.message_obj, "message_id", "") or "")
        if not is_active:
            return next(
                (entry for entry in reversed(visible) if entry.message_id == trigger_id),
                None,
            )
        return pick_weighted(
            visible,
            max_age_seconds=self.quote_max_age,
            exclude_message_id=trigger_id,
        )

    @staticmethod
    def replace_plain_text(chain: list, text: str) -> None:
        """把剥离标记后的正文写回消息链。

        模型通常在回复开头输出标记，因此只有一个 Plain 段时直接替换；存在多个
        Plain 段时逐段剥离标记，保留组件顺序。
        """
        plains = [comp for comp in chain if isinstance(comp, Plain)]
        if not plains:
            return
        if len(plains) == 1:
            plains[0].text = text
            return
        for comp in plains:
            comp.text = pointing_protocol.strip_markers(comp.text)
