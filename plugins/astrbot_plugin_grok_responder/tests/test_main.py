from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

from astrbot.api.message_components import At, Plain
from astrbot.api.platform import MessageType
from astrbot.api.provider import ProviderRequest

MODULE_PATH = Path(__file__).resolve().parents[1] / "main.py"
spec = importlib.util.spec_from_file_location("grok_responder_main", MODULE_PATH)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def make_message_obj(message_str: str, group_id: str = "g1", message=None):
    return SimpleNamespace(
        message_str=message_str,
        group_id=group_id,
        self_id="bot1",
        sender=SimpleNamespace(user_id="u1", nickname="测试用户"),
        message=message or [],
    )


class FakeEvent:
    def __init__(self, message_obj):
        self.message_obj = message_obj
        self.message_str = message_obj.message_str
        self.is_at_or_wake_command = False
        self.is_wake = False
        self.extras = {}

    def get_message_type(self) -> MessageType:
        return MessageType.GROUP_MESSAGE

    def set_extra(self, key, value):
        self.extras[key] = value

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)


class ContainsAtGrokTests(unittest.TestCase):
    def test_plain_text_any_case(self):
        self.assertTrue(module.contains_at_grok("大家好 @grok 出来一下"))
        self.assertTrue(module.contains_at_grok("@Grok 你好"))
        self.assertTrue(module.contains_at_grok("HELLO @GROK!"))
        self.assertTrue(module.contains_at_grok("@gRoK"))
        self.assertTrue(module.contains_at_grok("就一个@GRok也触发"))

    def test_requires_at_symbol(self):
        self.assertFalse(module.contains_at_grok("grok 你好"))
        self.assertFalse(module.contains_at_grok("今天 grok 了吗"))
        self.assertFalse(module.contains_at_grok(""))

    def test_other_mentions_not_triggered(self):
        self.assertFalse(module.contains_at_grok("@机器人 在吗"))
        self.assertFalse(module.contains_at_grok("@GroBot 注册的是别的名字"))

    def test_at_grok_substring_trigger(self):
        self.assertTrue(module.contains_at_grok("@GrokBot 注册的是别的名字"))

    def test_at_segment_name_fallback(self):
        self.assertTrue(module.contains_at_grok("有人艾特我了", ["Grok 大师"]))
        self.assertFalse(module.contains_at_grok("有人艾特我了", ["小明"]))


class StripAtGrokTests(unittest.TestCase):
    def test_removes_alias_anywhere_and_normalizes_spaces(self):
        self.assertEqual(
            module.strip_at_grok("  请问 @GROK   is that true？  "),
            "请问 is that true？",
        )
        self.assertEqual(module.strip_at_grok("前@grok中@grok后"), "前 中 后")

    def test_alias_only_becomes_empty(self):
        self.assertEqual(module.strip_at_grok(" @grok "), "")


