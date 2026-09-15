from __future__ import annotations

import base64
import importlib.util
import sys
import time
import unittest
from collections import defaultdict, deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot.api.message_components import Plain

MODULE_PATH = Path(__file__).resolve().parents[1] / "main.py"
spec = importlib.util.spec_from_file_location("netease_music_main", MODULE_PATH)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class FakeEvent:
    def __init__(
        self,
        message_str: str = "/网易云 临时登录",
        sender_id: str = "user-1",
        self_id: str = "bot-1",
        platform_id: str = "platform-1",
        umo: str = "platform-1:GroupMessage:group-1",
    ):
        self.message_str = message_str
        self.message_obj = SimpleNamespace(message_str=message_str)
        self.unified_msg_origin = umo
        self._sender_id = sender_id
        self._self_id = self_id
        self._platform_id = platform_id
        self.is_at_or_wake_command = False
        self.is_wake = False
        self.stopped = False
        self.sent_messages = []

    def get_sender_id(self):
        return self._sender_id

    def get_self_id(self):
        return self._self_id

    def get_platform_id(self):
        return self._platform_id

    def get_sender_name(self):
        return "测试用户"

    async def send(self, message):
        self.sent_messages.append(message)

    def stop_event(self):
        self.stopped = True


class QrImageTests(unittest.TestCase):
    def test_decode_valid_data_url(self):
        encoded = base64.b64encode(b"png-bytes").decode()
        self.assertEqual(
            module.decode_qr_image(f"data:image/png;base64,{encoded}"),
            b"png-bytes",
        )

    def test_reject_invalid_qr_data(self):
        for value in (
            "",
            "not-a-data-url",
            "data:image/png,abc",
            "data:image/png;base64,!!!",
        ):
            with self.assertRaises(module.NeteaseApiError):
                module.decode_qr_image(value)


class ProfileAndPlaylistTests(unittest.TestCase):
    def test_normalize_nested_profile(self):
        uid, nickname = module.normalize_profile(
            {
                "data": {
                    "code": 200,
                    "account": {"id": 42},
                    "profile": {"userId": 42, "nickname": " 测试 用户 "},
                }
            }
        )
        self.assertEqual(uid, "42")
        self.assertEqual(nickname, "测试 用户")

    def test_profile_without_login_is_rejected(self):
        with self.assertRaises(module.NeteaseAuthError):
            module.normalize_profile({"data": {"code": 301, "profile": None}})

    def test_playlist_owner_and_fields_are_normalized(self):
        playlist = module.normalize_playlist(
            {
                "id": 123,
                "name": " 我的歌单 ",
                "trackCount": "9",
                "creator": {"userId": 42},
            },
            "42",
        )
        self.assertEqual(playlist["netease_id"], "123")
        self.assertEqual(playlist["track_count"], 9)
        self.assertTrue(playlist["owned"])

    def test_playlist_display_ids_start_at_one(self):
        text = module.format_playlist_list(
            "用户",
            [
                {"name": "第一张", "track_count": 1, "owned": True},
                {"name": "第二张", "track_count": 2, "owned": False},
            ],
        )
        self.assertIn("1. 第一张", text)
        self.assertIn("2. 第二张", text)
        self.assertNotIn("0. ", text)


class MessageSplitTests(unittest.TestCase):
    def test_short_message_is_unchanged(self):
        self.assertEqual(module.split_message("短消息", 500), ["短消息"])

    def test_long_message_keeps_all_non_newline_text(self):
        source = "\n".join(f"第{i}行" for i in range(300))
        chunks = module.split_message(source, 200)
        self.assertTrue(all(len(chunk) <= 200 for chunk in chunks))
        self.assertEqual("".join(chunks).replace("\n", ""), source.replace("\n", ""))


