"""戳一戳回应插件的针对性单元测试。"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

from astrbot.api.message_components import Plain, Poke, Reply

PLUGIN_DIR = Path(__file__).resolve().parents[1]
MODULE_NAME = "astrbot_plugin_poke_reply_testpkg"

if MODULE_NAME not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        MODULE_NAME,
        PLUGIN_DIR / "main.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)

main = sys.modules[MODULE_NAME]


class FakeConversationManager:
    def __init__(self, conversation="conversation") -> None:
        self.conversation = conversation
        self.current_conversation_id = "conv1"
        self.new_conversations = 0
        self.requested_ids = []

    async def get_curr_conversation_id(self, umo: str):
        return self.current_conversation_id

    async def new_conversation(self, umo: str, platform_id: str):
        self.new_conversations += 1
        self.current_conversation_id = f"new-conv-{self.new_conversations}"
        return self.current_conversation_id

    async def get_conversation(self, umo: str, conversation_id: str):
        self.requested_ids.append(conversation_id)
        return self.conversation


class FakeContext:
    def __init__(self, conversation_manager: FakeConversationManager | None = None):
        self.conversation_manager = conversation_manager or FakeConversationManager()


class FakeMessageChain:
    def __init__(self, chain) -> None:
        self.chain = chain


class FakeEvent:
    def __init__(
        self,
        message,
        *,
        raw_message=None,
        message_id: str = "poke-uuid",
        group_id: str = "group1",
        sender_id: str = "user1",
        sender_name: str = "小明",
        self_id: str = "bot1",
        platform_name: str = "aiocqhttp",
        private: bool = False,
    ) -> None:
        self.message_obj = SimpleNamespace(
            message=message,
            message_id=message_id,
            raw_message=raw_message or {},
        )
        self.message_str = ""
        self.unified_msg_origin = (
            f"{platform_name}:{'private' if private else 'group'}:{group_id}"
        )
        self.platform_name = platform_name
        self.group_id = group_id
        self.sender_id = sender_id
        self.sender_name = sender_name
        self.self_id = self_id
        self.private = private
        self.extras = {}
        self.sent_chains = []
        self._stopped = False

    def get_platform_name(self):
        return self.platform_name

    def get_platform_id(self):
        return "platform1"

    def get_group_id(self):
        return self.group_id

    def get_self_id(self):
        return self.self_id

    def get_sender_id(self):
        return self.sender_id

    def get_sender_name(self):
        return self.sender_name

    def get_messages(self):
        return self.message_obj.message

    def is_private_chat(self):
        return self.private

    def set_extra(self, key, value):
        self.extras[key] = value

    def get_extra(self, key=None, default=None):
        if key is None:
            return self.extras
        return self.extras.get(key, default)

    def plain_result(self, text):
        return ("plain", text)

    def request_llm(self, prompt, conversation=None):
        return ("llm", {"prompt": prompt, "conversation": conversation})

    def stop_event(self):
        self._stopped = True

    def is_stopped(self):
        return self._stopped

    async def send(self, message_chain):
        self.sent_chains.append(message_chain)


async def collect_results(plugin: main.PokeReply, event: FakeEvent) -> list:
    return [result async for result in plugin.handle_poke(event)]


class PokeReplyTest(unittest.IsolatedAsyncioTestCase):
    def make_plugin(self, config=None, manager=None) -> main.PokeReply:
        return main.PokeReply(FakeContext(manager), config or {})

    async def test_random_mode_sends_one_configured_text(self):
        plugin = self.make_plugin(
            {"response_mode": "random", "reply_texts": ["甲", "乙"]},
        )
        event = FakeEvent([Poke(id="bot1")])

        results = await collect_results(plugin, event)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][0], "plain")
        self.assertIn(results[0][1], {"甲", "乙"})
        self.assertTrue(event.is_stopped())

    async def test_random_mode_respects_empty_list(self):
        plugin = self.make_plugin({"response_mode": "random", "reply_texts": []})
        event = FakeEvent([Poke(id="bot1")])

        results = await collect_results(plugin, event)

        self.assertEqual(results, [])
        self.assertTrue(event.is_stopped())

    async def test_ignores_poke_targeting_other_user(self):
        plugin = self.make_plugin({"response_mode": "random"})
        event = FakeEvent([Poke(id="bot2")])

        results = await collect_results(plugin, event)

        self.assertEqual(results, [])
        self.assertFalse(event.is_stopped())

    async def test_ignores_poke_sent_by_bot_itself(self):
        plugin = self.make_plugin({"response_mode": "random"})
        event = FakeEvent([Poke(id="bot1")], sender_id="bot1")

        results = await collect_results(plugin, event)

        self.assertEqual(results, [])
        self.assertFalse(event.is_stopped())

    async def test_ignores_non_poke_message(self):
        plugin = self.make_plugin({"response_mode": "random"})
        event = FakeEvent([Plain("你好")])

        results = await collect_results(plugin, event)

        self.assertEqual(results, [])
        self.assertFalse(event.is_stopped())

    async def test_ignores_other_platform(self):
        plugin = self.make_plugin({"response_mode": "random"})
        event = FakeEvent([Poke(id="bot1")], platform_name="telegram")

        results = await collect_results(plugin, event)

        self.assertEqual(results, [])

    async def test_llm_mode_uses_prompt_and_current_conversation(self):
        conversation = object()
        manager = FakeConversationManager(conversation=conversation)
        plugin = self.make_plugin(
            {
                "response_mode": "llm",
                "llm_prompt": "{username}/{user_id}/{scene}",
            },
            manager=manager,
        )
        event = FakeEvent(
            [Poke(id="bot1")],
            raw_message={"sender": {"card": "群名片"}},
        )

        results = await collect_results(plugin, event)

        self.assertEqual(len(results), 1)
        kind, payload = results[0]
        self.assertEqual(kind, "llm")
        self.assertEqual(payload["prompt"], "群名片/user1/群聊")
        self.assertIs(payload["conversation"], conversation)
        self.assertTrue(event.is_stopped())

    async def test_llm_mode_creates_conversation_when_missing(self):
        conversation = object()
        manager = FakeConversationManager(conversation=conversation)
        manager.current_conversation_id = None
        plugin = self.make_plugin({"response_mode": "llm"}, manager=manager)
        event = FakeEvent([Poke(id="bot1")])

        results = await collect_results(plugin, event)

        self.assertEqual(results[0][0], "llm")
        self.assertEqual(manager.new_conversations, 1)
        self.assertIs(results[0][1]["conversation"], conversation)

    async def test_unknown_mode_falls_back_to_random(self):
        plugin = self.make_plugin(
            {"response_mode": "unknown", "reply_texts": ["甲"]},
        )
        event = FakeEvent([Poke(id="bot1")])

        results = await collect_results(plugin, event)

        self.assertEqual(results, [("plain", "甲")])

    async def test_cooldown_blocks_repeated_poke_from_same_user(self):
        plugin = self.make_plugin(
            {
                "response_mode": "random",
                "reply_texts": ["甲"],
                "cooldown_seconds": 30,
            },
        )
        first_event = FakeEvent([Poke(id="bot1")])
        second_event = FakeEvent([Poke(id="bot1")])

        first_results = await collect_results(plugin, first_event)
        second_results = await collect_results(plugin, second_event)

        self.assertEqual(len(first_results), 1)
        self.assertEqual(second_results, [])
        self.assertFalse(second_event.is_stopped())

    async def test_disabled_plugin_does_nothing(self):
        plugin = self.make_plugin({"enable": False, "response_mode": "random"})
        event = FakeEvent([Poke(id="bot1")])

        results = await collect_results(plugin, event)

        self.assertEqual(results, [])
        self.assertFalse(event.is_stopped())

    def test_poke_filter_accepts_poke_to_bot(self):
        event = FakeEvent([Poke(id="bot1")])

        self.assertTrue(main.PokeToBotFilter().filter(event, {}))

    def test_poke_filter_rejects_other_target(self):
        event = FakeEvent([Poke(id="bot2")])

        self.assertFalse(main.PokeToBotFilter().filter(event, {}))

    def test_poke_filter_rejects_normal_message(self):
        event = FakeEvent([Plain("你好")])

        self.assertFalse(main.PokeToBotFilter().filter(event, {}))

    def test_poke_filter_rejects_other_platform(self):
        event = FakeEvent([Poke(id="bot1")], platform_name="telegram")

        self.assertFalse(main.PokeToBotFilter().filter(event, {}))

    def test_poke_filter_rejects_self_poke(self):
        event = FakeEvent([Poke(id="bot1")], sender_id="bot1")

        self.assertFalse(main.PokeToBotFilter().filter(event, {}))

    async def test_strip_invalid_poke_quote(self):
        plugin = self.make_plugin({"response_mode": "random"})
        event = FakeEvent([Poke(id="bot1")], message_id="poke-uuid")
        event.set_extra(main.POKE_REPLY_EXTRA, True)

        await plugin.strip_invalid_poke_quote(event)
        chain = FakeMessageChain(
            [Reply(id="poke-uuid"), Plain("回复内容")],
        )
        await event.send(chain)

        self.assertEqual(len(event.sent_chains), 1)
        self.assertEqual(
            [component.type for component in event.sent_chains[0].chain],
            ["Plain"],
        )
        self.assertTrue(event.get_extra(main.POKE_QUOTE_FILTER_EXTRA))

    async def test_does_not_strip_real_reply_for_normal_event(self):
        plugin = self.make_plugin({"response_mode": "random"})
        event = FakeEvent([Plain("你好")], message_id="123")
        chain = FakeMessageChain([Reply(id="123"), Plain("回复内容")])

        await plugin.strip_invalid_poke_quote(event)
        await event.send(chain)

        self.assertEqual(
            [component.type for component in event.sent_chains[0].chain],
            ["Reply", "Plain"],
        )


if __name__ == "__main__":
    unittest.main()