class CooldownTests(unittest.TestCase):
    def test_cooldown_blocks_second_trigger(self):
        plugin = object.__new__(module.GrokResponder)
        plugin.cooldown_seconds = 15
        plugin._last_trigger_at = {}
        self.assertTrue(plugin._passes_cooldown("g1"))
        self.assertFalse(plugin._passes_cooldown("g1"))
        self.assertTrue(plugin._passes_cooldown("g2"))

    def test_cooldown_disabled_when_zero(self):
        plugin = object.__new__(module.GrokResponder)
        plugin.cooldown_seconds = 0
        plugin._last_trigger_at = {}
        self.assertTrue(plugin._passes_cooldown("g1"))
        self.assertTrue(plugin._passes_cooldown("g1"))


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    def _make_plugin(self, enabled: bool = True, cooldown: int = 0):
        plugin = object.__new__(module.GrokResponder)
        plugin.enabled = enabled
        plugin.cooldown_seconds = cooldown
        plugin._last_trigger_at = {}
        return plugin

    async def test_trigger_marks_wake_and_cleans_model_input(self):
        plugin = self._make_plugin()
        message_obj = make_message_obj("请问 @grok 今天天气怎么样")
        event = FakeEvent(message_obj)

        await plugin.on_group_message(event)

        self.assertTrue(event.is_at_or_wake_command)
        self.assertTrue(event.is_wake)
        self.assertEqual(event.message_str, "请问 今天天气怎么样")
        self.assertTrue(event.get_extra(module._GROK_TRIGGERED_EXTRA))
        self.assertEqual(message_obj.message_str, "请问 @grok 今天天气怎么样")

    async def test_alias_only_gets_non_empty_model_prompt(self):
        plugin = self._make_plugin()
        event = FakeEvent(make_message_obj("@Grok"))

        await plugin.on_group_message(event)

        self.assertEqual(event.message_str, module._WAKE_ONLY_PROMPT)

    async def test_request_hook_adds_temporary_alias_hint_once_per_call(self):
        plugin = self._make_plugin()
        event = FakeEvent(make_message_obj("@grok 测试"))
        req = ProviderRequest(prompt="测试")
        await plugin.on_group_message(event)

        await plugin.add_alias_hint(event, req)

        self.assertEqual(len(req.extra_user_content_parts), 1)
        part = req.extra_user_content_parts[0]
        self.assertIn("当前助手的唤醒别名", part.text)
        self.assertTrue(part.model_dump_for_context()["_no_save"])

    async def test_request_hook_ignores_normal_requests(self):
        plugin = self._make_plugin()
        event = FakeEvent(make_message_obj("普通消息"))
        req = ProviderRequest(prompt="普通消息")

        await plugin.add_alias_hint(event, req)

        self.assertEqual(req.extra_user_content_parts, [])

    async def test_non_trigger_message_is_ignored(self):
        plugin = self._make_plugin()
        event = FakeEvent(make_message_obj("普通闲聊，没有触发词"))
        await plugin.on_group_message(event)
        self.assertFalse(event.is_at_or_wake_command)
        self.assertFalse(event.is_wake)

    async def test_grok_without_at_is_ignored(self):
        plugin = self._make_plugin()
        event = FakeEvent(make_message_obj("grok 这个模型不错"))
        await plugin.on_group_message(event)
        self.assertFalse(event.is_at_or_wake_command)

    async def test_at_segment_trigger(self):
        plugin = self._make_plugin()
        message_obj = make_message_obj(
            "你好呀",
            message=[At(qq="12345", name="Grok 大师"), Plain(text="你好呀")],
        )
        event = FakeEvent(message_obj)
        await plugin.on_group_message(event)
        self.assertTrue(event.is_at_or_wake_command)
        self.assertEqual(event.message_str, "你好呀")

    async def test_own_message_is_ignored(self):
        plugin = self._make_plugin()
        message_obj = make_message_obj("@grok 自己发的")
        message_obj.sender = SimpleNamespace(user_id="bot1", nickname="机器人")
        event = FakeEvent(message_obj)
        await plugin.on_group_message(event)
        self.assertFalse(event.is_at_or_wake_command)

    async def test_cooldown_skips_second_trigger(self):
        plugin = self._make_plugin(cooldown=15)
        event1 = FakeEvent(make_message_obj("@grok 第一条", group_id="g1"))
        event2 = FakeEvent(make_message_obj("@grok 第二条", group_id="g1"))
        await plugin.on_group_message(event1)
        await plugin.on_group_message(event2)
        self.assertTrue(event1.is_at_or_wake_command)
        self.assertFalse(event2.is_at_or_wake_command)
        self.assertEqual(event2.message_str, "@grok 第二条")

    async def test_disabled_plugin_does_nothing(self):
        plugin = self._make_plugin(enabled=False)
        event = FakeEvent(make_message_obj("@grok 在吗"))
        await plugin.on_group_message(event)
        self.assertFalse(event.is_at_or_wake_command)


if __name__ == "__main__":
    unittest.main()