class AnalysisPromptTests(unittest.TestCase):
    @staticmethod
    def make_track(index: int):
        return {
            "name": f"歌曲{index}",
            "ar": [{"name": "歌手甲" if index % 2 == 0 else "歌手乙"}],
            "al": {"name": f"专辑{index % 3}"},
            "publishTime": 1_577_836_800_000,
            "dt": 180_000,
        }

    def test_analysis_uses_full_statistics_and_limited_sample(self):
        tracks = [self.make_track(index) for index in range(1000)]
        data = module.build_analysis_data(
            {
                "playlist": {
                    "name": "测试歌单",
                    "description": "说明",
                    "tags": ["流行"],
                    "playCount": 10,
                    "subscribedCount": 2,
                }
            },
            tracks,
            track_limit=100,
            max_chars=20000,
        )
        self.assertEqual(data.track_count, 1000)
        self.assertLessEqual(data.included_track_count, 100)
        self.assertIn("歌手甲(500)", data.prompt)
        self.assertIn("歌手乙(500)", data.prompt)
        self.assertIn("歌曲0", data.prompt)
        self.assertIn("歌曲999", data.prompt)

    def test_analysis_respects_small_prompt_budget(self):
        tracks = [self.make_track(index) for index in range(500)]
        data = module.build_analysis_data(
            {"playlist": {"name": "预算测试"}},
            tracks,
            track_limit=500,
            max_chars=4000,
        )
        self.assertLess(len(data.prompt), 4500)
        self.assertLess(data.included_track_count, 500)

    def test_empty_analysis_prompt_uses_default(self):
        self.assertEqual(
            module.normalize_analysis_prompt("   "),
            module.DEFAULT_ANALYSIS_PROMPT,
        )

    def test_custom_analysis_prompt_is_composed_within_budget(self):
        prompt = module.compose_analysis_request(
            "歌单数据" * 2000,
            "重点分析现场感和情绪变化。",
            3000,
        )
        self.assertLessEqual(len(prompt), 3000)
        self.assertIn("【分析要求】", prompt)
        self.assertTrue(prompt.endswith("重点分析现场感和情绪变化。"))


class ApiClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_qr_calls_key_then_create(self):
        client = module.NeteaseMusicClient("http://127.0.0.1:3010")
        qr = base64.b64encode(b"image").decode()
        client._post = AsyncMock(
            side_effect=[
                {"code": 200, "data": {"unikey": "key-1"}},
                {"code": 200, "data": {"qrimg": f"data:image/png;base64,{qr}"}},
            ]
        )
        key, image = await client.create_login_qr()
        self.assertEqual((key, image), ("key-1", b"image"))
        self.assertEqual(client._post.await_args_list[1].args[0], "/login/qr/create")
        self.assertEqual(client._post.await_args_list[1].args[1]["key"], "key-1")

    async def test_qr_check_disables_response_cookie(self):
        client = module.NeteaseMusicClient("http://127.0.0.1:3010")
        client._post = AsyncMock(return_value={"code": 801})
        await client.check_login_qr("key-1")
        payload = client._post.await_args.args[1]
        self.assertEqual(payload["noCookie"], "true")

    async def test_playlist_pagination_deduplicates(self):
        client = module.NeteaseMusicClient("http://127.0.0.1:3010")
        first = [
            {
                "id": index,
                "name": f"歌单{index}",
                "trackCount": index,
                "creator": {"userId": 1},
            }
            for index in range(1, 201)
        ]
        second = [first[-1], {"id": 201, "name": "最后", "creator": {"userId": 2}}]
        client._post = AsyncMock(
            side_effect=[
                {"code": 200, "playlist": first, "more": True},
                {"code": 200, "playlist": second, "more": False},
            ]
        )
        playlists = await client.list_playlists("1", "secret-cookie")
        self.assertEqual(len(playlists), 201)
        self.assertEqual(playlists[-1]["netease_id"], "201")
        second_payload = client._post.await_args_list[1].args[1]
        self.assertEqual(second_payload["offset"], "200")
        self.assertEqual(second_payload["cookie"], "secret-cookie")

    async def test_track_pagination_reads_expected_count(self):
        client = module.NeteaseMusicClient("http://127.0.0.1:3010")
        client._post = AsyncMock(
            side_effect=[
                {"code": 200, "songs": [{"id": i} for i in range(500)]},
                {"code": 200, "songs": [{"id": i} for i in range(500, 620)]},
            ]
        )
        tracks = await client.get_playlist_tracks("99", "cookie", 620)
        self.assertEqual(len(tracks), 620)
        self.assertEqual(client._post.await_args_list[1].args[1]["offset"], "500")
        self.assertEqual(client._post.await_args_list[1].args[1]["limit"], "120")

    async def test_http_session_uses_dummy_cookie_jar(self):
        client = module.NeteaseMusicClient("http://127.0.0.1:3010")
        session = await client._get_session()
        try:
            self.assertIsInstance(session.cookie_jar, module.aiohttp.DummyCookieJar)
        finally:
            await client.close()


