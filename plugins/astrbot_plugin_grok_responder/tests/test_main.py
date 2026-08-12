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


class FakeContext:
    """最小化 context 桩：记录 llm_generate 调用并返回固定回复。"""

    def __init__(self, reply: str = "🪐 我在，有什么可以帮你？"):
        self.reply = reply
        self.provider_id = "fake-provider"
        self.generate_calls: list[dict] = []
        self.fail_generate = False
        self.persona_prompt: str | None = None
        self.prompt_prefix: str = ""

        class _ConfigMgr:
            def __init__(self, owner):
                self.owner = owner

            def get_conf(self, umo: str):
                return {
                    "provider_settings": {
                        "prompt_prefix": self.owner.prompt_prefix,
                        "default_personality": None,
                    }
                }

        class _ConvMgr:
            async def get_curr_conversation_id(self, umo: str) -> str | None:
                return None

            async def get_conversation(self, umo: str, cid: str):
                return None

        class _PersonaMgr:
            def __init__(self, owner):
                self.owner = owner

            async def resolve_selected_persona(self, **kwargs):
                if self.owner.persona_prompt:
                    persona = {"prompt": self.owner.persona_prompt, "name": "p1"}
                    return ("p1", persona, None, False)
                return (None, None, None, False)

        self.astrbot_config_mgr = _ConfigMgr(self)
        self.conversation_manager = _ConvMgr()
        self.persona_manager = _PersonaMgr(self)

    async def get_current_chat_provider_id(self, umo: str) -> str:
        self.umo = umo
        return self.provider_id

    async def llm_generate(self, **kwargs):
        self.generate_calls.append(kwargs)
        if self.fail_generate:
            raise RuntimeError("mock failure")
        return SimpleNamespace(completion_text=self.reply)


class FakeEvent:
    def __init__(self, message_obj):
        self.message_obj = message_obj
        self._stopped = False
        self.results: list = []
        self.message_str = message_obj.message_str

    @property
    def unified_msg_origin(self) -> str:
        return f"aiocqhttp:group:{self.message_obj.group_id}"

    def get_message_type(self) -> MessageType:
        return MessageType.GROUP_MESSAGE

    def get_platform_name(self) -> str:
        return "aiocqhttp"

    def get_sender_name(self) -> str:
        return self.message_obj.sender.nickname or ""

    def plain_result(self, text: str):
        return SimpleNamespace(text=text)

    def stop_event(self) -> None:
        self._stopped = True


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
    def _make_plugin(self, reply: str = "🪐 我在，有什么可以帮你？"):
        plugin = object.__new__(module.GrokResponder)
        plugin.enabled = True
        plugin.cooldown_seconds = 0
        plugin.fallback_text = module.DEFAULT_FALLBACK_TEXT
        plugin._last_trigger_at = {}
        plugin.context = FakeContext(reply=reply)
        return plugin

    async def _collect(self, plugin, event):
        return [ret async for ret in plugin.on_group_message(event)]

    async def test_trigger_generates_llm_reply_and_stops_event(self):
        plugin = self._make_plugin()
        event = FakeEvent(make_message_obj("请问 @grok 今天天气怎么样"))
        results = await self._collect(plugin, event)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].text, "🪐 我在，有什么可以帮你？")
        self.assertTrue(event._stopped, "触发后应 stop_event 防止重复回复")
        self.assertEqual(len(plugin.context.generate_calls), 1)
        call = plugin.context.generate_calls[0]
        self.assertEqual(call["chat_provider_id"], "fake-provider")
        self.assertIn("今天天气怎么样", call["prompt"])
        # 不写死任何身份：无 persona 时 system_prompt 只含输出约束
        self.assertNotIn("你是", call["system_prompt"])
        self.assertIn("回复约束", call["system_prompt"])

    async def test_uses_configured_persona_identity(self):
        plugin = self._make_plugin()
        plugin.context.persona_prompt = "你是小星，一个活泼可爱的机器人。"
        event = FakeEvent(make_message_obj("@grok 你好"))
        await self._collect(plugin, event)
        call = plugin.context.generate_calls[0]
        self.assertIn("你是小星", call["system_prompt"])
        self.assertIn("回复约束", call["system_prompt"])

    async def test_applies_prompt_prefix(self):
        plugin = self._make_plugin()
        plugin.context.prompt_prefix = "[系统] "
        event = FakeEvent(make_message_obj("@grok 你好"))
        await self._collect(plugin, event)
        call = plugin.context.generate_calls[0]
        self.assertTrue(call["prompt"].startswith("[系统] "))

    async def test_non_trigger_message_is_ignored(self):
        plugin = self._make_plugin()
        event = FakeEvent(make_message_obj("普通闲聊，没有触发词"))
        results = await self._collect(plugin, event)

        self.assertEqual(results, [])
        self.assertFalse(event._stopped)
        self.assertEqual(plugin.context.generate_calls, [])

    async def test_grok_without_at_is_ignored(self):
        plugin = self._make_plugin()
        event = FakeEvent(make_message_obj("grok 这个模型不错"))
        results = await self._collect(plugin, event)
        self.assertEqual(results, [])
        self.assertFalse(event._stopped)

    async def test_at_segment_trigger(self):
        plugin = self._make_plugin()
        message_obj = make_message_obj(
            "你好呀",
            message=[At(qq="12345", name="Grok 大师"), Plain(text="你好呀")],
        )
        event = FakeEvent(message_obj)
        results = await self._collect(plugin, event)
        self.assertEqual(len(results), 1)
        self.assertTrue(event._stopped)

    async def test_own_message_is_ignored(self):
        plugin = self._make_plugin()
        message_obj = make_message_obj("@grok 自己发的")
        message_obj.sender = SimpleNamespace(user_id="bot1", nickname="机器人")
        event = FakeEvent(message_obj)
        results = await self._collect(plugin, event)
        self.assertEqual(results, [])
        self.assertFalse(event._stopped)

    async def test_llm_failure_uses_fallback(self):
        plugin = self._make_plugin()
        plugin.context.fail_generate = True
        event = FakeEvent(make_message_obj("@grok 在吗"))
        results = await self._collect(plugin, event)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].text, module.DEFAULT_FALLBACK_TEXT)
        self.assertTrue(event._stopped)

    async def test_disabled_plugin_does_nothing(self):
        plugin = self._make_plugin()
        plugin.enabled = False
        event = FakeEvent(make_message_obj("@grok 在吗"))
        results = await self._collect(plugin, event)
        self.assertEqual(results, [])
        self.assertFalse(event._stopped)
        self.assertEqual(plugin.context.generate_calls, [])


if __name__ == "__main__":
    unittest.main()
