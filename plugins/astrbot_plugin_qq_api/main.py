"""QQ 接口插件：把 OneBot v11 接口作为工具暴露给 AstrBot 的 LLM。

工具按对象分为群、机器人自身和其他用户三组，并通过插件配置逐项开关。
所有写入类操作默认关闭，且默认仅允许 AstrBot 管理员调用。
"""

from __future__ import annotations

import asyncio
import copy
import html
import json
import random
import time
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Poke, Reply
from astrbot.api.star import Context, Star
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext
from pydantic import Field
from pydantic.dataclasses import dataclass

PLUGIN_NAME = "astrbot_plugin_qq_api"
SUPPORTED_PLATFORM = "aiocqhttp"
MAX_TOOL_RESULT_CHARS = 24000

POKE_MODE_RANDOM = "random"
POKE_MODE_LLM = "llm"
POKE_DEFAULT_REPLY_TEXTS = [
    "在呢，怎么啦？",
    "别戳啦，有事直说。",
    "戳我做什么？",
    "我在这儿。",
]
POKE_DEFAULT_LLM_PROMPT = (
    "用户 {username}（ID：{user_id}）在{scene}里戳了你一下。"
    "请保持当前人设，结合最近的对话上下文，自然地回应这次戳一戳，"
    "只输出要发送给用户的内容。"
)
POKE_REPLY_EXTRA = "_poke_reply_triggered"
POKE_QUOTE_FILTER_EXTRA = "_poke_reply_quote_filter_installed"
POKE_COOLDOWN_CACHE_LIMIT = 500
POKE_COOLDOWN_RETENTION_SECONDS = 600

GROUP_READ_ACTIONS = (
    "list",
    "info",
    "members",
    "member",
    "announcements",
    "files",
    "honor",
    "messages",
    "essence",
    "system_messages",
    "shut_list",
    "at_all_remain",
)
GROUP_INFO_WRITE_ACTIONS = (
    "set_name",
    "set_portrait",
    "set_remark",
    "send_announcement",
    "delete_announcement",
)
GROUP_MEMBER_WRITE_ACTIONS = (
    "set_card",
    "set_special_title",
    "set_admin",
    "ban",
    "whole_ban",
    "kick",
)
GROUP_FILE_WRITE_ACTIONS = (
    "upload",
    "delete",
    "create_folder",
    "delete_folder",
    "move",
)
GROUP_MESSAGE_WRITE_ACTIONS = (
    "set_essence",
    "delete_essence",
    "recall",
    "reaction",
    "mark_read",
    "handle_request",
)
GROUP_LIFECYCLE_WRITE_ACTIONS = ("leave",)
SELF_READ_ACTIONS = ("account", "profile", "likes", "friends")
SELF_WRITE_ACTIONS = (
    "set_profile",
    "set_avatar",
    "set_online_status",
    "set_input_status",
    "set_long_nick",
)
USER_READ_ACTIONS = ("info", "private_messages", "likes", "requests")
USER_WRITE_ACTIONS = (
    "set_remark",
    "like",
    "poke",
    "delete_friend",
    "handle_request",
    "handle_doubt_request",
)

DEFAULT_ENABLED_ACTIONS: dict[str, set[str]] = {
    "group_read": set(GROUP_READ_ACTIONS),
    "group_info_write": set(),
    "group_member_write": set(),
    "group_file_write": set(),
    "group_message_write": set(),
    "group_lifecycle_write": set(),
    "self_read": set(SELF_READ_ACTIONS),
    "self_write": set(),
    "user_read": set(USER_READ_ACTIONS),
    "user_write": set(),
}

WRITE_TOOL_KEYS = {
    "group_info_write",
    "group_member_write",
    "group_file_write",
    "group_message_write",
    "group_lifecycle_write",
    "self_write",
    "user_write",
}

ONLINE_STATUS_VALUES = {
    "online": 10,
    "在线": 10,
    "qme": 60,
    "q我吧": 60,
    "away": 30,
    "离开": 30,
    "busy": 50,
    "忙碌": 50,
    "dnd": 70,
    "请勿打扰": 70,
    "invisible": 40,
    "隐身": 40,
}

_SEX_VALUES = {"unknown": 0, "未知": 0, "male": 1, "男": 1, "female": 2, "女": 2}


class QQApiError(Exception):
    """QQ 接口工具的可读错误。"""


def _format_time(value: Any) -> Any:
    """把 Unix 时间戳转为本地时间字符串；无法解析时原样返回。"""
    if value in (None, ""):
        return value
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return value
    if timestamp <= 0:
        return value
    try:
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, OverflowError, ValueError):
        return value


def _format_time_fields(data: Any, keys: tuple[str, ...]) -> Any:
    """格式化字典中的指定时间字段，不修改原始对象。"""
    if not isinstance(data, dict):
        return data
    result = dict(data)
    for key in keys:
        if key in result:
            result[key] = _format_time(result[key])
    return result


def _segment_text(segment: Any) -> str:
    """把单个 OneBot 消息段转换为可读文本。"""
    if not isinstance(segment, dict):
        return ""
    data = segment.get("data")
    if not isinstance(data, dict):
        data = {}
    segment_type = str(segment.get("type") or "")
    if segment_type == "text":
        return str(data.get("text") or "").strip()
    if segment_type == "at":
        target = str(data.get("qq") or "")
        return "@全体成员" if target == "all" else f"@{target}"
    placeholders = {
        "image": "[图片]",
        "face": "[表情]",
        "mface": "[表情]",
        "record": "[语音]",
        "video": "[视频]",
        "reply": "[回复]",
        "forward": "[转发消息]",
        "json": "[卡片消息]",
        "xml": "[卡片消息]",
        "poke": "[戳一戳]",
        "markdown": "[Markdown]",
        "file": "[文件]",
    }
    if segment_type in placeholders:
        if segment_type == "file" and data.get("name"):
            return f"[文件: {data['name']}]"
        return placeholders[segment_type]
    return f"[{segment_type}]" if segment_type else ""


def message_text(message: Any) -> str:
    """提取消息中的可读文本。"""
    if isinstance(message, str):
        return message.strip()
    if isinstance(message, dict):
        return _segment_text(message)
    if not isinstance(message, list):
        return ""
    parts = [_segment_text(segment) for segment in message]
    return " ".join(part for part in parts if part).strip()


