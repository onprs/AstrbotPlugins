from __future__ import annotations

import asyncio
import importlib.util
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from collections import defaultdict, deque
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from PIL import Image as PillowImage

from astrbot.api.message_components import Image, Plain
from astrbot.api.provider import ProviderRequest
from astrbot.core.agent.tool import FunctionTool, ToolSet

MODULE_PATH = Path(__file__).resolve().parents[1] / "main.py"
spec = importlib.util.spec_from_file_location("sticker_plugin_main", MODULE_PATH)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def make_tool(name: str) -> FunctionTool:
    return FunctionTool(
        name=name,
        description=name,
        parameters={"type": "object", "properties": {}},
    )


def make_image_bytes(
    image_format: str = "PNG", size: tuple[int, int] = (32, 32)
) -> bytes:
    buffer = BytesIO()
    PillowImage.new("RGB", size, "white").save(buffer, format=image_format)
    return buffer.getvalue()


class FakeContext:
    def __init__(self, send_result: bool = True) -> None:
        self.send_result = send_result
        self.sent: list[tuple[str, object]] = []

    async def send_message(self, origin: str, chain: object) -> bool:
        self.sent.append((origin, chain))
        return self.send_result


class FakeEvent:
    def __init__(
        self,
        origin: str = "aiocqhttp:GroupMessage:123",
        platform_name: str = module.SUPPORTED_PLATFORM,
        private: bool = False,
    ) -> None:
        self.unified_msg_origin = origin
        self.platform_name = platform_name
        self.private = private
        self.extras: dict[str, object] = {}
        self.result = None
        self.messages: list[object] = []
        self.message_obj = SimpleNamespace(raw_message={"message": []})
        self.sender_id = "10001"
        self.self_id = "20002"
        self.stopped = False
        self.is_at_or_wake_command = False
        self.is_wake = False

    def get_platform_name(self) -> str:
        return self.platform_name

    def is_private_chat(self) -> bool:
        return self.private

    def set_extra(self, key: str, value: object) -> None:
        self.extras[key] = value

    def get_extra(self, key: str, default: object = None) -> object:
        return self.extras.get(key, default)

    def get_result(self):
        return self.result

    def get_messages(self) -> list[object]:
        return self.messages

    def get_sender_id(self) -> str:
        return self.sender_id

    def get_self_id(self) -> str:
        return self.self_id

    def stop_event(self) -> None:
        self.stopped = True


class FakeUpload:
    def __init__(self, filename: str, data: bytes) -> None:
        self.filename = filename
        self.data = data

    async def save(self, path: Path) -> None:
        path.write_bytes(self.data)


class FakeUploadRequest:
    def __init__(self, upload: FakeUpload) -> None:
        self.username = "admin"
        self.upload = upload

    async def files(self) -> dict[str, FakeUpload]:
        return {"file": self.upload}


class StickerPluginTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)
        self.plugin = object.__new__(module.StickerPlugin)
        self.plugin.plugin_config = {}
        self.plugin.store = module.StickerStore(self.base)
        self.plugin.cooldown = {}
        self.plugin._send_locks = {}
        self.plugin._outgoing_stickers = defaultdict(deque)
        self.plugin.context = FakeContext()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    async def add_sticker(
        self,
        *,
        md5: str | None = None,
        description: str = "开心庆祝时使用",
    ) -> dict:
        image_data = make_image_bytes()
        image_md5 = md5 or module._md5_bytes(image_data)
        image_path = self.plugin.store.images_dir / f"{image_md5}.png"
        image_path.write_bytes(image_data)
        return await self.plugin.store.add(
            {
                "md5": image_md5,
                "description": description,
                "local_path": str(image_path.resolve()),
                "pending_path": "",
                "use_count": 0,
            }
        )

    async def test_store_serializes_concurrent_changes_and_never_reuses_ids(self):
        added = await asyncio.gather(
            *(self.plugin.store.add({"md5": f"{index:032x}"}) for index in range(20))
        )
        self.assertEqual(sorted(item["local_id"] for item in added), list(range(1, 21)))

        await asyncio.gather(*(self.plugin.store.increment_use(1) for _ in range(30)))
        await self.plugin.store.remove(20)
        replacement = await self.plugin.store.add({"md5": "f" * 32})

        items = await self.plugin.store.load()
        first = next(item for item in items if item["local_id"] == 1)
        self.assertEqual(first["use_count"], 30)
        self.assertEqual(replacement["local_id"], 21)

    async def test_store_rejects_duplicate_capacity_and_corrupt_catalog(self):
        await self.plugin.store.add({"md5": "a" * 32})
        with self.assertRaises(module.DuplicateStickerError):
            await self.plugin.store.add({"md5": "a" * 32})
        with self.assertRaises(module.StickerCapacityError):
            await self.plugin.store.add({"md5": "b" * 32}, max_items=1)

        self.plugin.store.file.write_text("{broken", encoding="utf-8")
        with self.assertRaises(module.StickerStoreError):
            await self.plugin.store.load()
        self.assertEqual(
            self.plugin.store.file.read_text(encoding="utf-8"),
            "{broken",
        )

    async def test_favorite_metadata_update_preserves_description(self):
        item = await self.plugin.store.add({"md5": "a" * 32, "description": "原备注"})
        await self.plugin.store.update_description(item["local_id"], "新备注")
        updated = await self.plugin.store.update_favorite_metadata(
            item["local_id"],
            favorite_ok=True,
            favorite_error="",
            res_id="resource-id",
            emoji_id=0,
            favorite_url="https://example.test/sticker.png",
        )
        self.assertEqual(updated["description"], "新备注")
        self.assertEqual(updated["emoji_id"], "0")

    async def test_upload_keeps_local_copy_and_cleans_pending_when_favorite_fails(
        self,
    ):
        image_data = make_image_bytes()
        upload_request = FakeUploadRequest(FakeUpload("../unsafe-name.png", image_data))
        self.plugin._napcat_add_custom_face = AsyncMock(
            side_effect=RuntimeError("NapCat unavailable")
        )

        with (
            patch.object(module, "request", upload_request),
            patch.object(module, "json_response", side_effect=lambda payload: payload),
        ):
            response = await self.plugin.api_upload()

        self.assertFalse(response["fav_ok"])
        self.assertIn("NapCat unavailable", response["fav_error"])
        self.assertEqual(response["sticker"]["local_id"], 1)
        self.assertEqual(response["sticker"]["file_name"], "unsafe-name.png")
        self.assertNotIn("local_path", response["sticker"])
        self.assertNotIn("pending_path", response["sticker"])
        self.assertNotIn("md5", response["sticker"])

        [stored] = await self.plugin.store.load()
        self.assertEqual(stored["pending_path"], "")
        self.assertTrue(Path(stored["local_path"]).is_file())
        self.assertEqual(list(self.plugin.store.pending_dir.iterdir()), [])

    def test_image_validation_checks_content_format_and_pixels(self):
        png = make_image_bytes()
        self.assertIsNone(self.plugin._check_allowed("valid.png", png))
        self.assertIn("后缀不一致", self.plugin._check_allowed("wrong.jpg", png))
        self.assertIn(
            "无法识别", self.plugin._check_allowed("broken.png", b"not-image")
        )

        self.plugin.plugin_config["max_image_pixels"] = 1_000_000
        oversized = make_image_bytes(size=(1001, 1000))
        self.assertIn("像素过大", self.plugin._check_allowed("large.png", oversized))

    def test_sticker_component_preserves_onebot_sub_type(self):
        component = module.StickerImage(file="/tmp/sticker.png")
        self.assertEqual(
            component.toDict(),
            {
                "type": "image",
                "data": {"file": "/tmp/sticker.png", "sub_type": 1},
            },
        )

    async def test_request_injects_full_catalog_once(self):
        description = "角色来源说明；适用情绪：收到夸奖后害羞回应"
        await self.add_sticker(description=description)
        request = ProviderRequest(system_prompt="基础提示")
        request.func_tool = ToolSet([make_tool(module.STICKER_TOOL_NAME)])
        event = FakeEvent()

        await self.plugin.on_llm_request(event, request)
        await self.plugin.on_llm_request(event, request)

        self.assertIsNotNone(request.func_tool.get_tool(module.STICKER_TOOL_NAME))
        self.assertIn(description, request.system_prompt)
        self.assertIn("主动参与群聊和被 @ 回复时均可", request.system_prompt)
        self.assertIn("assistant 消息本身必须完全不含文字", request.system_prompt)
        self.assertIn(module.STICKER_SENT_ACK, request.system_prompt)
        self.assertEqual(request.system_prompt.count("【QQ 表情包工具】"), 1)

    async def test_request_hides_tool_outside_supported_scope(self):
        await self.add_sticker()
        for event in (
            FakeEvent(platform_name="webchat"),
            FakeEvent(private=True),
        ):
            request = ProviderRequest(system_prompt="基础提示")
            request.func_tool = ToolSet(
                [make_tool(module.STICKER_TOOL_NAME), make_tool("other_tool")]
            )
            await self.plugin.on_llm_request(event, request)
            self.assertIsNone(request.func_tool.get_tool(module.STICKER_TOOL_NAME))
            self.assertIsNotNone(request.func_tool.get_tool("other_tool"))
            self.assertEqual(request.system_prompt, "基础提示")

        empty_plugin = object.__new__(module.StickerPlugin)
        empty_plugin.plugin_config = {}
        empty_plugin.store = module.StickerStore(self.base / "empty")
        empty_request = ProviderRequest(system_prompt="基础提示")
        empty_request.func_tool = ToolSet([make_tool(module.STICKER_TOOL_NAME)])
        await empty_plugin.on_llm_request(FakeEvent(), empty_request)
        self.assertIsNone(empty_request.func_tool.get_tool(module.STICKER_TOOL_NAME))

    async def test_send_uses_session_route_cooldown_and_event_local_only_flag(self):
        item = await self.add_sticker()
        event = FakeEvent()

        result = await self.plugin.send_sticker(event, item["local_id"], True)

        self.assertIn("[STICKER_ONLY]", result)
        self.assertIn(module.STICKER_SENT_ACK, result)
        self.assertEqual(len(self.plugin.context.sent), 1)
        origin, chain = self.plugin.context.sent[0]
        self.assertEqual(origin, event.unified_msg_origin)
        self.assertEqual(chain.chain[0].sub_type, 1)
        self.assertTrue(chain.chain[0].file.startswith("base64://"))
        self.assertIsNotNone(event.get_extra(module.STICKER_ONLY_EVENT_KEY))

        event.result = SimpleNamespace(chain=[Plain("不应发送")])
        await self.plugin.on_decorating_result(event)
        self.assertEqual(event.result.chain, [])

        event.result = SimpleNamespace(chain=[Plain("后续文字也不应发送")])
        await self.plugin.on_decorating_result(event)
        self.assertEqual(event.result.chain, [])

        concurrent_event = FakeEvent(origin=event.unified_msg_origin)
        concurrent_event.result = SimpleNamespace(chain=[Plain("应保留")])
        await self.plugin.on_decorating_result(concurrent_event)
        self.assertEqual(concurrent_event.result.chain[0].text, "应保留")

        cooldown_result = await self.plugin.send_sticker(event, item["local_id"], False)
        self.assertIn("冷却中", cooldown_result)
        self.assertEqual(len(self.plugin.context.sent), 1)

        other_event = FakeEvent(origin="aiocqhttp:GroupMessage:456")
        await self.plugin.send_sticker(other_event, item["local_id"], False)
        self.assertEqual(len(self.plugin.context.sent), 2)
        stored = (await self.plugin.store.load())[0]
        self.assertEqual(stored["use_count"], 2)

    async def test_send_failure_rolls_back_echo_state_and_usage(self):
        item = await self.add_sticker()
        self.plugin.context = FakeContext(send_result=False)
        event = FakeEvent()

        result = await self.plugin.send_sticker(event, item["local_id"], True)

        self.assertIn("STICKER_ERROR", result)
        self.assertIn("不要再次调用", result)
        self.assertNotIn(event.unified_msg_origin, self.plugin.cooldown)
        self.assertNotIn(event.unified_msg_origin, self.plugin._outgoing_stickers)
        stored = (await self.plugin.store.load())[0]
        self.assertEqual(stored["use_count"], 0)
        self.assertIsNone(event.get_extra(module.STICKER_ONLY_EVENT_KEY))

    async def test_send_rejects_non_integral_sticker_id(self):
        await self.add_sticker()
        result = await self.plugin.send_sticker(FakeEvent(), 1.5, False)
        self.assertIn("编号必须是整数", result)
        self.assertIn("不要再次调用", result)
        self.assertEqual(self.plugin.context.sent, [])

    async def test_send_rejects_paths_outside_plugin_data(self):
        outside = self.base.parent / "outside.png"
        outside.write_bytes(make_image_bytes())
        try:
            item = await self.plugin.store.add(
                {
                    "md5": "a" * 32,
                    "description": "测试",
                    "local_path": str(outside.resolve()),
                }
            )
            result = await self.plugin.send_sticker(FakeEvent(), item["local_id"])
            self.assertIn("文件缺失", result)
            self.assertEqual(self.plugin.context.sent, [])
        finally:
            outside.unlink(missing_ok=True)

    async def test_echo_guard_consumes_matching_self_message(self):
        md5 = "a" * 32
        event = FakeEvent()
        event.sender_id = event.self_id
        event.messages = [Image(file=f"{md5}.image")]
        event.message_obj.raw_message = {
            "message": [{"type": "image", "data": {"file": f"{md5}.image"}}]
        }
        self.plugin._register_outgoing_sticker(event.unified_msg_origin, md5)

        await self.plugin.guard_sticker_output_echo(event)

        self.assertTrue(event.stopped)
        self.assertTrue(event.is_wake)
        self.assertNotIn(event.unified_msg_origin, self.plugin._outgoing_stickers)


if __name__ == "__main__":
    unittest.main()
