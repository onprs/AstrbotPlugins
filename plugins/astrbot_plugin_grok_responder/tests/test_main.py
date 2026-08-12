from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

from astrbot.api.message_components import At, Plain
from astrbot.api.platform import MessageType

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

    def get_message_type(self) -> MessageType:
        return MessageType.GROUP_MESSAGE


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
        self.assertFalse(module.contains_at_grok("完全没有内容"))

    def test_other_mentions_not_triggered(self):
        self.assertFalse(module.contains_at_grok("@机器人 在吗"))
        self.assertFalse(module.contains_at_grok("@GroBot 注册的是别的名字"))

    def test_at_grok_substring_trigger(self):
        # 子串语义：@GrokBot 也包含 @Grok，会触发
        self.assertTrue(module.contains_at_grok("@GrokBot 注册的是别的名字"))

    def test_at_segment_name_fallback(self):
        # At 段昵称含 grok，但 message_str 中未包含（兜底路径）
        self.assertTrue(module.contains_at_grok("有人艾特我了", ["Grok 大师", "小明"]))
        self.assertFalse(module.contains_at_grok("有人艾特我了", ["小明", "小红"]))


class CooldownTests(unittest.TestCase):
    def test_cooldown_blocks_second_trigger(self):
        plugin = object.__new__(module.GrokResponder)
        plugin.cooldown_seconds = 15
        plugin._last_trigger_at = {}
        self.assertTrue(plugin._passes_cooldown("g1"))
        self.assertFalse(plugin._passes_cooldown("g1"))
        # 不同群互不影响
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

    async def test_trigger_marks_wake_and_yields_nothing(self):
        plugin = self._make_plugin()
        event = FakeEvent(make_message_obj("请问 @grok 今天天气怎么样"))

        await plugin.on_group_message(event)
        # 只做触发：不产生任何结果、不发送消息
        # 标记唤醒，交由主流程处理
        self.assertTrue(event.is_at_or_wake_command)
        self.assertTrue(event.is_wake)

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

    async def test_disabled_plugin_does_nothing(self):
        plugin = self._make_plugin(enabled=False)
        event = FakeEvent(make_message_obj("@grok 在吗"))
        await plugin.on_group_message(event)
        self.assertFalse(event.is_at_or_wake_command)


if __name__ == "__main__":
    unittest.main()
