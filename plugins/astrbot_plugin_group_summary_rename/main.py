"""群聊总结改名插件。

定时（或手动）总结指定群的聊天记录，使用 AstrBot 配置中的 LLM 生成
新群名，并通过 OneBot API 自动修改 QQ 群名称。
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Plain
from astrbot.api.platform import MessageType
from astrbot.api.star import Context, Star

PLUGIN_NAME = "astrbot_plugin_group_summary_rename"
DEFAULT_TRIGGER_TIME = "20:00"
DEFAULT_HISTORY_COUNT = 50
DEFAULT_NAME_MAX_LEN = 30
DEFAULT_CHECK_INTERVAL = 60

# 群名不允许包含的字符（QQ 群名限制）
_INVALID_NAME_CHARS_RE = re.compile(r"[\r\n\t]")

# 用于解析 LLM 输出中的 JSON 对象
_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def build_messages_text(messages: list[dict]) -> str:
    """将 OneBot 群历史消息转换为可读文本。

    每条消息格式：`[HH:MM] 昵称: 内容`，图片标注为 [图片]。
    """
    lines: list[str] = []
    for msg in messages:
        nickname = msg.get("nickname") or str(msg.get("user_id", "未知"))
        timestamp = int(msg.get("time", 0) or 0)
        time_str = (
            datetime.fromtimestamp(timestamp).strftime("%H:%M") if timestamp else ""
        )
        content = extract_message_text(msg.get("message", []))
        # 跳过没有实际文本内容的消息（如纯图片/表情/语音）
        text_only = content
        for tag in ("[图片]", "[表情]", "[语音]", "[视频]", "[@]"):
            text_only = text_only.replace(tag, "").strip()
        if not text_only:
            continue
        lines.append(f"[{time_str}] {nickname}: {content}")
    return "\n".join(lines)


def extract_message_text(message: Any) -> str:
    """从 OneBot 消息段中提取纯文本。

    Args:
        message: OneBot 消息段列表，或纯文本字符串。

    Returns:
        拼接后的文本；图片/表情等非文本段标注为 [图片] 等。
    """
    if isinstance(message, str):
        return message.strip()
    if not isinstance(message, list):
        return ""
    parts: list[str] = []
    for seg in message:
        if not isinstance(seg, dict):
            continue
        seg_type = seg.get("type", "")
        data = seg.get("data", {}) or {}
        if seg_type == "text":
            text = (data.get("text") or "").strip()
            if text:
                parts.append(text)
        elif seg_type == "image":
            parts.append("[图片]")
        elif seg_type == "face":
            parts.append("[表情]")
        elif seg_type == "at":
            qq = data.get("qq")
            parts.append(f"@{qq}" if qq else "[@]")
        elif seg_type == "record":
            parts.append("[语音]")
        elif seg_type == "video":
            parts.append("[视频]")
    return " ".join(parts).strip()


def parse_llm_output(text: str) -> dict:
    """解析 LLM 输出，期望为 JSON 对象：{"summary": "...", "name": "..."}。

    容错：从文本中提取首个 JSON 对象；若字段缺失则返回空字符串。
    """
    cleaned = text.strip()
    # 去掉可能的 markdown 代码块标记
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned).strip()
    match = _JSON_BLOCK_RE.search(cleaned)
    if not match:
        raise ValueError(f"LLM 输出中未找到 JSON 对象: {text[:200]}")
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as e:
        raise ValueError(f"LLM 输出 JSON 解析失败: {e}") from e
    if not isinstance(data, dict):
        raise ValueError("LLM 输出 JSON 不是对象")
    return {
        "summary": str(data.get("summary", "") or "").strip(),
        "name": str(data.get("name", "") or "").strip(),
    }


def sanitize_group_name(name: str, max_len: int) -> str:
    """清洗 LLM 生成的群名：去首尾空白/引号、非法字符，限制长度。"""
    name = name.strip().strip("\"'“”‘’`")
    name = _INVALID_NAME_CHARS_RE.sub(" ", name).strip()
    name = re.sub(r"\s+", " ", name)
    if len(name) > max_len:
        name = name[:max_len].rstrip()
    return name


def parse_trigger_time(value: str) -> str:
    """校验并规范化触发时间，格式 HH:MM（24 小时制）。"""
    value = value.strip()
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError(f"无效的触发时间: {value!r}，应为 HH:MM（如 20:00）")
    return value


def is_trigger_time(now: datetime, trigger_time: str) -> bool:
    """判断当前时间是否命中触发时间（精确到分钟）。"""
    try:
        hour, minute = trigger_time.split(":")
        return now.hour == int(hour) and now.minute == int(minute)
    except (ValueError, TypeError):
        return False


class GroupSummaryRename(Star):
    """群聊总结改名插件。"""

    def __init__(self, context: Context, config: dict[str, Any] | None = None):
        super().__init__(context, config)
        self.config = config or {}
        self._scheduler_task: asyncio.Task | None = None
        self._running_groups: set[str] = set()

    # ---------------------------------------------------------------- 指令

    @filter.command_group("群名总结")
    def group_summary(self):
        """群名总结指令组。"""

    @group_summary.command("开启")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def cmd_enable(self, event: AstrMessageEvent):
        """开启本群的自动总结改名功能。"""
        if self._is_bot_self(event):
            event.stop_event()
            return
        if not await self._is_privileged(event):
            yield event.plain_result("仅群主/管理员或机器人管理员可执行此操作。")
            event.stop_event()
            return
        group_id = event.get_group_id()
        state = await self._get_group_state(group_id)
        state["enabled"] = True
        state.setdefault(
            "trigger_time", str(self.config.get("trigger_time", DEFAULT_TRIGGER_TIME))
        )
        state.setdefault("platform_id", event.get_platform_id())
        state.setdefault("self_id", event.get_self_id())
        await self._save_group_state(group_id, state)
        yield event.plain_result(
            f"✅ 已开启本群自动总结改名，触发时间：{state['trigger_time']}。\n"
            "使用「群名总结 时间 HH:MM」修改触发时间，或「群名总结 立即」手动执行一次。"
        )
        event.stop_event()

    @group_summary.command("关闭")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def cmd_disable(self, event: AstrMessageEvent):
        """关闭本群的自动总结改名功能。"""
        if self._is_bot_self(event):
            event.stop_event()
            return
        if not await self._is_privileged(event):
            yield event.plain_result("仅群主/管理员或机器人管理员可执行此操作。")
            event.stop_event()
            return
        group_id = event.get_group_id()
        state = await self._get_group_state(group_id)
        state["enabled"] = False
        await self._save_group_state(group_id, state)
        yield event.plain_result("✅ 已关闭本群自动总结改名功能。")
        event.stop_event()

    @group_summary.command("时间")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def cmd_set_time(self, event: AstrMessageEvent, time_str: str):
        """设置本群固定触发时间，格式 HH:MM。"""
        if self._is_bot_self(event):
            event.stop_event()
            return
        if not await self._is_privileged(event):
            yield event.plain_result("仅群主/管理员或机器人管理员可执行此操作。")
            event.stop_event()
            return
        try:
            trigger_time = parse_trigger_time(time_str)
        except ValueError as e:
            yield event.plain_result(f"❌ {e}")
            event.stop_event()
            return
        group_id = event.get_group_id()
        state = await self._get_group_state(group_id)
        state["trigger_time"] = trigger_time
        state.setdefault("platform_id", event.get_platform_id())
        state.setdefault("self_id", event.get_self_id())
        await self._save_group_state(group_id, state)
        hint = "（功能未开启，仅保存了时间）" if not state.get("enabled") else ""
        yield event.plain_result(f"✅ 已设置本群触发时间为 {trigger_time} {hint}")
        event.stop_event()

    @group_summary.command("立即")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def cmd_run_now(self, event: AstrMessageEvent):
        """立即总结一次群聊并修改群名。"""
        if self._is_bot_self(event):
            event.stop_event()
            return
        if not await self._is_privileged(event):
            yield event.plain_result("仅群主/管理员或机器人管理员可执行此操作。")
            event.stop_event()
            return
        group_id = event.get_group_id()
        state = await self._get_group_state(group_id)
        state.setdefault("platform_id", event.get_platform_id())
        state.setdefault("self_id", event.get_self_id())
        await self._save_group_state(group_id, state)
        yield event.plain_result("⏳ 正在总结群聊并生成新群名，请稍候…")
        try:
            # 手动执行不更新 last_run_date，避免影响当天定时触发
            await self._run_summary_for_group(
                event.get_platform_id(), group_id, announce=False, update_last_run=False
            )
        except Exception as e:
            logger.exception(f"[{PLUGIN_NAME}] 手动总结失败: {e}")
            yield event.plain_result(f"❌ 总结失败：{e}")
            event.stop_event()
            return
        yield event.plain_result("✅ 总结完成，群名已更新。")
        event.stop_event()

    @group_summary.command("状态")
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def cmd_status(self, event: AstrMessageEvent):
        """查看本群总结改名功能状态。"""
        if self._is_bot_self(event):
            event.stop_event()
            return
        group_id = event.get_group_id()
        state = await self._get_group_state(group_id)
        enabled = "✅ 已开启" if state.get("enabled") else "❌ 未开启"
        lines = [
            f"本群自动总结改名：{enabled}",
            f"触发时间：{state.get('trigger_time', '未设置')}",
        ]
        if state.get("last_name"):
            lines.append(f"最近一次群名：{state['last_name']}")
        if state.get("last_run_date"):
            lines.append(f"最近一次执行日期：{state['last_run_date']}")
        yield event.plain_result("\n".join(lines))
        event.stop_event()

    # ------------------------------------------------------------ 定时任务

    @filter.on_plugin_loaded()
    async def on_plugin_loaded(self, metadata):
        """插件加载完成时启动定时检查任务。

        使用 on_plugin_loaded 而非 on_astrbot_loaded：
        插件热重载（WebUI 重载）时不会触发 on_astrbot_loaded，
        但会触发 on_plugin_loaded，确保重载后定时任务仍会启动。
        """
        self._ensure_scheduler()

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self):
        """AstrBot 加载完成后启动定时检查任务（兜底）。"""
        self._ensure_scheduler()

    def _ensure_scheduler(self) -> None:
        """确保定时检查任务正在运行。"""
        if self._scheduler_task is None or self._scheduler_task.done():
            self._scheduler_task = asyncio.create_task(self._scheduler_loop())
            logger.info(f"[{PLUGIN_NAME}] 定时检查任务已启动")

    @filter.on_plugin_unloaded()
    async def on_plugin_unloaded(self, metadata):
        """插件卸载/重载时取消后台定时任务。"""
        if self._scheduler_task and not self._scheduler_task.done():
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass
        self._scheduler_task = None

    async def _scheduler_loop(self) -> None:
        """周期检查所有已开启功能的群，命中触发时间则执行总结改名。"""
        interval = max(
            10, int(self.config.get("check_interval", DEFAULT_CHECK_INTERVAL))
        )
        while True:
            try:
                await self._check_scheduled_groups()
            except Exception as e:
                logger.exception(f"[{PLUGIN_NAME}] 定时检查异常: {e}")
            await asyncio.sleep(interval)

    async def _check_scheduled_groups(self) -> None:
        """遍历全部群状态，命中触发时间且当天未执行过的群执行总结。"""
        states = await self.get_kv_data("group_states", {}) or {}
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        pending: list[tuple[str, str, str]] = []
        for group_id, state in states.items():
            try:
                if not state.get("enabled"):
                    continue
                trigger_time = state.get("trigger_time") or str(
                    self.config.get("trigger_time", DEFAULT_TRIGGER_TIME)
                )
                if not is_trigger_time(now, trigger_time):
                    continue
                if state.get("last_run_date") == today:
                    continue
                platform_id = state.get("platform_id")
                if not platform_id:
                    logger.warning(
                        f"[{PLUGIN_NAME}] 群 {group_id} 缺少 platform_id，跳过"
                    )
                    continue
                pending.append((platform_id, group_id, trigger_time))
            except Exception as e:
                logger.exception(f"[{PLUGIN_NAME}] 群 {group_id} 定时检查失败: {e}")
        for platform_id, group_id, trigger_time in pending:
            try:
                logger.info(
                    f"[{PLUGIN_NAME}] 定时触发：群 {group_id}，时间 {trigger_time}"
                )
                await self._run_summary_for_group(platform_id, group_id, announce=True)
            except Exception as e:
                logger.exception(f"[{PLUGIN_NAME}] 群 {group_id} 定时执行失败: {e}")

    # ------------------------------------------------------------ 核心逻辑

    async def _run_summary_for_group(
        self,
        platform_id: str,
        group_id: str,
        announce: bool,
        update_last_run: bool = True,
    ) -> None:
        """执行一次完整的总结改名流程。

        1. 拉取群最近消息；
        2. 调用 AstrBot 配置的 LLM 生成总结与新群名；
        3. 通过 OneBot API 修改群名；
        4. 更新状态存储，可选发送通知。
        """
        if group_id in self._running_groups:
            logger.info(f"[{PLUGIN_NAME}] 群 {group_id} 正在执行中，跳过")
            return
        self._running_groups.add(group_id)
        try:
            platform = self.context.get_platform_inst(platform_id)
            if platform is None:
                raise RuntimeError(f"未找到平台实例: {platform_id}")
            bot = getattr(platform, "bot", None)
            if bot is None:
                raise RuntimeError(f"平台 {platform_id} 无 bot 实例")

            state = await self._get_group_state(group_id)
            self_id = state.get("self_id") or self._get_bot_self_id(bot)
            # 拉取群历史消息
            history_count = int(self.config.get("history_count", DEFAULT_HISTORY_COUNT))
            messages = await self._fetch_group_messages(
                bot, group_id, history_count, self_id
            )
            if not messages:
                raise RuntimeError("未能获取到群消息")

            # 调用 LLM 生成总结与群名
            summary, new_name = await self._generate_summary_and_name(
                platform_id, group_id, messages
            )
            if not new_name:
                raise RuntimeError("LLM 未生成有效群名")
            max_len = int(self.config.get("name_max_len", DEFAULT_NAME_MAX_LEN))
            new_name = sanitize_group_name(new_name, max_len)
            if not new_name:
                raise RuntimeError("清洗后的群名为空")

            # 修改群名
            await self._set_group_name(bot, group_id, new_name, self_id)
            today = datetime.now().strftime("%Y-%m-%d")
            state["last_name"] = new_name
            state["last_summary"] = summary
            if update_last_run:
                state["last_run_date"] = today
            await self._save_group_state(group_id, state)
            logger.info(f"[{PLUGIN_NAME}] 群 {group_id} 群名已修改为: {new_name}")

            if announce:
                await self._notify_group(platform_id, group_id, summary, new_name)
        finally:
            self._running_groups.discard(group_id)

    def _get_bot_self_id(self, bot) -> str:
        """从平台 bot 上尝试获取机器人自身 ID。"""
        for attr in ("self_id", "bot_id"):
            value = getattr(bot, attr, None)
            if value:
                return str(value)
        return ""

    async def _fetch_group_messages(
        self, bot, group_id: str, count: int, self_id: str
    ) -> list[dict]:
        """调用 OneBot API 拉取群最近消息。"""
        params: dict[str, Any] = {
            "group_id": int(group_id),
            "count": max(1, min(count, 100)),
        }
        if self_id:
            params["self_id"] = int(self_id)
        resp = await self._call_onebot_api_with_retry(
            bot, "get_group_msg_history", **params
        )
        messages = (resp or {}).get("messages") or []
        return list(messages)

    async def _call_onebot_api_with_retry(
        self, bot, action: str, retries: int = 3, delay: float = 1.0, **params
    ):
        """调用 OneBot API，失败时重试。

        aiocqhttp 在 API 客户端（WS 连接）瞬时不可用时会抛 ApiNotAvailable，
        此时短暂等待后重试可自愈。
        """
        last_exc: Exception | None = None
        for attempt in range(retries):
            try:
                return await bot.call_action(action, **params)
            except Exception as e:  # noqa: BLE001 - 兼容 aiocqhttp 各类异常
                last_exc = e
                if attempt < retries - 1:
                    logger.warning(
                        f"[{PLUGIN_NAME}] OneBot API {action} 第 {attempt + 1} 次调用失败: "
                        f"{e}，{delay} 秒后重试"
                    )
                    await asyncio.sleep(delay)
        raise RuntimeError(f"OneBot API {action} 调用失败: {last_exc}") from last_exc

    async def _generate_summary_and_name(
        self, platform_id: str, group_id: str, messages: list[dict]
    ) -> tuple[str, str]:
        """调用 AstrBot 配置的 LLM 生成群聊总结与新群名。

        Returns:
            (summary, name)
        """
        umo = f"{platform_id}:{MessageType.GROUP_MESSAGE.value}:{group_id}"
        provider_id = await self.context.get_current_chat_provider_id(umo=umo)
        max_len = int(self.config.get("name_max_len", DEFAULT_NAME_MAX_LEN))
        rename_prompt = str(self.config.get("rename_prompt", "") or "").strip()

        system_prompt = (
            "你是一个群聊分析助手。请根据提供的群聊消息记录，完成两项任务：\n"
            "1. 总结该群最近讨论的主题与氛围，输出 50-150 字的群聊总结；\n"
            "2. 根据总结内容生成一个贴合群聊主题、简洁有趣的新群名。\n"
            f"群名要求：长度不超过 {max_len} 个字符，不含换行、引号、@ 等特殊符号，"
            "不使用无意义的纯符号。\n"
        )
        if rename_prompt:
            system_prompt += f"关于修改群名的补充要求：{rename_prompt}\n"
        system_prompt += (
            "请只输出一个 JSON 对象，格式为："
            '{"summary": "群聊总结", "name": "新群名"}，不要输出其他任何内容。'
        )

        history_text = build_messages_text(messages)
        prompt = f"以下是群聊消息记录：\n{history_text}"
        llm_resp = await self.context.llm_generate(
            chat_provider_id=provider_id,
            prompt=prompt,
            system_prompt=system_prompt,
        )
        result = parse_llm_output(llm_resp.completion_text)
        return result.get("summary", ""), result.get("name", "")

    async def _set_group_name(
        self, bot, group_id: str, new_name: str, self_id: str
    ) -> None:
        """调用 OneBot API 修改群名。"""
        params: dict[str, Any] = {
            "group_id": int(group_id),
            "group_name": new_name,
        }
        if self_id:
            params["self_id"] = int(self_id)
        await self._call_onebot_api_with_retry(bot, "set_group_name", **params)

    async def _notify_group(
        self, platform_id: str, group_id: str, summary: str, new_name: str
    ) -> None:
        """向群内发送总结与改名结果。"""
        umo = f"{platform_id}:{MessageType.GROUP_MESSAGE.value}:{group_id}"
        text = f"📝 群聊总结：\n{summary}\n\n🏷️ 群名已更新为：{new_name}"
        await self.context.send_message(umo, MessageChain([Plain(text)]))

    # ------------------------------------------------------------ 权限与存储

    def _is_bot_self(self, event: AstrMessageEvent) -> bool:
        """是否为机器人自己发送的消息（防止自触发循环）。"""
        self_id = event.get_self_id()
        sender_id = event.get_sender_id()
        return bool(self_id) and self_id == sender_id

    async def _is_privileged(self, event: AstrMessageEvent) -> bool:
        """是否为群主/群管理员或 AstrBot 配置的管理员。"""
        if event.is_admin():
            return True
        try:
            platform = self.context.get_platform_inst(event.get_platform_id())
            bot = getattr(platform, "bot", None)
            if bot is None:
                return False
            params: dict[str, Any] = {
                "group_id": int(event.get_group_id()),
                "user_id": int(event.get_sender_id()),
            }
            if event.get_self_id():
                params["self_id"] = int(event.get_self_id())
            info = await bot.call_action("get_group_member_info", **params)
            return (info or {}).get("role") in ("owner", "admin")
        except Exception as e:
            logger.warning(f"[{PLUGIN_NAME}] 权限检查失败: {e}")
            return False

    async def _get_group_state(self, group_id: str) -> dict:
        states = await self.get_kv_data("group_states", {}) or {}
        return dict(states.get(group_id) or {})

    async def _save_group_state(self, group_id: str, state: dict) -> None:
        states = await self.get_kv_data("group_states", {}) or {}
        states[group_id] = state
        await self.put_kv_data("group_states", states)