def _bool_value(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on", "是", "开"}


def _as_non_negative_float(value: Any, default: float) -> float:
    """把配置值收敛为不小于 0 的浮点数。"""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return default if parsed < 0 else parsed


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
        try:
            if event.get_platform_name() != SUPPORTED_PLATFORM:
                return False
            if str(event.get_sender_id()) == str(event.get_self_id()):
                return False

            bot_id = str(event.get_self_id())
            for component in event.get_messages():
                if isinstance(component, Poke) and _poke_target(component) == bot_id:
                    return True
        except Exception:  # noqa: BLE001 - 异常时跳过本次事件，避免影响普通消息
            return False
        return False


def _int_value(
    value: Any,
    *,
    name: str,
    default: int | None = None,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if value in (None, ""):
        if default is None:
            raise QQApiError(f"缺少参数 {name}")
        result = default
    else:
        try:
            result = int(value)
        except (TypeError, ValueError) as exc:
            raise QQApiError(f"参数 {name} 必须是整数") from exc
    if minimum is not None:
        result = max(minimum, result)
    if maximum is not None:
        result = min(maximum, result)
    return result


def _validate_remote_file(value: Any, name: str) -> str:
    """仅允许远程地址或 base64 数据，避免模型读取服务器本地文件。"""
    text = str(value or "").strip()
    if not text:
        raise QQApiError(f"缺少参数 {name}")
    if text.startswith(("base64://", "data:")):
        return text
    parsed = urlparse(text)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return text
    raise QQApiError(f"{name} 只支持 http(s) 地址或 base64 数据，不接受本地路径")


def _json_result(data: Any, max_chars: int = MAX_TOOL_RESULT_CHARS) -> str:
    text = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    if len(text) <= max_chars:
        return text
    compact = json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)
    if len(compact) <= max_chars:
        return compact
    preview = compact[: max(0, max_chars - 200)]
    return json.dumps(
        {
            "truncated": True,
            "message": "结果超过返回上限，请使用 limit/offset 或更精确的查询减少返回内容。",
            "preview": preview,
        },
        ensure_ascii=False,
        indent=2,
    )


def _summarize_message(message: dict[str, Any]) -> dict[str, Any]:
    sender = message.get("sender")
    if not isinstance(sender, dict):
        sender = {}
    content = message.get("message")
    if not content:
        content = message.get("raw_message")
    return {
        "message_id": message.get("message_id") or message.get("real_id"),
        "message_seq": message.get("message_seq") or message.get("real_seq"),
        "time": _format_time(message.get("time")),
        "sender": {
            "user_id": sender.get("user_id"),
            "nickname": sender.get("nickname"),
            "card": sender.get("card"),
            "role": sender.get("role"),
        },
        "text": message_text(content),
    }


def _summarize_messages(
    messages: Any,
    max_chars: int,
) -> tuple[list[dict[str, Any]], int]:
    if not isinstance(messages, list):
        return [], 0
    result: list[dict[str, Any]] = []
    used = 0
    for message in messages:
        if not isinstance(message, dict):
            continue
        item = _summarize_message(message)
        text = item["text"]
        remaining = max_chars - used
        if remaining <= 0:
            break
        if len(text) > remaining:
            item["text"] = text[:remaining] + "…"
        result.append(item)
        used += len(item["text"])
    return result, len(messages)


def _summarize_member(member: Any) -> Any:
    if not isinstance(member, dict):
        return member
    keys = (
        "user_id",
        "nickname",
        "card",
        "sex",
        "age",
        "role",
        "level",
        "qq_level",
        "join_time",
        "last_sent_time",
        "shut_up_timestamp",
        "title",
        "title_expire_time",
    )
    return _format_time_fields(
        {key: member.get(key) for key in keys if key in member},
        ("join_time", "last_sent_time", "shut_up_timestamp", "title_expire_time"),
    )


def _summarize_group(group: Any) -> Any:
    if not isinstance(group, dict):
        return group
    keys = (
        "group_id",
        "group_name",
        "group_remark",
        "member_count",
        "max_member_count",
        "group_all_shut",
    )
    return {key: group.get(key) for key in keys if key in group}


def _summarize_file(file_info: Any) -> Any:
    if not isinstance(file_info, dict):
        return file_info
    keys = (
        "file_id",
        "file_name",
        "file_size",
        "busid",
        "upload_time",
        "dead_time",
        "modify_time",
        "download_times",
        "uploader",
        "uploader_name",
    )
    result = {key: file_info.get(key) for key in keys if key in file_info}
    if "file_size" not in result and "size" in file_info:
        result["file_size"] = file_info["size"]
    return _format_time_fields(result, ("upload_time", "dead_time", "modify_time"))


def _summarize_folder(folder: Any) -> Any:
    if not isinstance(folder, dict):
        return folder
    keys = (
        "folder_id",
        "folder_name",
        "create_time",
        "creator",
        "total_file_count",
    )
    return _format_time_fields(
        {key: folder.get(key) for key in keys if key in folder},
        ("create_time",),
    )


def _summarize_friend_request(request: Any) -> Any:
    if not isinstance(request, dict):
        return request
    keys = ("user_id", "nickname", "age", "sex", "reason", "flag")
    return {key: request.get(key) for key in keys if key in request}


def _summarize_friend(friend: Any) -> Any:
    if not isinstance(friend, dict):
        return friend
    keys = (
        "user_id",
        "nickname",
        "remark",
        "sex",
        "age",
        "level",
        "long_nick",
        "is_vip",
    )
    return {key: friend.get(key) for key in keys if key in friend}


def _summarize_profile(data: Any) -> Any:
    """精简用户资料，去掉内部编码、联系方式和超长字段。"""
    if not isinstance(data, dict):
        return data
    result = {
        "user_id": data.get("user_id") or data.get("uin"),
        "nickname": data.get("nickname") or data.get("nick"),
        "remark": data.get("remark"),
        "sex": data.get("sex"),
        "age": data.get("age"),
        "qid": data.get("qid"),
        "qq_level": data.get("qq_level") or data.get("qqLevel"),
        "signature": data.get("long_nick") or data.get("longNick"),
        "birthday_year": data.get("birthday_year"),
        "birthday_month": data.get("birthday_month"),
        "birthday_day": data.get("birthday_day"),
        "country": data.get("country"),
        "province": data.get("province"),
        "city": data.get("city"),
        "college": data.get("college"),
        "constellation": data.get("constellation"),
        "zodiac": data.get("shengXiao"),
        "reg_time": _format_time(data.get("reg_time") or data.get("regTime")),
        "is_vip": data.get("is_vip") or data.get("isvip"),
    }
    return {key: value for key, value in result.items() if value not in (None, "")}


def _summarize_likes(data: Any, limit: int) -> Any:
    """精简点赞资料，去掉内部 UID 和超长用户列表。"""
    if not isinstance(data, dict):
        return data
    result: dict[str, Any] = {}
    if "time" in data:
        result["time"] = _format_time(data["time"])
    for group_key in ("favoriteInfo", "voteInfo"):
        info = data.get(group_key)
        if not isinstance(info, dict):
            continue
        summary = {
            key: info.get(key)
            for key in (
                "total_count",
                "today_count",
                "new_count",
                "new_nearby_count",
                "last_time",
                "last_visit_time",
            )
            if key in info
        }
        for time_key in ("last_time", "last_visit_time"):
            if time_key in summary:
                summary[time_key] = _format_time(summary[time_key])
        users = info.get("userInfos")
        if isinstance(users, list):
            summary["users"] = [
                {
                    "user_id": user.get("uin"),
                    "nickname": user.get("nick"),
                    "count": user.get("count"),
                    "latest_time": _format_time(user.get("latestTime")),
                    "is_friend": user.get("isFriend"),
                    "gender": user.get("gender"),
                    "age": user.get("age"),
                }
                for user in users[:limit]
            ]
        result[group_key] = summary
    return result


def _summarize_announcement(item: Any) -> Any:
    if not isinstance(item, dict):
        return item
    message = item.get("message")
    if not isinstance(message, dict):
        message = {}
    images = message.get("images") or message.get("image") or []
    return {
        "notice_id": item.get("notice_id"),
        "sender_id": item.get("sender_id"),
        "publish_time": _format_time(item.get("publish_time")),
        "text": html.unescape(str(message.get("text") or "")).replace("\xa0", " "),
        "images": images,
        "read_num": item.get("read_num"),
    }


def _summarize_essence(item: Any) -> Any:
    if not isinstance(item, dict):
        return item
    result = _format_time_fields(item, ("sender_time", "operator_time"))
    result["text"] = message_text(result.pop("content", []))
    return result


def _paged(items: Any, limit: int, offset: int) -> tuple[list[Any], int]:
    if not isinstance(items, list):
        return [], 0
    return items[offset : offset + limit], len(items)


def _tool_parameters(
    action_enum: tuple[str, ...],
    properties: dict[str, Any],
) -> dict[str, Any]:
    params = {
        "action": {
            "type": "string",
            "enum": list(action_enum),
            "description": "要执行的具体操作，见工具说明。",
        }
    }
    params.update(properties)
    return {
        "type": "object",
        "properties": params,
        "required": ["action"],
    }


TOOL_SPECS: dict[str, dict[str, Any]] = {
    "group_read": {
        "name": "qq_group_read",
        "description": (
            "读取 QQ 群信息。用 action 选择内容：\n"
            "- list：机器人加入的群列表。\n"
            "- info：群资料，需要 group_id。\n"
            "- members：群成员列表，需要 group_id；可用 keyword 过滤昵称/群名片/QQ，limit、offset 分页。\n"
            "- member：单个群成员资料，需要 group_id 和 user_id。\n"
            "- announcements：群公告，需要 group_id。\n"
            "- files：群文件与文件夹，需要 group_id；folder_id 为空时读取根目录。\n"
            "- honor：群荣誉，需要 group_id；honor_type 可选 all/talkative/performer/legend/strong_newbie/emotion。\n"
            "- messages：群历史消息，需要 group_id；count 默认 30，可用 message_seq 翻页。\n"
            "- essence：群精华消息，需要 group_id。\n"
            "- system_messages：群系统消息（加群/邀请请求）；处理请求时使用返回的 request_id 作为 flag。\n"
            "- shut_list：群禁言列表，需要 group_id。\n"
            "- at_all_remain：剩余 @全体成员 次数，需要 group_id。\n"
            "省略 group_id 时使用当前群聊的群号。"
        ),
        "actions": GROUP_READ_ACTIONS,
        "parameters": _tool_parameters(
            GROUP_READ_ACTIONS,
            {
                "group_id": {
                    "type": "string",
                    "description": "群号；省略时使用当前群。",
                },
                "user_id": {"type": "string", "description": "目标 QQ 号。"},
                "folder_id": {"type": "string", "description": "群文件夹 ID。"},
                "message_seq": {
                    "type": "string",
                    "description": "消息序号；用于从指定位置继续读取。",
                },
                "keyword": {
                    "type": "string",
                    "description": "按 QQ 号、昵称或群名片过滤群成员。",
                },
                "limit": {
                    "type": "integer",
                    "description": "返回条数，默认 100，最大 500。",
                },
                "count": {
                    "type": "integer",
                    "description": "历史消息条数，默认 30，最大 100。",
                },
                "offset": {
                    "type": "integer",
                    "description": "分页偏移，默认 0。",
                },
                "honor_type": {
                    "type": "string",
                    "enum": [
                        "all",
                        "talkative",
                        "performer",
                        "legend",
                        "strong_newbie",
                        "emotion",
                    ],
                    "description": "群荣誉类型，默认 all。",
                },
            },
        ),
    },
    "group_info_write": {
        "name": "qq_group_info_write",
        "description": (
            "修改 QQ 群本身的信息。用 action 选择操作：\n"
            "- set_name：修改群名，需要 group_id 和 value。\n"
            "- set_portrait：修改群头像，需要 group_id 和 image（http(s) 地址或 base64）。\n"
            "- set_remark：修改机器人对群的备注，需要 group_id 和 value（可为空）。\n"
            "- send_announcement：发布群公告，需要 group_id 和 value；可附 image。\n"
            "- delete_announcement：删除群公告，需要 group_id 和 notice_id。\n"
            "省略 group_id 时使用当前群聊的群号。"
        ),
        "actions": GROUP_INFO_WRITE_ACTIONS,
        "parameters": _tool_parameters(
            GROUP_INFO_WRITE_ACTIONS,
            {
                "group_id": {
                    "type": "string",
                    "description": "群号；省略时使用当前群。",
                },
                "value": {
                    "type": "string",
                    "description": "群名、群备注或公告正文；set_remark 允许空字符串。",
                },
                "image": {
                    "type": "string",
                    "description": "图片地址（http/https）或 base64 图片数据。",
                },
                "notice_id": {"type": "string", "description": "群公告 ID。"},
            },
        ),
    },
    "group_member_write": {
        "name": "qq_group_member_write",
        "description": (
            "管理 QQ 群成员。用 action 选择操作：\n"
            "- set_card：修改群名片，需要 group_id、user_id 和 value。\n"
            "- set_special_title：设置专属头衔，需要 group_id、user_id 和 value。\n"
            "- set_admin：设置或取消管理员，需要 group_id、user_id 和 enable。\n"
            "- ban：禁言或解除禁言，需要 group_id、user_id 和 duration（秒，0 表示解除）。\n"
            "- whole_ban：全员禁言或解除，需要 group_id 和 enable。\n"
            "- kick：踢出成员，需要 group_id 和 user_id；reject_add_request 表示是否拒绝再次加群。\n"
            "省略 group_id 时使用当前群聊的群号。"
        ),
        "actions": GROUP_MEMBER_WRITE_ACTIONS,
        "parameters": _tool_parameters(
            GROUP_MEMBER_WRITE_ACTIONS,
            {
                "group_id": {
                    "type": "string",
                    "description": "群号；省略时使用当前群。",
                },
                "user_id": {"type": "string", "description": "目标群成员 QQ 号。"},
                "value": {
                    "type": "string",
                    "description": "群名片或专属头衔；可为空以清除。",
                },
                "duration": {
                    "type": "integer",
                    "description": "禁言时长（秒），0 表示解除禁言。",
                },
                "enable": {
                    "type": "boolean",
                    "description": "true 为开启，false 为关闭。",
                },
                "reject_add_request": {
                    "type": "boolean",
                    "description": "踢出时是否拒绝该成员再次加群。",
                },
            },
        ),
    },
    "group_file_write": {
        "name": "qq_group_file_write",
        "description": (
            "管理 QQ 群文件。用 action 选择操作：\n"
            "- upload：上传文件，需要 group_id、file（http(s) 地址或 base64）、name；folder_id 可选。\n"
            "- delete：删除文件，需要 group_id、file_id 和 busid。\n"
            "- create_folder：在群文件根目录创建文件夹，需要 group_id 和 name。\n"
            "- delete_folder：删除文件夹，需要 group_id 和 folder_id。\n"
            "- move：移动文件，需要 group_id、file_id、folder_id（当前目录）和 target_folder_id。\n"
            "省略 group_id 时使用当前群聊的群号。"
        ),
        "actions": GROUP_FILE_WRITE_ACTIONS,
        "parameters": _tool_parameters(
            GROUP_FILE_WRITE_ACTIONS,
            {
                "group_id": {
                    "type": "string",
                    "description": "群号；省略时使用当前群。",
                },
                "file": {
                    "type": "string",
                    "description": "上传文件的 http(s) 地址或 base64 数据。",
                },
                "name": {"type": "string", "description": "文件名或文件夹名。"},
                "folder_id": {"type": "string", "description": "群文件夹 ID。"},
                "file_id": {"type": "string", "description": "群文件 ID。"},
                "busid": {"type": "integer", "description": "群文件 busid。"},
                "target_folder_id": {
                    "type": "string",
                    "description": "移动文件的目标文件夹 ID。",
                },
            },
        ),
    },
    "group_message_write": {
        "name": "qq_group_message_write",
        "description": (
            "操作 QQ 群消息状态。用 action 选择操作：\n"
            "- set_essence：把消息设为精华，需要 message_id。\n"
            "- delete_essence：移除精华消息，需要 message_id。\n"
            "- recall：撤回消息，需要 message_id。\n"
            "- reaction：给消息添加或移除表情回应，需要 message_id、emoji_id 和 set_reaction。\n"
            "- mark_read：标记消息已读，需要 message_id。\n"
            "- handle_request：处理加群请求，需要 flag（system_messages 返回的 request_id）、sub_type（add/invite）、approve；reason 可选。"
        ),
        "actions": GROUP_MESSAGE_WRITE_ACTIONS,
        "parameters": _tool_parameters(
            GROUP_MESSAGE_WRITE_ACTIONS,
            {
                "message_id": {"type": "string", "description": "消息 ID。"},
                "emoji_id": {
                    "type": "string",
                    "description": "表情 ID，例如 128512。",
                },
                "set_reaction": {
                    "type": "boolean",
                    "description": "true 添加回应，false 移除回应。",
                },
                "flag": {"type": "string", "description": "加群请求的 flag。"},
                "sub_type": {
                    "type": "string",
                    "enum": ["add", "invite"],
                    "description": "加群请求类型。",
                },
                "approve": {
                    "type": "boolean",
                    "description": "true 同意，false 拒绝。",
                },
                "reason": {"type": "string", "description": "拒绝理由。"},
            },
        ),
    },
    "group_lifecycle_write": {
        "name": "qq_group_lifecycle_write",
        "description": (
            "处理 QQ 群生命周期。action 仅支持 leave：退出群或解散群，需要 group_id；"
            "is_dismiss 为 true 时解散群，否则退出群。"
        ),
        "actions": GROUP_LIFECYCLE_WRITE_ACTIONS,
        "parameters": _tool_parameters(
            GROUP_LIFECYCLE_WRITE_ACTIONS,
            {
                "group_id": {
                    "type": "string",
                    "description": "群号；省略时使用当前群。",
                },
                "is_dismiss": {
                    "type": "boolean",
                    "description": "true 表示解散群，false 表示退出群。",
                },
            },
        ),
    },
    "self_read": {
        "name": "qq_self_read",
        "description": (
            "读取机器人自己的 QQ 信息。用 action 选择内容：\n"
            "- account：登录账号、在线状态和协议版本。\n"
            "- profile：自己的 QQ 资料。\n"
            "- likes：自己收到的点赞资料；limit、offset 分页。\n"
            "- friends：好友列表；limit、offset 分页。"
        ),
        "actions": SELF_READ_ACTIONS,
        "parameters": _tool_parameters(
            SELF_READ_ACTIONS,
            {
                "limit": {
                    "type": "integer",
                    "description": "返回条数，默认 100，最大 500。",
                },
                "offset": {
                    "type": "integer",
                    "description": "分页偏移，默认 0。",
                },
            },
        ),
    },
    "self_write": {
        "name": "qq_self_write",
        "description": (
            "修改机器人自己的 QQ 信息。用 action 选择操作：\n"
            "- set_profile：修改昵称、签名或性别；nickname 省略时保留当前昵称。\n"
            "- set_avatar：修改头像，需要 file（http(s) 地址或 base64）。\n"
            "- set_online_status：修改在线状态，需要 status（online/qme/away/busy/dnd/invisible 或数字）；"
            "ext_status、battery_status 可选。\n"
            "- set_input_status：设置输入状态，需要 user_id 和 event_type（1 输入中，2 停止输入）。\n"
            "- set_long_nick：修改长昵称，需要 long_nick。"
        ),
        "actions": SELF_WRITE_ACTIONS,
        "parameters": _tool_parameters(
            SELF_WRITE_ACTIONS,
            {
                "nickname": {"type": "string", "description": "新昵称。"},
                "signature": {"type": "string", "description": "新个性签名。"},
                "sex": {
                    "type": "string",
                    "description": "性别：unknown/male/female 或 0/1/2。",
                },
                "file": {
                    "type": "string",
                    "description": "头像图片的 http(s) 地址或 base64 数据。",
                },
                "status": {
                    "type": "string",
                    "description": "在线状态：online/qme/away/busy/dnd/invisible，或数字状态码。",
                },
                "ext_status": {
                    "type": "integer",
                    "description": "扩展状态，默认 0。",
                },
                "battery_status": {
                    "type": "integer",
                    "description": "电量状态，默认 0。",
                },
                "user_id": {
                    "type": "string",
                    "description": "输入状态的接收者 QQ 号。",
                },
                "event_type": {
                    "type": "integer",
                    "description": "输入状态事件类型：1 输入中，2 停止输入。",
                },
                "long_nick": {"type": "string", "description": "新的长昵称。"},
            },
        ),
    },
    "user_read": {
        "name": "qq_user_read",
        "description": (
            "读取其他 QQ 用户的信息。用 action 选择内容：\n"
            "- info：陌生人或好友资料，需要 user_id。\n"
            "- private_messages：与指定用户的私聊历史，需要 user_id；count 默认 30。\n"
            "- likes：指定用户的点赞资料，需要 user_id；limit、offset 分页。\n"
            "- requests：可疑好友申请列表；返回的 flag 可用于 handle_request 或 handle_doubt_request。"
        ),
        "actions": USER_READ_ACTIONS,
        "parameters": _tool_parameters(
            USER_READ_ACTIONS,
            {
                "user_id": {"type": "string", "description": "目标 QQ 号。"},
                "count": {
                    "type": "integer",
                    "description": "私聊消息条数，默认 30，最大 100。",
                },
                "message_seq": {
                    "type": "string",
                    "description": "消息序号；用于从指定位置继续读取。",
                },
                "limit": {
                    "type": "integer",
                    "description": "返回条数，默认 100，最大 500。",
                },
                "offset": {
                    "type": "integer",
                    "description": "分页偏移，默认 0。",
                },
            },
        ),
    },
    "user_write": {
        "name": "qq_user_write",
        "description": (
            "修改与其他 QQ 用户相关的信息。用 action 选择操作：\n"
            "- set_remark：修改好友备注，需要 user_id 和 remark（可为空）。\n"
            "- like：给用户点赞，需要 user_id；times 默认 10，最大 10。\n"
            "- poke：戳一戳用户，需要 user_id。\n"
            "- delete_friend：删除好友，需要 user_id。\n"
            "- handle_request：处理好友请求，需要 flag 和 approve；remark 可选。\n"
            "- handle_doubt_request：处理可疑好友申请，需要 flag；approve 必须为 true。"
        ),
        "actions": USER_WRITE_ACTIONS,
        "parameters": _tool_parameters(
            USER_WRITE_ACTIONS,
            {
                "user_id": {"type": "string", "description": "目标 QQ 号。"},
                "remark": {"type": "string", "description": "好友备注；可为空。"},
                "times": {
                    "type": "integer",
                    "description": "点赞次数，默认 10，最大 10。",
                },
                "flag": {"type": "string", "description": "好友请求的 flag。"},
                "approve": {
                    "type": "boolean",
                    "description": "true 同意，false 拒绝。",
                },
            },
        ),
    },
}


@dataclass
class QQOneBotTool(FunctionTool[AstrAgentContext]):
    """把工具调用转发给插件实例的通用 OneBot 工具。"""

    __pydantic_config__ = {"arbitrary_types_allowed": True}  # noqa: RUF012

    plugin: Any = Field(default=None, repr=False)
    tool_key: str = ""

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        **kwargs,
    ) -> ToolExecResult:
        return await self.plugin.handle_tool(self.tool_key, context, kwargs)


