"""QQ 接口插件的针对性单元测试。"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

PLUGIN_DIR = Path(__file__).resolve().parents[1]
MODULE_NAME = "astrbot_plugin_qq_api_testpkg"

if MODULE_NAME not in sys.modules:
    spec = importlib.util.spec_from_file_location(MODULE_NAME, PLUGIN_DIR / "main.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)

main = sys.modules[MODULE_NAME]


class FakeToolManager:
    def __init__(self) -> None:
        self.removed: list[str] = []

    def remove_func(self, name: str) -> None:
        self.removed.append(name)


class FakeProviderManager:
    def __init__(self) -> None:
        self.llm_tools = FakeToolManager()


class FakeBot:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def call_action(self, action, **params):
        self.calls.append((action, params))


class FakePlatform:
    def __init__(self, bot) -> None:
        self.bot = bot


class FakeContext:
    def __init__(self, admins: list[str] | None = None) -> None:
        self.tools: list[object] = []
        self.provider_manager = FakeProviderManager()
        self.admins = admins or []
        self.platform = None

    def add_llm_tools(self, *tools) -> None:
        self.tools.extend(tools)

    def get_config(self):
        return {"admins_id": self.admins}

    def get_platform_inst(self, platform_id):
        return self.platform


class FakeEvent:
    def __init__(
        self,
        *,
        group_id: str = "660402685",
        self_id: str = "3809232719",
        sender_id: str = "839097298",
        role: str = "member",
    ) -> None:
        self.group_id = group_id
        self.self_id = self_id
        self.sender_id = sender_id
        self.role = role

    def get_platform_name(self):
        return "aiocqhttp"

    def get_platform_id(self):
        return "platform1"

    def get_group_id(self):
        return self.group_id

    def get_self_id(self):
        return self.self_id

    def get_sender_id(self):
        return self.sender_id


def make_plugin(config: dict | None = None, admins: list[str] | None = None):
    context = FakeContext(admins=admins)
    plugin = main.QQApiPlugin(context, config or {})
    return plugin, context


def make_wrapper(event: FakeEvent):
    return SimpleNamespace(context=SimpleNamespace(event=event))


def tool_names(context: FakeContext) -> set[str]:
    return {tool.name for tool in context.tools}


class ToolRegistrationTests(unittest.TestCase):
    def test_default_registers_read_tools_only(self):
        plugin, context = make_plugin()
        self.assertEqual(
            tool_names(context),
            {"qq_group_read", "qq_self_read", "qq_user_read"},
        )
        self.assertEqual(
            set(plugin._registered_tool_names),
            {"qq_group_read", "qq_self_read", "qq_user_read"},
        )

    def test_write_tool_registered_when_action_enabled(self):
        _, context = make_plugin(
            {"group_info_write": {"set_name": True, "set_remark": False}}
        )
        self.assertIn("qq_group_info_write", tool_names(context))
        tool = next(
            tool for tool in context.tools if tool.name == "qq_group_info_write"
        )
        self.assertEqual(
            tool.parameters["properties"]["action"]["enum"],
            ["set_name"],
        )

    def test_all_actions_disabled_skips_tool(self):
        _, context = make_plugin(
            {
                "user_read": {
                    "info": False,
                    "private_messages": False,
                    "likes": False,
                    "requests": False,
                }
            }
        )
        self.assertNotIn("qq_user_read", tool_names(context))

    def test_plugin_disabled_registers_nothing(self):
        _, context = make_plugin({"enable": False})
        self.assertEqual(context.tools, [])

    def test_terminate_removes_registered_tools(self):
        plugin, context = make_plugin()
        asyncio.run(plugin.terminate())
        self.assertEqual(
            set(context.provider_manager.llm_tools.removed),
            {"qq_group_read", "qq_self_read", "qq_user_read"},
        )


class PermissionTests(unittest.TestCase):
    def test_write_denied_for_member(self):
        plugin, _ = make_plugin({"group_info_write": {"set_name": True}})
        event = FakeEvent(role="member")
        result = asyncio.run(
            plugin.handle_tool(
                "group_info_write",
                make_wrapper(event),
                {"action": "set_name", "value": "新群名"},
            )
        )
        self.assertIn("管理员权限", result)

    def test_write_allowed_for_admin_role(self):
        plugin, _ = make_plugin({"group_info_write": {"set_name": True}})
        plugin._call = AsyncMock(return_value=None)
        event = FakeEvent(role="admin")
        result = asyncio.run(
            plugin.handle_tool(
                "group_info_write",
                make_wrapper(event),
                {"action": "set_name", "value": "新群名"},
            )
        )
        self.assertIn('"ok": true', result)
        plugin._call.assert_awaited_once_with(
            event,
            "set_group_name",
            group_id="660402685",
            group_name="新群名",
        )

    def test_write_allowed_for_admins_id(self):
        plugin, _ = make_plugin(
            {"group_info_write": {"set_name": True}},
            admins=["839097298"],
        )
        plugin._call = AsyncMock(return_value=None)
        result = asyncio.run(
            plugin.handle_tool(
                "group_info_write",
                make_wrapper(FakeEvent(role="member")),
                {"action": "set_name", "value": "新群名"},
            )
        )
        self.assertIn('"ok": true', result)

    def test_write_allowed_for_extra_user(self):
        plugin, _ = make_plugin(
            {
                "group_info_write": {"set_name": True},
                "write_allowed_users": ["839097298"],
            }
        )
        plugin._call = AsyncMock(return_value=None)
        result = asyncio.run(
            plugin.handle_tool(
                "group_info_write",
                make_wrapper(FakeEvent(role="member")),
                {"action": "set_name", "value": "新群名"},
            )
        )
        self.assertIn('"ok": true', result)

    def test_write_require_admin_can_be_disabled(self):
        plugin, _ = make_plugin(
            {
                "group_info_write": {"set_name": True},
                "write_require_admin": False,
            }
        )
        plugin._call = AsyncMock(return_value=None)
        result = asyncio.run(
            plugin.handle_tool(
                "group_info_write",
                make_wrapper(FakeEvent(role="member")),
                {"action": "set_name", "value": "新群名"},
            )
        )
        self.assertIn('"ok": true', result)


class GroupReadTests(unittest.TestCase):
    def setUp(self):
        self.plugin, _ = make_plugin()
        self.event = FakeEvent()
        self.wrapper = make_wrapper(self.event)

    def call(self, action: str, **kwargs):
        return asyncio.run(
            self.plugin.handle_tool(
                "group_read",
                self.wrapper,
                {"action": action, **kwargs},
            )
        )

    def test_group_info_formats_time(self):
        self.plugin._call = AsyncMock(
            return_value={
                "group_id": "660402685",
                "group_name": "测试群",
                "create_time": 1700000000,
            }
        )
        result = json.loads(self.call("info"))
        self.assertEqual(result["group_name"], "测试群")
        self.assertNotEqual(result["create_time"], 1700000000)

    def test_group_members_filter_and_paginate(self):
        self.plugin._call = AsyncMock(
            return_value=[
                {"user_id": "1", "nickname": "Alice", "card": "", "role": "owner"},
                {"user_id": "2", "nickname": "Bob", "card": "Alice2", "role": "member"},
                {"user_id": "3", "nickname": "Carol", "card": "", "role": "member"},
            ]
        )
        result = json.loads(self.call("members", keyword="alice", limit=1, offset=0))
        self.assertEqual(result["total"], 2)
        self.assertEqual(len(result["members"]), 1)

    def test_group_messages_are_summarized(self):
        self.plugin._call = AsyncMock(
            return_value={
                "messages": [
                    {
                        "message_id": "100",
                        "time": 1700000000,
                        "sender": {"user_id": "1", "nickname": "Alice"},
                        "message": [{"type": "text", "data": {"text": "你好"}}],
                    },
                    {
                        "message_id": "101",
                        "time": 1700000001,
                        "sender": {"user_id": "2", "nickname": "Bob"},
                        "message": [{"type": "image", "data": {}}],
                    },
                ]
            }
        )
        result = json.loads(self.call("messages", count=10))
        self.assertEqual(result["returned"], 2)
        self.assertEqual(result["messages"][0]["text"], "你好")
        self.assertEqual(result["messages"][1]["text"], "[图片]")

    def test_system_messages_does_not_require_group(self):
        self.plugin._call = AsyncMock(return_value={"join_requests": []})
        self.event.group_id = ""
        result = json.loads(self.call("system_messages"))
        self.assertEqual(result, {"join_requests": []})

    def test_group_id_defaults_to_current_group(self):
        self.plugin._call = AsyncMock(return_value={})
        self.call("info")
        self.plugin._call.assert_awaited_once_with(
            self.event, "get_group_info", group_id="660402685"
        )

    def test_group_files_are_paginated(self):
        self.plugin._call = AsyncMock(
            side_effect=[
                {
                    "files": [
                        {"file_id": "1", "file_name": "a.txt", "size": 10},
                        {"file_id": "2", "file_name": "b.txt", "size": 20},
                        {"file_id": "3", "file_name": "c.txt", "size": 30},
                    ],
                    "folders": [{"folder_id": "f1", "folder_name": "目录"}],
                },
                {"file_count": 3},
            ]
        )
        result = json.loads(self.call("files", limit=2, offset=1))
        self.assertEqual(result["total_files"], 3)
        self.assertEqual([item["file_id"] for item in result["files"]], ["2", "3"])
        self.assertEqual(result["files"][0]["file_size"], 20)


class GroupWriteTests(unittest.TestCase):
    def setUp(self):
        self.plugin, _ = make_plugin(
            {
                "group_info_write": {
                    "set_name": True,
                    "set_portrait": True,
                    "set_remark": True,
                    "send_announcement": True,
                    "delete_announcement": True,
                },
                "group_member_write": {
                    "set_card": True,
                    "set_admin": True,
                    "ban": True,
                    "whole_ban": True,
                    "kick": True,
                },
                "group_file_write": {
                    "upload": True,
                    "delete": True,
                    "move": True,
                },
                "group_message_write": {
                    "set_essence": True,
                    "delete_essence": True,
                    "recall": True,
                    "reaction": True,
                    "mark_read": True,
                    "handle_request": True,
                },
                "group_lifecycle_write": {"leave": True},
            }
        )
        self.plugin._call = AsyncMock(return_value=None)
        self.event = FakeEvent(role="admin")
        self.wrapper = make_wrapper(self.event)

    def call(self, tool_key: str, action: str, **kwargs):
        return asyncio.run(
            self.plugin.handle_tool(
                tool_key,
                self.wrapper,
                {"action": action, **kwargs},
            )
        )

    def test_send_announcement_with_image(self):
        result = self.call(
            "group_info_write",
            "send_announcement",
            value="公告正文",
            image="https://example.com/a.png",
        )
        self.assertIn('"ok": true', result)
        self.plugin._call.assert_awaited_once_with(
            self.event,
            "_send_group_notice",
            group_id="660402685",
            content="公告正文",
            image="https://example.com/a.png",
        )

    def test_delete_announcement(self):
        self.call(
            "group_info_write",
            "delete_announcement",
            notice_id="notice_123",
        )
        self.plugin._call.assert_awaited_once_with(
            self.event,
            "_del_group_notice",
            group_id="660402685",
            notice_id="notice_123",
        )

    def test_group_portrait_rejects_local_path(self):
        result = self.call(
            "group_info_write",
            "set_portrait",
            image="/etc/passwd",
        )
        self.assertIn("只支持", result)
        self.plugin._call.assert_not_awaited()

    def test_set_admin_and_ban(self):
        self.call(
            "group_member_write",
            "set_admin",
            user_id="10001",
            enable=True,
        )
        self.plugin._call.assert_awaited_once_with(
            self.event,
            "set_group_admin",
            group_id="660402685",
            user_id="10001",
            enable=True,
        )
        self.plugin._call.reset_mock()
        self.call(
            "group_member_write",
            "ban",
            user_id="10001",
            duration=0,
        )
        self.plugin._call.assert_awaited_once_with(
            self.event,
            "set_group_ban",
            group_id="660402685",
            user_id="10001",
            duration=0,
        )

    def test_kick_with_reject(self):
        self.call(
            "group_member_write",
            "kick",
            user_id="10001",
            reject_add_request=True,
        )
        self.plugin._call.assert_awaited_once_with(
            self.event,
            "set_group_kick",
            group_id="660402685",
            user_id="10001",
            reject_add_request=True,
        )

    def test_upload_file(self):
        self.call(
            "group_file_write",
            "upload",
            file="https://example.com/a.txt",
            name="a.txt",
            folder_id="folder1",
        )
        self.plugin._call.assert_awaited_once_with(
            self.event,
            "upload_group_file",
            group_id="660402685",
            file="https://example.com/a.txt",
            name="a.txt",
            folder="folder1",
        )

    def test_move_file(self):
        self.call(
            "group_file_write",
            "move",
            file_id="file1",
            folder_id="folder1",
            target_folder_id="folder2",
        )
        self.plugin._call.assert_awaited_once_with(
            self.event,
            "move_group_file",
            group_id="660402685",
            file_id="file1",
            current_parent_directory="folder1",
            target_parent_directory="folder2",
        )

    def test_reaction(self):
        self.call(
            "group_message_write",
            "reaction",
            message_id="100",
            emoji_id="128512",
            set_reaction=False,
        )
        self.plugin._call.assert_awaited_once_with(
            self.event,
            "set_msg_emoji_like",
            message_id=100,
            emoji_id="128512",
            set=False,
        )

    def test_handle_group_request(self):
        self.call(
            "group_message_write",
            "handle_request",
            flag="flag1",
            sub_type="add",
            approve=True,
        )
        self.plugin._call.assert_awaited_once_with(
            self.event,
            "set_group_add_request",
            flag="flag1",
            sub_type="add",
            approve=True,
            reason="",
        )

    def test_leave_group(self):
        self.call(
            "group_lifecycle_write",
            "leave",
            is_dismiss=True,
        )
        self.plugin._call.assert_awaited_once_with(
            self.event,
            "set_group_leave",
            group_id="660402685",
            is_dismiss=True,
        )


class SelfAndUserTests(unittest.TestCase):
    def setUp(self):
        self.plugin, _ = make_plugin(
            {
                "self_write": {
                    "set_profile": True,
                    "set_avatar": True,
                    "set_online_status": True,
                    "set_input_status": True,
                    "set_long_nick": True,
                },
                "user_write": {
                    "set_remark": True,
                    "like": True,
                    "poke": True,
                    "delete_friend": True,
                    "handle_request": True,
                    "handle_doubt_request": True,
                },
            }
        )
        self.event = FakeEvent(role="admin")
        self.wrapper = make_wrapper(self.event)

    def call(self, tool_key: str, action: str, **kwargs):
        return asyncio.run(
            self.plugin.handle_tool(
                tool_key,
                self.wrapper,
                {"action": action, **kwargs},
            )
        )

    def test_set_profile_uses_current_nickname(self):
        self.plugin._call = AsyncMock(
            side_effect=[
                {"user_id": "3809232719", "nickname": "当前昵称"},
                None,
            ]
        )
        self.call("self_write", "set_profile", signature="新签名")
        self.plugin._call.assert_awaited_with(
            self.event,
            "set_qq_profile",
            nickname="当前昵称",
            personal_note="新签名",
        )

    def test_set_online_status_name(self):
        self.plugin._call = AsyncMock(return_value=None)
        self.call("self_write", "set_online_status", status="dnd")
        self.plugin._call.assert_awaited_once_with(
            self.event,
            "set_online_status",
            status=70,
            ext_status=0,
            battery_status=0,
        )

    def test_set_avatar_rejects_local_path(self):
        self.plugin._call = AsyncMock(return_value=None)
        result = self.call("self_write", "set_avatar", file="C:\\avatar.png")
        self.assertIn("只支持", result)
        self.plugin._call.assert_not_awaited()

    def test_like_user(self):
        self.plugin._call = AsyncMock(return_value=None)
        self.call("user_write", "like", user_id="10001", times=5)
        self.plugin._call.assert_awaited_once_with(
            self.event,
            "send_like",
            user_id="10001",
            times=5,
        )

    def test_friend_request(self):
        self.plugin._call = AsyncMock(return_value=None)
        self.call(
            "user_write",
            "handle_request",
            flag="flag1",
            approve=False,
            remark="备注",
        )
        self.plugin._call.assert_awaited_once_with(
            self.event,
            "set_friend_add_request",
            flag="flag1",
            approve=False,
            remark="备注",
        )

    def test_friend_requests_can_be_read(self):
        self.plugin._call = AsyncMock(
            return_value=[
                {
                    "user_id": 10001,
                    "nickname": "申请人",
                    "reason": "想加好友",
                    "flag": "flag1",
                }
            ]
        )
        result = json.loads(self.call("user_read", "requests", limit=5))
        self.assertEqual(result["requests"][0]["flag"], "flag1")
        self.plugin._call.assert_awaited_once_with(
            self.event,
            "get_doubt_friends_add_request",
            count=5,
        )

    def test_handle_doubt_request_requires_approve(self):
        self.plugin._call = AsyncMock(return_value=None)
        result = self.call(
            "user_write",
            "handle_doubt_request",
            flag="flag1",
            approve=False,
        )
        self.assertIn("只支持同意", result)
        self.plugin._call.assert_not_awaited()
        self.call(
            "user_write",
            "handle_doubt_request",
            flag="flag1",
            approve=True,
        )
        self.plugin._call.assert_awaited_once_with(
            self.event,
            "set_doubt_friends_add_request",
            flag="flag1",
            approve=True,
        )


class HelperTests(unittest.TestCase):
    def test_message_text_handles_segments(self):
        text = main.message_text(
            [
                {"type": "at", "data": {"qq": "10001"}},
                {"type": "text", "data": {"text": "你好"}},
                {"type": "image", "data": {}},
            ]
        )
        self.assertEqual(text, "@10001 你好 [图片]")

    def test_remote_file_accepts_url_and_base64(self):
        self.assertEqual(
            main._validate_remote_file("https://example.com/a.png", "file"),
            "https://example.com/a.png",
        )
        self.assertEqual(
            main._validate_remote_file("base64://AAAA", "file"),
            "base64://AAAA",
        )

    def test_remote_file_rejects_path(self):
        with self.assertRaises(main.QQApiError):
            main._validate_remote_file("/tmp/a.png", "file")

    def test_call_preserves_empty_string_and_self_id(self):
        async def run():
            plugin, context = make_plugin()
            bot = FakeBot()
            context.platform = FakePlatform(bot)
            event = FakeEvent(role="admin")
            await plugin._call(event, "set_group_remark", group_id="1", remark="")
            return bot

        bot = asyncio.run(run())
        action, params = bot.calls[0]
        self.assertEqual(action, "set_group_remark")
        self.assertEqual(params["remark"], "")
        self.assertEqual(params["self_id"], 3809232719)

    def test_format_time_handles_invalid(self):
        self.assertEqual(main._format_time("not-a-time"), "not-a-time")
        self.assertEqual(main._format_time(0), 0)

    def test_bool_value_parses_strings(self):
        self.assertTrue(main._bool_value("true"))
        self.assertFalse(main._bool_value("false"))
        self.assertFalse(main._bool_value(None, False))

    def test_summarize_messages_respects_limit(self):
        messages = [
            {
                "message_id": str(index),
                "message": [{"type": "text", "data": {"text": "x" * 10}}],
            }
            for index in range(5)
        ]
        summarized, returned = main._summarize_messages(messages, 25)
        self.assertEqual(returned, 5)
        self.assertLessEqual(sum(len(item["text"]) for item in summarized), 26)

    def test_summarize_profile_removes_private_fields(self):
        result = main._summarize_profile(
            {
                "uin": "10001",
                "nick": "测试用户",
                "longNick": "签名",
                "qqLevel": 30,
                "regTime": "1700000000",
                "phoneNum": "138******00",
                "eMail": "a@example.com",
                "richBuffer": {"0": 1, "1": 2},
            }
        )
        self.assertEqual(result["user_id"], "10001")
        self.assertEqual(result["nickname"], "测试用户")
        self.assertEqual(result["qq_level"], 30)
        self.assertNotIn("phoneNum", result)
        self.assertNotIn("eMail", result)
        self.assertNotIn("richBuffer", result)

    def test_summarize_announcement_decodes_html(self):
        result = main._summarize_announcement(
            {
                "notice_id": "n1",
                "publish_time": 1700000000,
                "message": {
                    "text": "第一行&nbsp;第二行&#10;第三行",
                    "images": [{"id": "img1"}],
                },
            }
        )
        self.assertEqual(result["text"], "第一行 第二行\n第三行")
        self.assertEqual(result["images"], [{"id": "img1"}])

    def test_summarize_essence_uses_text(self):
        result = main._summarize_essence(
            {
                "message_id": "1",
                "content": [{"type": "text", "data": {"text": "精华内容"}}],
            }
        )
        self.assertEqual(result["text"], "精华内容")
        self.assertNotIn("content", result)

    def test_json_result_truncation_is_valid_json(self):
        result = main._json_result({"items": ["x" * 100 for _ in range(100)]}, 500)
        parsed = json.loads(result)
        self.assertTrue(parsed["truncated"])
        self.assertIn("preview", parsed)


if __name__ == "__main__":
    unittest.main()