class PluginBehaviorTests(unittest.IsolatedAsyncioTestCase):
    def make_plugin(self):
        plugin = object.__new__(module.NeteaseMusicPlugin)
        plugin._pending_logins = {}
        plugin._outgoing_echoes = defaultdict(deque)
        plugin._context_locks = {}
        plugin._storage_lock = __import__("asyncio").Lock()
        plugin._analysis_users = set()
        plugin.context = SimpleNamespace()
        plugin.api = SimpleNamespace(logout=AsyncMock())
        plugin.message_chunk_chars = 1500
        return plugin

    async def test_send_registers_echo_before_platform_send(self):
        plugin = self.make_plugin()
        plugin._persist_output = AsyncMock()
        event = FakeEvent()
        await plugin._send_message(event, [Plain("结果")])
        key = ("platform-1:GroupMessage:group-1", "结果")
        self.assertIn(key, plugin._outgoing_echoes)
        self.assertEqual(len(event.sent_messages), 1)
        plugin._persist_output.assert_awaited_once()

    async def test_output_is_persisted_as_bot_message(self):
        plugin = self.make_plugin()
        history_manager = SimpleNamespace(insert_message_chain=AsyncMock())
        plugin.context = SimpleNamespace(
            get_config=lambda umo: {
                "provider_ltm_settings": {
                    "group_message_history_enable": True,
                    "group_message_history_max_cnt": 321,
                }
            },
            message_history_manager=history_manager,
        )
        event = FakeEvent()
        chain = module.MessageChain([Plain("结果")])
        await plugin._persist_output(event, chain)
        history_manager.insert_message_chain.assert_awaited_once_with(
            platform_id="platform-1",
            user_id="platform-1:GroupMessage:group-1",
            message_chain=chain,
            role="bot",
            sender_id="bot-1",
            sender_name="bot",
            max_messages=321,
        )

    async def test_tracked_own_echo_is_stopped(self):
        plugin = self.make_plugin()
        key = ("platform-1:GroupMessage:group-1", "插件结果")
        plugin._outgoing_echoes[key].append(time.monotonic() + 30)
        event = FakeEvent(message_str="插件结果", sender_id="bot-1", self_id="bot-1")
        await plugin.guard_plugin_output_echo(event)
        self.assertTrue(event.stopped)
        self.assertTrue(event.is_at_or_wake_command)
        self.assertNotIn(key, plugin._outgoing_echoes)

    async def test_tracked_own_echo_with_at_prefix_is_stopped(self):
        plugin = self.make_plugin()
        key = ("platform-1:GroupMessage:group-1", "插件结果")
        plugin._outgoing_echoes[key].append(time.monotonic() + 30)
        event = FakeEvent(
            message_str="@测试用户(user-1) 插件结果",
            sender_id="bot-1",
            self_id="bot-1",
        )
        await plugin.guard_plugin_output_echo(event)
        self.assertTrue(event.stopped)
        self.assertNotIn(key, plugin._outgoing_echoes)

    async def test_untracked_own_message_is_not_stopped(self):
        plugin = self.make_plugin()
        event = FakeEvent(
            message_str="普通机器人消息", sender_id="bot-1", self_id="bot-1"
        )
        await plugin.guard_plugin_output_echo(event)
        self.assertFalse(event.stopped)

    async def test_other_user_cannot_consume_echo_guard(self):
        plugin = self.make_plugin()
        key = ("platform-1:GroupMessage:group-1", "插件结果")
        plugin._outgoing_echoes[key].append(time.monotonic() + 30)
        event = FakeEvent(message_str="插件结果", sender_id="user-1", self_id="bot-1")
        await plugin.guard_plugin_output_echo(event)
        self.assertFalse(event.stopped)
        self.assertIn(key, plugin._outgoing_echoes)

    async def test_yes_confirmation_saves_binding_only_then(self):
        plugin = self.make_plugin()
        pending = module.PendingLogin(
            identity="platform-1:user-1",
            event=FakeEvent(),
            umo="platform-1:GroupMessage:group-1",
            platform_id="platform-1",
            user_id="user-1",
            display_name="测试用户",
            login_key="key",
            created_at=time.monotonic(),
            cookie="secret",
            uid="42",
            nickname="账号名",
            confirmation_deadline=time.monotonic() + 60,
        )
        plugin._pending_logins[pending.identity] = pending
        plugin._save_binding = AsyncMock()
        plugin._discard_pending = AsyncMock()
        plugin._reply_command = AsyncMock()
        event = FakeEvent(message_str="/网易云 临时登录确认 yes")

        await plugin.confirm_temporary_login(event, "yes")

        self.assertTrue(event.stopped)
        plugin._save_binding.assert_awaited_once()
        saved = plugin._save_binding.await_args.args[1]
        self.assertEqual(saved["cookie"], "secret")
        self.assertEqual(saved["uid"], "42")
        plugin._discard_pending.assert_awaited_once_with(
            "platform-1:user-1", logout=False
        )

    async def test_no_confirmation_never_saves_binding(self):
        plugin = self.make_plugin()
        pending = module.PendingLogin(
            identity="platform-1:user-1",
            event=FakeEvent(),
            umo="platform-1:GroupMessage:group-1",
            platform_id="platform-1",
            user_id="user-1",
            display_name="测试用户",
            login_key="key",
            created_at=time.monotonic(),
            cookie="secret",
            uid="42",
            nickname="账号名",
            confirmation_deadline=time.monotonic() + 60,
        )
        plugin._pending_logins[pending.identity] = pending
        plugin._save_binding = AsyncMock()
        plugin._discard_pending = AsyncMock()
        plugin._reply_command = AsyncMock()
        event = FakeEvent(message_str="/网易云 临时登录确认 no")

        await plugin.confirm_temporary_login(event, "no")

        plugin._save_binding.assert_not_awaited()
        plugin._discard_pending.assert_awaited_once_with(
            "platform-1:user-1", logout=True
        )

    async def test_qr_waiting_scanned_then_expired(self):
        plugin = self.make_plugin()
        plugin.qr_poll_interval = 1
        plugin.qr_login_timeout = 300
        pending = module.PendingLogin(
            identity="platform-1:user-1",
            event=FakeEvent(),
            umo="platform-1:GroupMessage:group-1",
            platform_id="platform-1",
            user_id="user-1",
            display_name="测试用户",
            login_key="key",
            created_at=time.monotonic(),
        )
        plugin._pending_logins[pending.identity] = pending
        plugin.api = SimpleNamespace(
            check_login_qr=AsyncMock(
                side_effect=[{"code": 801}, {"code": 802}, {"code": 800}]
            )
        )
        plugin._expire_qr_login = AsyncMock()
        with patch.object(module.asyncio, "sleep", new=AsyncMock()):
            await plugin._poll_login(pending.identity, "key")
        self.assertEqual(plugin.api.check_login_qr.await_count, 3)
        plugin._expire_qr_login.assert_awaited_once_with("platform-1:user-1", "key")

    async def test_qr_success_keeps_cookie_in_memory_until_confirmation(self):
        plugin = self.make_plugin()
        plugin.qr_poll_interval = 1
        plugin.qr_login_timeout = 300
        plugin.confirmation_timeout = 60
        pending = module.PendingLogin(
            identity="platform-1:user-1",
            event=FakeEvent(),
            umo="platform-1:GroupMessage:group-1",
            platform_id="platform-1",
            user_id="user-1",
            display_name="测试用户",
            login_key="key",
            created_at=time.monotonic(),
        )
        plugin._pending_logins[pending.identity] = pending
        plugin.api = SimpleNamespace(
            check_login_qr=AsyncMock(
                side_effect=[{"code": 802}, {"code": 803, "cookie": "secret"}]
            ),
            get_profile=AsyncMock(return_value=("42", "账号名")),
            logout=AsyncMock(),
        )
        plugin._send_message = AsyncMock()
        plugin._append_assistant_context = AsyncMock()
        plugin._save_binding = AsyncMock()
        expiry_marker = SimpleNamespace(done=lambda: False, cancel=lambda: None)

        def capture_expiry_task(coroutine):
            coroutine.close()
            return expiry_marker

        with (
            patch.object(module.asyncio, "sleep", new=AsyncMock()),
            patch.object(
                module.asyncio,
                "create_task",
                side_effect=capture_expiry_task,
            ),
        ):
            await plugin._poll_login(pending.identity, "key")
        self.assertEqual(pending.cookie, "secret")
        self.assertEqual(pending.uid, "42")
        self.assertEqual(pending.nickname, "账号名")
        self.assertGreater(pending.confirmation_deadline, time.monotonic())
        self.assertIs(pending.task, expiry_marker)
        plugin._send_message.assert_awaited_once()
        plugin._save_binding.assert_not_awaited()

    async def test_playlist_handler_updates_snapshot_and_returns_all_items(self):
        plugin = self.make_plugin()
        plugin.enabled = True
        plugin._get_binding = AsyncMock(return_value={"cookie": "secret"})
        plugin._save_binding = AsyncMock()
        plugin._reply_command = AsyncMock()
        plugin.api = SimpleNamespace(
            get_profile=AsyncMock(return_value=("42", "账号名")),
            list_playlists=AsyncMock(
                return_value=[
                    {
                        "netease_id": "100",
                        "name": "第一张",
                        "track_count": 10,
                        "owned": True,
                    },
                    {
                        "netease_id": "200",
                        "name": "第二张",
                        "track_count": 20,
                        "owned": False,
                    },
                ]
            ),
        )
        event = FakeEvent(message_str="/网易云 歌单list")

        await plugin.list_user_playlists(event)

        self.assertTrue(event.stopped)
        plugin._save_binding.assert_awaited_once()
        saved = plugin._save_binding.await_args.args[1]
        self.assertEqual(
            [item["netease_id"] for item in saved["playlists"]], ["100", "200"]
        )
        response = plugin._reply_command.await_args.args[1]
        self.assertIn("1. 第一张", response)
        self.assertIn("2. 第二张", response)

    async def test_analysis_handler_uses_current_provider_and_snapshot(self):
        plugin = self.make_plugin()
        plugin.enabled = True
        plugin.analysis_track_limit = 100
        plugin.analysis_prompt_max_chars = 10000
        plugin.analysis_prompt = "重点分析现场感、曲风跨度和情绪变化。"
        plugin._get_binding = AsyncMock(
            return_value={
                "cookie": "secret",
                "playlists": [
                    {
                        "netease_id": "100",
                        "name": "目标歌单",
                        "track_count": 1,
                    }
                ],
            }
        )
        plugin.api = SimpleNamespace(
            get_profile=AsyncMock(return_value=("42", "账号名")),
            get_playlist_detail=AsyncMock(
                return_value={"playlist": {"name": "目标歌单", "trackCount": 1}}
            ),
            get_playlist_tracks=AsyncMock(
                return_value=[
                    {
                        "name": "目标歌曲",
                        "ar": [{"name": "目标歌手"}],
                        "al": {"name": "目标专辑"},
                        "dt": 180000,
                    }
                ]
            ),
        )
        plugin.context = SimpleNamespace(
            get_current_chat_provider_id=AsyncMock(return_value="current-provider"),
            llm_generate=AsyncMock(
                return_value=SimpleNamespace(completion_text="分析结果")
            ),
        )
        plugin._send_text = AsyncMock()
        plugin._record_command_context = AsyncMock()
        event = FakeEvent(message_str="/网易云 歌单分析 1")

        await plugin.analyze_playlist(event, "1")

        self.assertTrue(event.stopped)
        plugin.context.get_current_chat_provider_id.assert_awaited_once_with(
            umo=event.unified_msg_origin
        )
        llm_call = plugin.context.llm_generate.await_args.kwargs
        self.assertEqual(llm_call["chat_provider_id"], "current-provider")
        self.assertIn("目标歌曲", llm_call["prompt"])
        self.assertIn("【分析要求】", llm_call["prompt"])
        self.assertIn(plugin.analysis_prompt, llm_call["prompt"])
        self.assertLessEqual(len(llm_call["prompt"]), plugin.analysis_prompt_max_chars)
        self.assertEqual(plugin._send_text.await_count, 2)
        self.assertEqual(
            plugin._send_text.await_args_list[-1].args,
            (event, "分析结果"),
        )
        plugin._record_command_context.assert_awaited_once()
        self.assertNotIn("platform-1:user-1", plugin._analysis_users)

    async def test_identity_isolated_by_platform_and_user(self):
        plugin = self.make_plugin()
        first = FakeEvent(sender_id="user-1", platform_id="platform-a")
        second = FakeEvent(sender_id="user-1", platform_id="platform-b")
        third = FakeEvent(sender_id="user-2", platform_id="platform-a")
        self.assertEqual(plugin._identity(first), "platform-a:user-1")
        self.assertNotEqual(plugin._identity(first), plugin._identity(second))
        self.assertNotEqual(plugin._identity(first), plugin._identity(third))


if __name__ == "__main__":
    unittest.main()