class QQApiPlugin(Star):
    """QQ 接口插件。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context, config)
        self.config = config or {}
        self._registered_tool_names: list[str] = []
        self._init_poke_reply()
        self._register_tools()

    def _init_poke_reply(self) -> None:
        poke_config = self.config.get("poke_reply")
        if not isinstance(poke_config, dict):
            poke_config = {}
        self.poke_enabled = _bool_value(poke_config.get("enable"), True)
        mode = str(poke_config.get("response_mode") or POKE_MODE_RANDOM).strip().lower()
        self.poke_response_mode = (
            mode if mode in {POKE_MODE_RANDOM, POKE_MODE_LLM} else POKE_MODE_RANDOM
        )
        configured_reply_texts = poke_config.get("reply_texts")
        self.poke_reply_texts = (
            _as_text_list(configured_reply_texts)
            if configured_reply_texts is not None
            else list(POKE_DEFAULT_REPLY_TEXTS)
        )
        prompt = str(poke_config.get("llm_prompt") or "").strip()
        self.poke_llm_prompt = prompt or POKE_DEFAULT_LLM_PROMPT
        self.poke_cooldown_seconds = _as_non_negative_float(
            poke_config.get("cooldown_seconds"),
            0.0,
        )
        self._poke_last_reply_at: dict[tuple[str, str], float] = {}

    # ------------------------------------------------------------ 工具注册

    def _is_action_enabled(self, tool_key: str, action: str) -> bool:
        """配置缺省时使用内置默认值，保证旧配置仍可工作。"""
        default_enabled = action in DEFAULT_ENABLED_ACTIONS.get(tool_key, set())
        section = self.config.get(tool_key)
        if not isinstance(section, dict):
            return default_enabled
        return _bool_value(section.get(action), default_enabled)

    def _enabled_actions(self, tool_key: str) -> tuple[str, ...]:
        spec = TOOL_SPECS[tool_key]
        return tuple(
            action
            for action in spec["actions"]
            if self._is_action_enabled(tool_key, action)
        )

    def _register_tools(self) -> None:
        if not _bool_value(self.config.get("enable"), True):
            return
        for tool_key, spec in TOOL_SPECS.items():
            enabled = self._enabled_actions(tool_key)
            if not enabled:
                continue
            parameters = copy.deepcopy(spec["parameters"])
            parameters["properties"]["action"]["enum"] = list(enabled)
            tool = QQOneBotTool(
                name=spec["name"],
                description=spec["description"],
                parameters=parameters,
                plugin=self,
                tool_key=tool_key,
            )
            self.context.add_llm_tools(tool)
            self._registered_tool_names.append(spec["name"])

    async def terminate(self) -> None:
        for name in self._registered_tool_names:
            try:
                self.context.provider_manager.llm_tools.remove_func(name)
            except Exception:  # noqa: BLE001 - 插件卸载时应尽量清理
                logger.exception("[%s] 移除工具 %s 失败", PLUGIN_NAME, name)
        self._registered_tool_names.clear()
        self._poke_last_reply_at.clear()

    # ------------------------------------------------------------ 工具入口

    async def handle_tool(
        self,
        tool_key: str,
        context: ContextWrapper[AstrAgentContext],
        kwargs: dict[str, Any],
    ) -> str:
        if not _bool_value(self.config.get("enable"), True):
            return "QQ 接口插件已停用。"
        event = getattr(getattr(context, "context", None), "event", None)
        if event is None:
            return "无法获取当前消息事件，QQ 接口工具只能在消息处理过程中使用。"
        action = str(kwargs.get("action") or "").strip()
        if action not in TOOL_SPECS[tool_key]["actions"]:
            return f"不支持的操作：{action}"
        if not self._is_action_enabled(tool_key, action):
            return f"操作 {action} 未在插件配置中启用。"
        if tool_key in WRITE_TOOL_KEYS:
            denied = self._check_write_permission(event)
            if denied:
                return denied
        try:
            return await self._dispatch(tool_key, action, event, kwargs)
        except QQApiError as exc:
            return f"操作失败：{exc}"
        except Exception as exc:  # noqa: BLE001 - 工具应把错误返回给模型
            logger.exception("[%s] 工具 %s 执行异常", PLUGIN_NAME, tool_key)
            return f"操作失败：{exc}"

    def _check_write_permission(self, event: Any) -> str:
        if not _bool_value(self.config.get("write_require_admin"), True):
            return ""
        sender_id = str(getattr(event, "get_sender_id", lambda: "")() or "")
        allowed = {
            str(item)
            for item in (self.config.get("write_allowed_users") or [])
            if str(item).strip()
        }
        if sender_id and sender_id in allowed:
            return ""
        if getattr(event, "role", "") == "admin":
            return ""
        admins = {
            str(item) for item in (self.context.get_config().get("admins_id") or [])
        }
        if sender_id and sender_id in admins:
            return ""
        return "该操作会修改 QQ 内容，需要 AstrBot 管理员权限。当前用户未被授权。"

    async def _dispatch(
        self,
        tool_key: str,
        action: str,
        event: Any,
        kwargs: dict[str, Any],
    ) -> str:
        handlers = {
            "group_read": self._group_read,
            "group_info_write": self._group_info_write,
            "group_member_write": self._group_member_write,
            "group_file_write": self._group_file_write,
            "group_message_write": self._group_message_write,
            "group_lifecycle_write": self._group_lifecycle_write,
            "self_read": self._self_read,
            "self_write": self._self_write,
            "user_read": self._user_read,
            "user_write": self._user_write,
        }
        return await handlers[tool_key](event, action, kwargs)

    # ------------------------------------------------------------ 公共参数

    def _group_id(self, event: Any, kwargs: dict[str, Any]) -> str:
        raw = kwargs.get("group_id")
        if raw in (None, ""):
            raw = getattr(event, "get_group_id", lambda: "")()
        value = str(raw or "").strip()
        if not value:
            raise QQApiError("缺少 group_id，且当前会话不是群聊")
        return value

    def _user_id(self, kwargs: dict[str, Any]) -> str:
        value = str(kwargs.get("user_id") or "").strip()
        if not value:
            raise QQApiError("缺少 user_id")
        return value

    def _value(self, kwargs: dict[str, Any], *, allow_empty: bool = False) -> str:
        if "value" not in kwargs or kwargs.get("value") is None:
            raise QQApiError("缺少 value")
        value = str(kwargs.get("value")).strip()
        if not value and not allow_empty:
            raise QQApiError("value 不能为空")
        return value

    def _limit(self, kwargs: dict[str, Any]) -> int:
        configured = _int_value(
            self.config.get("limits", {}).get("max_list_items", 100)
            if isinstance(self.config.get("limits"), dict)
            else 100,
            name="max_list_items",
            default=100,
            minimum=10,
            maximum=500,
        )
        return _int_value(
            kwargs.get("limit"),
            name="limit",
            default=configured,
            minimum=1,
            maximum=min(configured, 500),
        )

    def _offset(self, kwargs: dict[str, Any]) -> int:
        return _int_value(kwargs.get("offset"), name="offset", default=0, minimum=0)

    def _max_message_chars(self) -> int:
        limits = self.config.get("limits")
        if not isinstance(limits, dict):
            return 8000
        return _int_value(
            limits.get("max_message_chars"),
            name="max_message_chars",
            default=8000,
            minimum=1000,
            maximum=30000,
        )

    # ------------------------------------------------------------ OneBot 调用

    def _get_bot(self, event: Any) -> Any:
        platform_name = getattr(event, "get_platform_name", lambda: "")()
        if platform_name != SUPPORTED_PLATFORM:
            raise QQApiError(
                f"QQ 接口仅支持 aiocqhttp 平台，当前平台为 {platform_name or '未知'}"
            )
        platform_id = event.get_platform_id()
        platform = self.context.get_platform_inst(platform_id)
        bot = getattr(platform, "bot", None)
        if bot is None:
            raise QQApiError("未找到 OneBot 机器人实例，请检查平台适配器是否已连接")
        return bot

    async def _call(self, event: Any, action: str, **params: Any) -> Any:
        bot = self._get_bot(event)
        # 保留空字符串：清空群备注、群名片等操作需要显式传空值。
        cleaned = {key: value for key, value in params.items() if value is not None}
        self_id = str(getattr(event, "get_self_id", lambda: "")() or "")
        if self_id.isdigit():
            cleaned.setdefault("self_id", int(self_id))
        try:
            return await bot.call_action(action, **cleaned)
        except Exception as exc:
            raise QQApiError(f"OneBot 接口 {action} 调用失败：{exc}") from exc

    # ------------------------------------------------------------ 群读取

    async def _group_read(self, event: Any, action: str, kwargs: dict[str, Any]) -> str:
        if action == "list":
            data = await self._call(event, "get_group_list")
            groups = data if isinstance(data, list) else []
            return _json_result({"groups": [_summarize_group(g) for g in groups]})

        if action == "system_messages":
            data = await self._call(event, "get_group_system_msg")
            return _json_result(data)

        group_id = self._group_id(event, kwargs)

        if action == "info":
            data = await self._call(event, "get_group_info", group_id=group_id)
            if isinstance(data, dict):
                data = _format_time_fields(data, ("create_time", "group_create_time"))
            return _json_result(data)

        if action == "members":
            data = await self._call(event, "get_group_member_list", group_id=group_id)
            members = data if isinstance(data, list) else []
            keyword = str(kwargs.get("keyword") or "").strip().lower()
            if keyword:
                members = [
                    member
                    for member in members
                    if isinstance(member, dict)
                    and (
                        keyword in str(member.get("user_id", "")).lower()
                        or keyword in str(member.get("nickname", "")).lower()
                        or keyword in str(member.get("card", "")).lower()
                    )
                ]
            page, total = _paged(members, self._limit(kwargs), self._offset(kwargs))
            return _json_result(
                {
                    "total": total,
                    "members": [_summarize_member(member) for member in page],
                }
            )

        if action == "member":
            data = await self._call(
                event,
                "get_group_member_info",
                group_id=group_id,
                user_id=self._user_id(kwargs),
            )
            return _json_result(_summarize_member(data))

        if action == "announcements":
            data = await self._call(event, "_get_group_notice", group_id=group_id)
            items = data if isinstance(data, list) else []
            page, total = _paged(items, self._limit(kwargs), self._offset(kwargs))
            return _json_result(
                {
                    "total": total,
                    "announcements": [_summarize_announcement(item) for item in page],
                }
            )

        if action == "files":
            folder_id = str(kwargs.get("folder_id") or "").strip()
            if folder_id:
                data = await self._call(
                    event,
                    "get_group_files_by_folder",
                    group_id=group_id,
                    folder_id=folder_id,
                )
            else:
                data = await self._call(
                    event, "get_group_root_files", group_id=group_id
                )
            file_system: Any = None
            try:
                file_system = await self._call(
                    event, "get_group_file_system_info", group_id=group_id
                )
            except QQApiError as exc:
                logger.debug("[%s] 读取群文件系统信息失败: %s", PLUGIN_NAME, exc)
            files = data.get("files", []) if isinstance(data, dict) else []
            folders = data.get("folders", []) if isinstance(data, dict) else []
            files = [item for item in files if isinstance(item, dict)]
            folders = [item for item in folders if isinstance(item, dict)]
            page, total = _paged(files, self._limit(kwargs), self._offset(kwargs))
            return _json_result(
                {
                    "file_system": file_system,
                    "total_files": total,
                    "folders": [_summarize_folder(folder) for folder in folders],
                    "files": [_summarize_file(file_info) for file_info in page],
                }
            )

        if action == "honor":
            honor_type = str(kwargs.get("honor_type") or "all")
            data = await self._call(
                event,
                "get_group_honor_info",
                group_id=group_id,
                type=honor_type,
            )
            return _json_result(data)

        if action == "messages":
            count = _int_value(
                kwargs.get("count"),
                name="count",
                default=30,
                minimum=1,
                maximum=100,
            )
            params: dict[str, Any] = {"group_id": group_id, "count": count}
            message_seq = str(kwargs.get("message_seq") or "").strip()
            if message_seq:
                params["message_seq"] = (
                    int(message_seq) if message_seq.isdigit() else message_seq
                )
            data = await self._call(event, "get_group_msg_history", **params)
            messages = data.get("messages", []) if isinstance(data, dict) else []
            summarized, returned = _summarize_messages(
                messages, self._max_message_chars()
            )
            return _json_result({"returned": returned, "messages": summarized})

        if action == "essence":
            data = await self._call(event, "get_essence_msg_list", group_id=group_id)
            items = data if isinstance(data, list) else []
            page, total = _paged(items, self._limit(kwargs), self._offset(kwargs))
            return _json_result(
                {
                    "total": total,
                    "essence": [_summarize_essence(item) for item in page],
                }
            )

        if action == "shut_list":
            data = await self._call(event, "get_group_shut_list", group_id=group_id)
            items = data if isinstance(data, list) else []
            page, total = _paged(items, self._limit(kwargs), self._offset(kwargs))
            return _json_result(
                {
                    "total": total,
                    "members": [
                        _format_time_fields(item, ("shut_up_time",)) for item in page
                    ],
                }
            )

        if action == "at_all_remain":
            data = await self._call(event, "get_group_at_all_remain", group_id=group_id)
            return _json_result(data)

        raise QQApiError(f"未实现的群读取操作：{action}")

    # ------------------------------------------------------------ 群写入

    async def _group_info_write(
        self, event: Any, action: str, kwargs: dict[str, Any]
    ) -> str:
        group_id = self._group_id(event, kwargs)

        if action == "set_name":
            data = await self._call(
                event,
                "set_group_name",
                group_id=group_id,
                group_name=self._value(kwargs),
            )
            return _json_result({"ok": True, "data": data})

        if action == "set_portrait":
            image = kwargs.get("image") or kwargs.get("file")
            data = await self._call(
                event,
                "set_group_portrait",
                group_id=group_id,
                file=_validate_remote_file(image, "image"),
            )
            return _json_result({"ok": True, "data": data})

        if action == "set_remark":
            data = await self._call(
                event,
                "set_group_remark",
                group_id=group_id,
                remark=self._value(kwargs, allow_empty=True),
            )
            return _json_result({"ok": True, "data": data})

        if action == "send_announcement":
            params: dict[str, Any] = {
                "group_id": group_id,
                "content": self._value(kwargs),
            }
            image = str(kwargs.get("image") or "").strip()
            if image:
                params["image"] = _validate_remote_file(image, "image")
            data = await self._call(event, "_send_group_notice", **params)
            return _json_result({"ok": True, "data": data})

        if action == "delete_announcement":
            notice_id = str(kwargs.get("notice_id") or "").strip()
            if not notice_id:
                raise QQApiError("缺少 notice_id")
            data = await self._call(
                event,
                "_del_group_notice",
                group_id=group_id,
                notice_id=notice_id,
            )
            return _json_result({"ok": True, "data": data})

        raise QQApiError(f"未实现的群资料操作：{action}")

    async def _group_member_write(
        self, event: Any, action: str, kwargs: dict[str, Any]
    ) -> str:
        group_id = self._group_id(event, kwargs)

        if action == "set_card":
            data = await self._call(
                event,
                "set_group_card",
                group_id=group_id,
                user_id=self._user_id(kwargs),
                card=self._value(kwargs, allow_empty=True),
            )
            return _json_result({"ok": True, "data": data})

        if action == "set_special_title":
            data = await self._call(
                event,
                "set_group_special_title",
                group_id=group_id,
                user_id=self._user_id(kwargs),
                special_title=self._value(kwargs, allow_empty=True),
            )
            return _json_result({"ok": True, "data": data})

        if action == "set_admin":
            if "enable" not in kwargs:
                raise QQApiError("缺少 enable")
            data = await self._call(
                event,
                "set_group_admin",
                group_id=group_id,
                user_id=self._user_id(kwargs),
                enable=_bool_value(kwargs.get("enable")),
            )
            return _json_result({"ok": True, "data": data})

        if action == "ban":
            duration = _int_value(kwargs.get("duration"), name="duration", minimum=0)
            data = await self._call(
                event,
                "set_group_ban",
                group_id=group_id,
                user_id=self._user_id(kwargs),
                duration=duration,
            )
            return _json_result({"ok": True, "data": data})

        if action == "whole_ban":
            if "enable" not in kwargs:
                raise QQApiError("缺少 enable")
            data = await self._call(
                event,
                "set_group_whole_ban",
                group_id=group_id,
                enable=_bool_value(kwargs.get("enable")),
            )
            return _json_result({"ok": True, "data": data})

        if action == "kick":
            data = await self._call(
                event,
                "set_group_kick",
                group_id=group_id,
                user_id=self._user_id(kwargs),
                reject_add_request=_bool_value(kwargs.get("reject_add_request"), False),
            )
            return _json_result({"ok": True, "data": data})

        raise QQApiError(f"未实现的群成员操作：{action}")

    async def _group_file_write(
        self, event: Any, action: str, kwargs: dict[str, Any]
    ) -> str:
        group_id = self._group_id(event, kwargs)

        if action == "upload":
            file_value = kwargs.get("file")
            name = str(kwargs.get("name") or "").strip()
            if not name:
                raise QQApiError("缺少 name")
            params: dict[str, Any] = {
                "group_id": group_id,
                "file": _validate_remote_file(file_value, "file"),
                "name": name,
            }
            folder_id = str(kwargs.get("folder_id") or "").strip()
            if folder_id:
                params["folder"] = folder_id
            data = await self._call(event, "upload_group_file", **params)
            return _json_result({"ok": True, "data": data})

        if action == "delete":
            file_id = str(kwargs.get("file_id") or "").strip()
            if not file_id:
                raise QQApiError("缺少 file_id")
            busid = _int_value(kwargs.get("busid"), name="busid", default=0)
            data = await self._call(
                event,
                "delete_group_file",
                group_id=group_id,
                file_id=file_id,
                busid=busid,
            )
            return _json_result({"ok": True, "data": data})

        if action == "create_folder":
            name = str(kwargs.get("name") or "").strip()
            if not name:
                raise QQApiError("缺少 name")
            data = await self._call(
                event,
                "create_group_file_folder",
                group_id=group_id,
                name=name,
            )
            return _json_result({"ok": True, "data": data})

        if action == "delete_folder":
            folder_id = str(kwargs.get("folder_id") or "").strip()
            if not folder_id:
                raise QQApiError("缺少 folder_id")
            data = await self._call(
                event,
                "delete_group_folder",
                group_id=group_id,
                folder_id=folder_id,
            )
            return _json_result({"ok": True, "data": data})

        if action == "move":
            file_id = str(kwargs.get("file_id") or "").strip()
            current_folder = str(kwargs.get("folder_id") or "").strip()
            target_folder = str(kwargs.get("target_folder_id") or "").strip()
            if not file_id:
                raise QQApiError("缺少 file_id")
            if not current_folder or not target_folder:
                raise QQApiError("move 需要 folder_id 和 target_folder_id")
            data = await self._call(
                event,
                "move_group_file",
                group_id=group_id,
                file_id=file_id,
                current_parent_directory=current_folder,
                target_parent_directory=target_folder,
            )
            return _json_result({"ok": True, "data": data})

        raise QQApiError(f"未实现的群文件操作：{action}")

    async def _group_message_write(
        self, event: Any, action: str, kwargs: dict[str, Any]
    ) -> str:
        if action == "handle_request":
            flag = str(kwargs.get("flag") or "").strip()
            if not flag:
                raise QQApiError("缺少 flag")
            sub_type = str(kwargs.get("sub_type") or "").strip()
            if sub_type not in {"add", "invite"}:
                raise QQApiError("sub_type 必须是 add 或 invite")
            if "approve" not in kwargs:
                raise QQApiError("缺少 approve")
            data = await self._call(
                event,
                "set_group_add_request",
                flag=flag,
                sub_type=sub_type,
                approve=_bool_value(kwargs.get("approve")),
                reason=str(kwargs.get("reason") or "").strip(),
            )
            return _json_result({"ok": True, "data": data})

        message_id = str(kwargs.get("message_id") or "").strip()
        if not message_id:
            raise QQApiError("缺少 message_id")
        message_id_value: Any = int(message_id) if message_id.isdigit() else message_id

        if action == "set_essence":
            data = await self._call(
                event, "set_essence_msg", message_id=message_id_value
            )
            return _json_result({"ok": True, "data": data})

        if action == "delete_essence":
            data = await self._call(
                event, "delete_essence_msg", message_id=message_id_value
            )
            return _json_result({"ok": True, "data": data})

        if action == "recall":
            data = await self._call(event, "delete_msg", message_id=message_id_value)
            return _json_result({"ok": True, "data": data})

        if action == "reaction":
            emoji_id = str(kwargs.get("emoji_id") or "").strip()
            if not emoji_id:
                raise QQApiError("缺少 emoji_id")
            if "set_reaction" not in kwargs:
                raise QQApiError("缺少 set_reaction")
            data = await self._call(
                event,
                "set_msg_emoji_like",
                message_id=message_id_value,
                emoji_id=emoji_id,
                set=_bool_value(kwargs.get("set_reaction")),
            )
            return _json_result({"ok": True, "data": data})

        if action == "mark_read":
            data = await self._call(
                event, "mark_msg_as_read", message_id=message_id_value
            )
            return _json_result({"ok": True, "data": data})

        raise QQApiError(f"未实现的群消息操作：{action}")

    async def _group_lifecycle_write(
        self, event: Any, action: str, kwargs: dict[str, Any]
    ) -> str:
        if action != "leave":
            raise QQApiError(f"未实现的群生命周期操作：{action}")
        data = await self._call(
            event,
            "set_group_leave",
            group_id=self._group_id(event, kwargs),
            is_dismiss=_bool_value(kwargs.get("is_dismiss"), False),
        )
        return _json_result({"ok": True, "data": data})

    # ------------------------------------------------------------ 自身

    async def _self_read(self, event: Any, action: str, kwargs: dict[str, Any]) -> str:
        if action == "account":
            login_info, status, version = await asyncio.gather(
                self._call(event, "get_login_info"),
                self._call(event, "get_status"),
                self._call(event, "get_version_info"),
                return_exceptions=True,
            )
            return _json_result(
                {
                    "login_info": _exception_text(login_info),
                    "status": _exception_text(status),
                    "version": _exception_text(version),
                }
            )

        if action == "profile":
            self_id = str(getattr(event, "get_self_id", lambda: "")() or "").strip()
            try:
                data = await self._call(event, "get_stranger_info", user_id=self_id)
            except QQApiError:
                data = await self._call(event, "get_login_info")
            return _json_result(_summarize_profile(data))

        if action == "likes":
            self_id = str(getattr(event, "get_self_id", lambda: "")() or "").strip()
            limit = self._limit(kwargs)
            params: dict[str, Any] = {
                "start": self._offset(kwargs),
                "count": limit,
            }
            if self_id:
                params["user_id"] = self_id
            data = await self._call(event, "get_profile_like", **params)
            return _json_result(_summarize_likes(data, limit))

        if action == "friends":
            data = await self._call(event, "get_friend_list")
            friends = data if isinstance(data, list) else []
            page, total = _paged(friends, self._limit(kwargs), self._offset(kwargs))
            return _json_result(
                {
                    "total": total,
                    "friends": [_summarize_friend(friend) for friend in page],
                }
            )

        raise QQApiError(f"未实现的自身读取操作：{action}")

    async def _self_write(self, event: Any, action: str, kwargs: dict[str, Any]) -> str:
        if action == "set_profile":
            nickname = str(kwargs.get("nickname") or "").strip()
            if not nickname:
                login_info = await self._call(event, "get_login_info")
                if isinstance(login_info, dict):
                    nickname = str(login_info.get("nickname") or "").strip()
            if not nickname:
                raise QQApiError("无法确定当前昵称，请提供 nickname")
            params: dict[str, Any] = {"nickname": nickname}
            if "signature" in kwargs and kwargs.get("signature") is not None:
                params["personal_note"] = str(kwargs.get("signature") or "")
            if "sex" in kwargs and kwargs.get("sex") not in (None, ""):
                sex_raw = str(kwargs.get("sex")).strip().lower()
                if sex_raw in _SEX_VALUES:
                    params["sex"] = _SEX_VALUES[sex_raw]
                else:
                    params["sex"] = _int_value(
                        sex_raw, name="sex", minimum=0, maximum=2
                    )
            data = await self._call(event, "set_qq_profile", **params)
            return _json_result({"ok": True, "data": data})

        if action == "set_avatar":
            data = await self._call(
                event,
                "set_qq_avatar",
                file=_validate_remote_file(kwargs.get("file"), "file"),
            )
            return _json_result({"ok": True, "data": data})

        if action == "set_online_status":
            status_raw = str(kwargs.get("status") or "").strip().lower()
            if not status_raw:
                raise QQApiError("缺少 status")
            if status_raw in ONLINE_STATUS_VALUES:
                status = ONLINE_STATUS_VALUES[status_raw]
            else:
                status = _int_value(status_raw, name="status", minimum=0, maximum=100)
            data = await self._call(
                event,
                "set_online_status",
                status=status,
                ext_status=_int_value(
                    kwargs.get("ext_status"),
                    name="ext_status",
                    default=0,
                    minimum=0,
                ),
                battery_status=_int_value(
                    kwargs.get("battery_status"),
                    name="battery_status",
                    default=0,
                    minimum=0,
                ),
            )
            return _json_result({"ok": True, "data": data})

        if action == "set_input_status":
            event_type = _int_value(
                kwargs.get("event_type"),
                name="event_type",
                default=1,
                minimum=0,
            )
            data = await self._call(
                event,
                "set_input_status",
                user_id=self._user_id(kwargs),
                event_type=event_type,
            )
            return _json_result({"ok": True, "data": data})

        if action == "set_long_nick":
            long_nick = str(kwargs.get("long_nick") or "").strip()
            if not long_nick:
                raise QQApiError("缺少 long_nick")
            data = await self._call(event, "set_self_longnick", longNick=long_nick)
            return _json_result({"ok": True, "data": data})

        raise QQApiError(f"未实现的自身修改操作：{action}")

    # ------------------------------------------------------------ 其他用户

    async def _user_read(self, event: Any, action: str, kwargs: dict[str, Any]) -> str:
        if action == "info":
            data = await self._call(
                event, "get_stranger_info", user_id=self._user_id(kwargs)
            )
            return _json_result(_summarize_profile(data))

        if action == "private_messages":
            count = _int_value(
                kwargs.get("count"),
                name="count",
                default=30,
                minimum=1,
                maximum=100,
            )
            params: dict[str, Any] = {
                "user_id": self._user_id(kwargs),
                "count": count,
            }
            message_seq = str(kwargs.get("message_seq") or "").strip()
            if message_seq:
                params["message_seq"] = (
                    int(message_seq) if message_seq.isdigit() else message_seq
                )
            data = await self._call(event, "get_friend_msg_history", **params)
            messages = data.get("messages", []) if isinstance(data, dict) else []
            summarized, returned = _summarize_messages(
                messages, self._max_message_chars()
            )
            return _json_result({"returned": returned, "messages": summarized})

        if action == "likes":
            limit = self._limit(kwargs)
            data = await self._call(
                event,
                "get_profile_like",
                user_id=self._user_id(kwargs),
                start=self._offset(kwargs),
                count=limit,
            )
            return _json_result(_summarize_likes(data, limit))

        if action == "requests":
            data = await self._call(
                event,
                "get_doubt_friends_add_request",
                count=self._limit(kwargs),
            )
            requests = data if isinstance(data, list) else []
            return _json_result(
                {
                    "requests": [
                        _summarize_friend_request(request) for request in requests
                    ]
                }
            )

        raise QQApiError(f"未实现的其他用户读取操作：{action}")

    async def _user_write(self, event: Any, action: str, kwargs: dict[str, Any]) -> str:
        if action == "set_remark":
            data = await self._call(
                event,
                "set_friend_remark",
                user_id=self._user_id(kwargs),
                remark=str(kwargs.get("remark") or "").strip(),
            )
            return _json_result({"ok": True, "data": data})

        if action == "like":
            times = _int_value(
                kwargs.get("times"),
                name="times",
                default=10,
                minimum=1,
                maximum=10,
            )
            data = await self._call(
                event,
                "send_like",
                user_id=self._user_id(kwargs),
                times=times,
            )
            return _json_result({"ok": True, "data": data})

        if action == "poke":
            data = await self._call(event, "friend_poke", user_id=self._user_id(kwargs))
            return _json_result({"ok": True, "data": data})

        if action == "delete_friend":
            data = await self._call(
                event, "delete_friend", user_id=self._user_id(kwargs)
            )
            return _json_result({"ok": True, "data": data})

        if action == "handle_request":
            flag = str(kwargs.get("flag") or "").strip()
            if not flag:
                raise QQApiError("缺少 flag")
            if "approve" not in kwargs:
                raise QQApiError("缺少 approve")
            params: dict[str, Any] = {
                "flag": flag,
                "approve": _bool_value(kwargs.get("approve")),
            }
            remark = str(kwargs.get("remark") or "").strip()
            if remark:
                params["remark"] = remark
            data = await self._call(event, "set_friend_add_request", **params)
            return _json_result({"ok": True, "data": data})

        if action == "handle_doubt_request":
            flag = str(kwargs.get("flag") or "").strip()
            if not flag:
                raise QQApiError("缺少 flag")
            if "approve" not in kwargs:
                raise QQApiError("缺少 approve")
            if not _bool_value(kwargs.get("approve")):
                raise QQApiError("可疑好友申请只支持同意，approve 必须为 true")
            data = await self._call(
                event,
                "set_doubt_friends_add_request",
                flag=flag,
                approve=True,
            )
            return _json_result({"ok": True, "data": data})

        raise QQApiError(f"未实现的其他用户修改操作：{action}")

    # ------------------------------------------------------------ 戳一戳回应

    @filter.custom_filter(PokeToBotFilter)
    async def handle_poke(self, event: AstrMessageEvent):
        """只处理目标为机器人的戳一戳通知。"""
        if not _bool_value(self.config.get("enable"), True):
            return
        if not self.poke_enabled:
            return
        if event.get_platform_name() != SUPPORTED_PLATFORM:
            return

        target = self._get_poke_target(event)
        if target is None or target != str(event.get_self_id()):
            return
        if str(event.get_sender_id()) == str(event.get_self_id()):
            return

        if self._poke_cooldown_active(event):
            return

        event.set_extra(POKE_REPLY_EXTRA, True)

        if self.poke_response_mode == POKE_MODE_LLM:
            async for result in self._handle_poke_llm(event):
                yield result
            return

        async for result in self._handle_poke_random_text(event):
            yield result

    @staticmethod
    def _get_poke_target(event: AstrMessageEvent) -> str | None:
        for component in event.get_messages():
            if isinstance(component, Poke):
                return _poke_target(component)
        return None

    def _poke_cooldown_active(self, event: AstrMessageEvent) -> bool:
        if self.poke_cooldown_seconds <= 0:
            return False

        key = (event.unified_msg_origin, str(event.get_sender_id()))
        now = time.monotonic()
        last_reply_at = self._poke_last_reply_at.get(key)
        if (
            last_reply_at is not None
            and now - last_reply_at < self.poke_cooldown_seconds
        ):
            return True

        self._poke_last_reply_at[key] = now
        if len(self._poke_last_reply_at) > POKE_COOLDOWN_CACHE_LIMIT:
            expire_before = now - max(
                self.poke_cooldown_seconds,
                POKE_COOLDOWN_RETENTION_SECONDS,
            )
            self._poke_last_reply_at = {
                item_key: timestamp
                for item_key, timestamp in self._poke_last_reply_at.items()
                if timestamp >= expire_before
            }
        return False

    async def _handle_poke_random_text(self, event: AstrMessageEvent):
        """从回复文本列表中随机发送一条。"""
        if not self.poke_reply_texts:
            logger.warning("戳一戳回复文本列表为空，跳过本次回应。")
            event.stop_event()
            return

        yield event.plain_result(random.choice(self.poke_reply_texts))
        event.stop_event()

    async def _handle_poke_llm(self, event: AstrMessageEvent):
        """用配置的提示词走 AstrBot 原有 LLM 回复链路。"""
        prompt = self._format_poke_prompt(event)
        if not prompt:
            logger.warning("戳一戳 LLM 提示词为空，跳过本次回应。")
            event.stop_event()
            return

        conversation = None
        try:
            conversation = await self._get_poke_conversation(event)
        except Exception:  # noqa: BLE001 - 对话服务异常时仍应回复
            logger.exception("获取当前对话失败，本次戳一戳将不带对话上下文请求 LLM。")

        yield event.request_llm(prompt=prompt, conversation=conversation)
        event.stop_event()

    async def _get_poke_conversation(self, event: AstrMessageEvent):
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

    def _format_poke_prompt(self, event: AstrMessageEvent) -> str:
        sender_id = str(event.get_sender_id())
        username = self._get_poke_sender_name(event) or sender_id
        scene = "私聊" if event.is_private_chat() else "群聊"
        return (
            self.poke_llm_prompt.replace("{username}", username)
            .replace("{user_id}", sender_id)
            .replace("{scene}", scene)
        )

    @staticmethod
    def _get_poke_sender_name(event: AstrMessageEvent) -> str:
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


def _exception_text(value: Any) -> Any:
    if isinstance(value, Exception):
        return f"读取失败：{value}"
    return value
