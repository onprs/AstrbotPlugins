"""拟人化回复插件的针对性单元测试。"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from astrbot.api.message_components import At, Image, Plain, Reply
from astrbot.api.platform import MessageType
from astrbot.api.provider import LLMResponse, ProviderRequest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "astrbot_plugin_humanized_reply_testpkg"

if PACKAGE_NAME not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME,
        PLUGIN_DIR / "main.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE_NAME] = module
    spec.loader.exec_module(module)

main = sys.modules[PACKAGE_NAME]
ledger_module = importlib.import_module(f"{PACKAGE_NAME}.modules.message_ledger")
pointing = importlib.import_module(f"{PACKAGE_NAME}.modules.pointing_protocol")
decorator = importlib.import_module(f"{PACKAGE_NAME}.modules.decorator")
anti_echo = importlib.import_module(f"{PACKAGE_NAME}.modules.anti_echo")
config_takeover = importlib.import_module(f"{PACKAGE_NAME}.modules.config_takeover")


class FakePlatformMeta:
    name = "aiocqhttp"
    id = "test_platform"


class FakeEvent:
    """最小可用的 AstrMessageEvent 替身。"""

    def __init__(
        self,
        *,
        message_str: str = "",
        group_id: str = "g1",
        message_id: str = "100",
        sender_id: str = "u1",
        nickname: str = "甲",
        self_id: str = "bot1",
        message=None,
        is_at_or_wake_command: bool = False,
        message_type: MessageType = MessageType.GROUP_MESSAGE,
    ) -> None:
        self.message_str = message_str
        self.message_obj = SimpleNamespace(
            message_id=message_id,
            timestamp=1000.0,
            group_id=group_id,
            self_id=self_id,
            sender=SimpleNamespace(user_id=sender_id, nickname=nickname),
            message=message or [Plain(message_str)],
        )
        self.platform_meta = FakePlatformMeta()
        self.is_at_or_wake_command = is_at_or_wake_command
        self.is_wake = is_at_or_wake_command
        self.unified_msg_origin = f"{message_type.value}:{group_id}"
        self._message_type = message_type
        self._extras: dict = {}
        self._result = None

    def get_message_type(self):
        return self._message_type

    def get_platform_name(self):
        return self.platform_meta.name

    def get_group_id(self):
        return self.message_obj.group_id

    def get_self_id(self):
        return self.message_obj.self_id

    def get_sender_id(self):
        return self.message_obj.sender.user_id

    def get_sender_name(self):
        return self.message_obj.sender.nickname

    def get_messages(self):
        return self.message_obj.message

    def is_private_chat(self):
        return self._message_type == MessageType.FRIEND_MESSAGE

    def set_extra(self, key, value):
        self._extras[key] = value

    def get_extra(self, key=None, default=None):
        if key is None:
            return self._extras
        return self._extras.get(key, default)

    def set_result(self, result):
        self._result = result

    def get_result(self):
        return self._result


class FakeAstrBotConfig(dict):
    """模拟 AstrBotConfig 的保存行为。"""

    def __init__(self, data: dict) -> None:
        super().__init__(data)
        self.save_count = 0

    def save_config(self, replace_config=None, **kwargs):
        self.save_count += 1


def make_plugin(config: dict | None = None) -> main.HumanizedReply:
    plugin = object.__new__(main.HumanizedReply)
    plugin.config = main.merge_config(main.DEFAULT_CONFIG, config or {})
    plugin.enabled = bool(plugin.config.get("enable", True))
    scope = plugin.config.get("scope", {})
    plugin.scope_groups = [str(item) for item in scope.get("groups") or []]
    plugin.scope_platforms = [str(item) for item in scope.get("platforms") or []]
    plugin.mention_passive = main.as_probability(
        plugin.config["mention"].get("passive_probability"),
        main.DEFAULT_CONFIG["mention"]["passive_probability"],
    )
    plugin.mention_active = main.as_probability(
        plugin.config["mention"].get("active_probability"),
        main.DEFAULT_CONFIG["mention"]["active_probability"],
    )
    plugin.quote_passive = main.as_probability(
        plugin.config["quote"].get("passive_probability"),
        main.DEFAULT_CONFIG["quote"]["passive_probability"],
    )
    plugin.quote_active = main.as_probability(
        plugin.config["quote"].get("active_probability"),
        main.DEFAULT_CONFIG["quote"]["active_probability"],
    )
    plugin.quote_max_age = 600.0
    plugin.pointing_enabled = True
    plugin.max_recent_messages = 30
    plugin.style_enabled = True
    plugin.anti_echo_mode = "observe"
    plugin.anti_echo_threshold = float(
        plugin.config["anti_echo"].get("similarity_threshold")
        or main.DEFAULT_CONFIG["anti_echo"]["similarity_threshold"],
    )
    if main.__dict__.get("__file__") and plugin.config["anti_echo"].get("mode"):
        plugin.anti_echo_mode = str(plugin.config["anti_echo"]["mode"]).lower()
    plugin.rewrite_limit = 2
    plugin.apply_to_non_llm = False
    plugin.ledger = ledger_module.MessageLedger(persist=False)
    plugin._rng = __import__("random").Random(0)
    return plugin


class MessageLedgerTests(unittest.TestCase):
    def test_record_and_visible_window(self):
        ledger = ledger_module.MessageLedger(persist=False)
        ledger.record(
            group_id="g1",
            message_id="m1",
            sender_id="u1",
            nickname="甲",
            summary="你好",
        )
        ledger.record(
            group_id="g2",
            message_id="m2",
            sender_id="u2",
            nickname="乙",
            summary="在吗",
        )
        self.assertEqual([entry.message_id for entry in ledger.visible_window("g1")], ["m1"])
        self.assertEqual([entry.message_id for entry in ledger.visible_window("g2")], ["m2"])

    def test_anchor_limits_visible_window(self):
        ledger = ledger_module.MessageLedger(persist=False)
        for index in range(3):
            ledger.record(
                group_id="g1",
                message_id=f"m{index}",
                sender_id="u1",
                nickname="甲",
                summary=str(index),
            )
        ledger.advance_visible_anchor("g1", 3)
        ledger.record(
            group_id="g1",
            message_id="m3",
            sender_id="u2",
            nickname="乙",
            summary="新消息",
        )
        self.assertEqual(
            [entry.message_id for entry in ledger.visible_window("g1")],
            ["m3"],
        )

    def test_duplicate_message_id_is_ignored(self):
        ledger = ledger_module.MessageLedger(persist=False)
        ledger.record(
            group_id="g1",
            message_id="m1",
            sender_id="u1",
            nickname="甲",
            summary="你好",
        )
        ledger.record(
            group_id="g1",
            message_id="m1",
            sender_id="u1",
            nickname="甲",
            summary="你好",
        )
        self.assertEqual(len(ledger.visible_window("g1")), 1)

    def test_ring_trim_keeps_latest(self):
        ledger = ledger_module.MessageLedger(
            persist=False,
            max_messages_per_group=3,
        )
        for index in range(5):
            ledger.record(
                group_id="g1",
                message_id=f"m{index}",
                sender_id="u1",
                nickname="甲",
                summary=str(index),
            )
        self.assertEqual(
            [entry.message_id for entry in ledger.visible_window("g1")],
            ["m2", "m3", "m4"],
        )

    def test_persist_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.db"
            ledger = ledger_module.MessageLedger(data_path=path)
            ledger.record(
                group_id="g1",
                message_id="m1",
                sender_id="u1",
                nickname="甲",
                summary="你好",
            )
            ledger.close()

            restored = ledger_module.MessageLedger(data_path=path)
            self.assertTrue(restored.load())
            entries = list(restored._entries["g1"])
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].message_id, "m1")
            # 重启后核心群上下文为空，载入的历史消息不作为引用目标。
            self.assertEqual(restored.visible_window("g1"), [])

    def test_pick_weighted_respects_age(self):
        entry = ledger_module.LedgerEntry(
            seq=1,
            message_id="old",
            group_id="g1",
            sender_id="u1",
            nickname="甲",
            timestamp=0.0,
            summary="很久以前",
        )
        picked = ledger_module.pick_weighted([entry], max_age_seconds=1.0)
        self.assertIsNone(picked)

    def test_latest_member_skips_self(self):
        entries = [
            ledger_module.LedgerEntry(
                seq=1,
                message_id="m1",
                group_id="g1",
                sender_id="bot1",
                nickname="机器人",
                timestamp=1000.0,
                summary="我",
            ),
            ledger_module.LedgerEntry(
                seq=2,
                message_id="m2",
                group_id="g1",
                sender_id="u2",
                nickname="乙",
                timestamp=1001.0,
                summary="在",
            ),
        ]
        entry = ledger_module.latest_member(entries, exclude="bot1")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.message_id, "m2")

    def test_summarize_chain(self):
        chain = [
            Plain("你好"),
            At(qq="123", name="小明"),
            Image(file="x.jpg"),
        ]
        summary = ledger_module.summarize_chain(chain)
        self.assertIn("你好", summary)
        self.assertIn("@小明", summary)
        self.assertIn("[图片]", summary)


class PointingProtocolTests(unittest.TestCase):
    def make_entry(self, seq: int, sender_id: str = "u1") -> ledger_module.LedgerEntry:
        return ledger_module.LedgerEntry(
            seq=seq,
            message_id=f"msg{seq}",
            group_id="g1",
            sender_id=sender_id,
            nickname="甲",
            timestamp=1000.0,
            summary="内容",
        )

    def test_quote_marker(self):
        plan = pointing.resolve_plan("[[quote:m2]]你好", visible=[self.make_entry(2)])
        self.assertEqual(plan.text, "你好")
        self.assertIsNotNone(plan.quote_target)
        self.assertEqual(plan.quote_target.message_id, "msg2")

    def test_at_marker(self):
        plan = pointing.resolve_plan("[[at:3]] 在吗", visible=[self.make_entry(3)])
        self.assertEqual(plan.text, "在吗")
        self.assertIsNotNone(plan.at_target)

    def test_marker_without_prefix(self):
        plan = pointing.resolve_plan("[[quote:2]]好的", visible=[self.make_entry(2)])
        self.assertIsNotNone(plan.quote_target)

    def test_unknown_seq_is_dropped(self):
        plan = pointing.resolve_plan("[[quote:99]]好的", visible=[self.make_entry(2)])
        self.assertIsNone(plan.quote_target)
        self.assertEqual(plan.text, "好的")
        self.assertTrue(plan.dropped_reason)

    def test_self_target_is_dropped(self):
        plan = pointing.resolve_plan(
            "[[quote:2]]好的",
            visible=[self.make_entry(2, sender_id="bot1")],
            self_id="bot1",
        )
        self.assertIsNone(plan.quote_target)

    def test_inline_markers_are_stripped(self):
        plan = pointing.resolve_plan(
            "你好 [[quote:m2]] 再见",
            visible=[self.make_entry(2)],
        )
        self.assertEqual(plan.text, "你好  再见")
        self.assertIsNone(plan.quote_target)

    def test_no_marker_keeps_text(self):
        plan = pointing.resolve_plan("普通回复", visible=[self.make_entry(2)])
        self.assertEqual(plan.text, "普通回复")
        self.assertFalse(plan.has_target)

    def test_render_reference_table(self):
        table = pointing.render_reference_table([self.make_entry(5)])
        self.assertTrue(table.startswith("m5 = 甲"))
        self.assertIn("内容", table)


class AntiEchoTests(unittest.TestCase):
    def test_identical_reply_is_echo(self):
        verdict = anti_echo.evaluate("今天天气很好，我们出去走走吧", ["今天天气很好，我们出去走走吧"])
        self.assertTrue(verdict.is_echo)

    def test_different_reply_is_not_echo(self):
        verdict = anti_echo.evaluate("我建议带上雨伞，下午可能下雨", ["今天天气很好，我们出去走走吧"])
        self.assertFalse(verdict.is_echo)

    def test_reworded_reply_is_echo(self):
        # 字符 3-gram 只识别表层改写，“重新组织措辞”的语义改写不在检测范围内。
        verdict = anti_echo.evaluate(
            "这个方案的成本太高",
            ["这个方案成本太高了"],
            threshold=0.4,
        )
        self.assertTrue(verdict.is_echo)

    def test_filler_reply_is_echo(self):
        verdict = anti_echo.evaluate("是的", ["今天天气很好，我们出去走走吧"])
        self.assertTrue(verdict.is_echo)
        self.assertIn("确认式", verdict.reason)

    def test_empty_sources_are_ignored(self):
        verdict = anti_echo.evaluate("好的", [""])
        self.assertFalse(verdict.is_echo)

    def test_semantic_paraphrase_is_out_of_scope(self):
        # 语义层面的改写不依赖字符重合，不应判定为附和，避免误杀正常表达。
        verdict = anti_echo.evaluate(
            "天气不错，建议出去走走",
            ["今天天气很好，我们出去走走吧"],
            threshold=0.6,
        )
        self.assertFalse(verdict.is_echo)

    def test_similarity_is_symmetric(self):
        left = "这个方案我觉得可行"
        right = "我觉得这个方案可行"
        self.assertAlmostEqual(
            anti_echo.similarity(left, right),
            anti_echo.similarity(right, left),
        )


class DecoratorTests(unittest.TestCase):
    def make_entry(self, seq: int, sender_id: str = "u1") -> ledger_module.LedgerEntry:
        return ledger_module.LedgerEntry(
            seq=seq,
            message_id=f"msg{seq}",
            group_id="g1",
            sender_id=sender_id,
            nickname="甲",
            timestamp=1000.0,
            summary="内容",
        )

    def test_head_has_pointing(self):
        self.assertTrue(decorator.head_has_pointing([Reply(id="1"), Plain("hi")]))
        self.assertTrue(decorator.head_has_pointing([At(qq="1"), Plain("hi")]))
        self.assertFalse(decorator.head_has_pointing([Plain("hi")]))

    def test_can_decorate(self):
        self.assertTrue(decorator.can_decorate([Plain("hi"), Image(file="x")]))
        self.assertFalse(decorator.can_decorate([]))

    def test_apply_pointing_order(self):
        chain = [Plain("你好")]
        decision = decorator.PointingDecision(
            at_target=self.make_entry(1),
            quote_target=self.make_entry(2),
        )
        decorator.apply_pointing(chain, decision)
        self.assertIsInstance(chain[0], Reply)
        self.assertEqual(chain[0].id, "msg2")
        self.assertIsInstance(chain[1], At)
        self.assertEqual(chain[1].qq, "u1")
        self.assertTrue(chain[2].text.startswith("\n"))

    def test_apply_pointing_quote_only(self):
        chain = [Plain("你好")]
        decorator.apply_pointing(
            chain,
            decorator.PointingDecision(quote_target=self.make_entry(1)),
        )
        self.assertIsInstance(chain[0], Reply)
        self.assertEqual(chain[1].text, "你好")

    def test_fallback_never_uses_both(self):
        import random

        rng = random.Random(0)
        for _ in range(50):
            use_mention, use_quote = decorator.decide_fallback(
                is_active=False,
                mention_probability=1.0,
                quote_probability=1.0,
                rng=rng,
            )
            self.assertFalse(use_mention and use_quote)


class ConfigTakeoverTests(unittest.TestCase):
    def make_confs(self) -> dict:
        return {
            "default": FakeAstrBotConfig(
                {
                    "platform_settings": {
                        "reply_with_mention": True,
                        "reply_with_quote": True,
                    },
                },
            ),
        }

    def test_takeover_closes_switches_and_backs_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "takeover.json"
            confs = self.make_confs()
            result = config_takeover.takeover(confs, path)
            self.assertTrue(result.applied)
            self.assertTrue(result.backup_created)
            settings = confs["default"]["platform_settings"]
            self.assertFalse(settings["reply_with_mention"])
            self.assertFalse(settings["reply_with_quote"])
            backup = config_takeover.backup_values(path)
            self.assertTrue(backup["default"]["reply_with_mention"])

    def test_second_takeover_keeps_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "takeover.json"
            confs = self.make_confs()
            config_takeover.takeover(confs, path)
            # 模拟用户手动改回开启状态后重载插件。
            confs["default"]["platform_settings"]["reply_with_mention"] = True
            confs["default"]["platform_settings"]["reply_with_quote"] = True
            result = config_takeover.takeover(confs, path)
            self.assertFalse(result.backup_created)
            backup = config_takeover.backup_values(path)
            self.assertTrue(backup["default"]["reply_with_mention"])
            self.assertFalse(
                confs["default"]["platform_settings"]["reply_with_mention"],
            )

    def test_restore_returns_original_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "takeover.json"
            confs = self.make_confs()
            config_takeover.takeover(confs, path)
            result = config_takeover.restore(confs, path)
            self.assertTrue(result.restored)
            settings = confs["default"]["platform_settings"]
            self.assertTrue(settings["reply_with_mention"])
            self.assertTrue(settings["reply_with_quote"])
            self.assertFalse(path.exists())

    def test_restore_without_state_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "takeover.json"
            result = config_takeover.restore(self.make_confs(), path)
            self.assertFalse(result.restored)

    def test_missing_profile_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "takeover.json"
            result = config_takeover.takeover({"other": FakeAstrBotConfig({})}, path)
            self.assertFalse(result.applied)


class PointingContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_injects_reference_table_once(self):
        plugin = make_plugin()
        event = FakeEvent(message_str="大家看看", message_id="100")
        plugin.ledger.record(
            group_id="g1",
            message_id="100",
            sender_id="u1",
            nickname="甲",
            summary="大家看看",
        )
        req = ProviderRequest(prompt="大家看看")

        await plugin.inject_pointing_context(event, req)

        self.assertEqual(len(req.extra_user_content_parts), 1)
        part = req.extra_user_content_parts[0]
        self.assertIn("<message_index>", part.text)
        self.assertIn("m1 = 甲", part.text)
        self.assertTrue(part.model_dump_for_context()["_no_save"])
        self.assertIn(1, event.get_extra(main.POINTING_EXTRA))

    async def test_skips_private_and_inactive_scope(self):
        plugin = make_plugin(config={"scope": {"groups": ["g9"]}})
        event = FakeEvent(message_str="你好")
        req = ProviderRequest(prompt="你好")
        await plugin.inject_pointing_context(event, req)
        self.assertEqual(req.extra_user_content_parts, [])

    async def test_active_style_only_for_active_reply(self):
        plugin = make_plugin()
        passive = FakeEvent(message_str="你好", is_at_or_wake_command=True)
        passive_req = ProviderRequest(prompt="你好")
        await plugin.inject_active_reply_style(passive, passive_req)
        self.assertEqual(passive_req.extra_user_content_parts, [])

        active = FakeEvent(message_str="你好")
        active_req = ProviderRequest(prompt="你好")
        await plugin.inject_active_reply_style(active, active_req)
        self.assertEqual(len(active_req.extra_user_content_parts), 1)
        self.assertTrue(active.get_extra(main.ACTIVE_REPLY_EXTRA))

    async def test_style_not_duplicated(self):
        plugin = make_plugin()
        event = FakeEvent(message_str="你好")
        req = ProviderRequest(prompt="你好")
        await plugin.inject_active_reply_style(event, req)
        await plugin.inject_active_reply_style(event, req)
        self.assertEqual(len(req.extra_user_content_parts), 1)


class DecorateResultTests(unittest.IsolatedAsyncioTestCase):
    def make_result(self, text: str):
        from astrbot.core.message.message_event_result import (
            MessageEventResult,
            ResultContentType,
        )

        result = MessageEventResult().message(text)
        result.set_result_content_type(ResultContentType.LLM_RESULT)
        return result

    async def test_model_marker_adds_reply_segment(self):
        plugin = make_plugin()
        event = FakeEvent(message_str="在吗", message_id="100")
        plugin.ledger.record(
            group_id="g1",
            message_id="100",
            sender_id="u1",
            nickname="甲",
            summary="在吗",
        )
        visible_entry = plugin.ledger.visible_window("g1")[0]
        event.set_extra(main.POINTING_EXTRA, {visible_entry.seq: visible_entry})
        event.set_result(self.make_result("[[quote:m1]]我在"))

        await plugin.decorate_group_reply(event)

        result = event.get_result()
        self.assertIsInstance(result.chain[0], Reply)
        self.assertEqual(result.chain[0].id, "100")
        self.assertEqual(result.chain[1].text, "我在")

    async def test_probability_zero_keeps_plain(self):
        plugin = make_plugin()
        event = FakeEvent(message_str="在吗", message_id="100")
        plugin.ledger.record(
            group_id="g1",
            message_id="100",
            sender_id="u1",
            nickname="甲",
            summary="在吗",
        )
        entry = plugin.ledger.visible_window("g1")[0]
        event.set_extra(main.POINTING_EXTRA, {entry.seq: entry})
        event.set_result(self.make_result("我在"))

        await plugin.decorate_group_reply(event)

        chain = event.get_result().chain
        self.assertEqual(len(chain), 1)
        self.assertIsInstance(chain[0], Plain)

    async def test_existing_pointing_skips_decoration(self):
        plugin = make_plugin(config={"mention": {"passive_probability": 1.0}})
        event = FakeEvent(message_str="在吗", message_id="100")
        plugin.ledger.record(
            group_id="g1",
            message_id="100",
            sender_id="u1",
            nickname="甲",
            summary="在吗",
        )
        entry = plugin.ledger.visible_window("g1")[0]
        event.set_extra(main.POINTING_EXTRA, {entry.seq: entry})
        event.set_result(self.make_result("我在"))
        event.get_result().chain.insert(0, At(qq="u1", name="甲"))

        await plugin.decorate_group_reply(event)

        chain = event.get_result().chain
        self.assertEqual(len(chain), 2)

    async def test_non_model_result_is_ignored(self):
        from astrbot.core.message.message_event_result import (
            MessageEventResult,
            ResultContentType,
        )

        plugin = make_plugin(config={"mention": {"passive_probability": 1.0}})
        event = FakeEvent(message_str="在吗", message_id="100")
        plugin.ledger.record(
            group_id="g1",
            message_id="100",
            sender_id="u1",
            nickname="甲",
            summary="在吗",
        )
        entry = plugin.ledger.visible_window("g1")[0]
        event.set_extra(main.POINTING_EXTRA, {entry.seq: entry})
        result = MessageEventResult().message("指令输出")
        result.set_result_content_type(ResultContentType.GENERAL_RESULT)
        event.set_result(result)

        await plugin.decorate_group_reply(event)

        self.assertEqual(len(event.get_result().chain), 1)

    async def test_fallback_mention_uses_sender(self):
        plugin = make_plugin(config={"mention": {"passive_probability": 1.0}})
        event = FakeEvent(message_str="在吗", message_id="100", is_at_or_wake_command=True)
        plugin.ledger.record(
            group_id="g1",
            message_id="100",
            sender_id="u1",
            nickname="甲",
            summary="在吗",
        )
        entry = plugin.ledger.visible_window("g1")[0]
        event.set_extra(main.POINTING_EXTRA, {entry.seq: entry})
        event.set_result(self.make_result("我在"))

        await plugin.decorate_group_reply(event)

        chain = event.get_result().chain
        self.assertIsInstance(chain[0], At)
        self.assertEqual(chain[0].qq, "u1")

    async def test_active_fallback_quote_skips_trigger(self):
        plugin = make_plugin(config={"quote": {"active_probability": 1.0}})
        plugin.quote_active = 1.0
        event = FakeEvent(message_str="刷屏消息", message_id="200")
        plugin.ledger.record(
            group_id="g1",
            message_id="100",
            sender_id="u2",
            nickname="乙",
            summary="更早的消息",
        )
        plugin.ledger.record(
            group_id="g1",
            message_id="200",
            sender_id="u1",
            nickname="甲",
            summary="刷屏消息",
        )
        entries = {entry.seq: entry for entry in plugin.ledger.visible_window("g1")}
        event.set_extra(main.POINTING_EXTRA, entries)
        event.set_result(self.make_result("我来说两句"))

        await plugin.decorate_group_reply(event)

        chain = event.get_result().chain
        self.assertIsInstance(chain[0], Reply)
        self.assertEqual(chain[0].id, "100")

    async def test_invalid_marker_is_stripped(self):
        plugin = make_plugin()
        event = FakeEvent(message_str="在吗", message_id="100")
        plugin.ledger.record(
            group_id="g1",
            message_id="100",
            sender_id="u1",
            nickname="甲",
            summary="在吗",
        )
        entry = plugin.ledger.visible_window("g1")[0]
        event.set_extra(main.POINTING_EXTRA, {entry.seq: entry})
        event.set_result(self.make_result("[[quote:99]]我在"))

        await plugin.decorate_group_reply(event)

        chain = event.get_result().chain
        self.assertEqual(len(chain), 1)
        self.assertEqual(chain[0].text, "我在")

    def test_replace_plain_text_multiple_segments(self):
        chain = [Plain("[[quote:m1]]第一段"), Image(file="x"), Plain("第二段")]
        main.HumanizedReply.replace_plain_text(chain, "第一段")
        self.assertEqual(chain[0].text, "第一段")
        self.assertEqual(chain[2].text, "第二段")


class EchoResponseTests(unittest.IsolatedAsyncioTestCase):
    async def test_observe_mode_keeps_text(self):
        plugin = make_plugin(config={"anti_echo": {"mode": "observe"}})
        event = FakeEvent(message_str="今天天气很好，我们出去走走吧")
        event.set_extra(main.POINTING_EXTRA, {1: object()})
        event.set_extra(
            main.ECHO_SOURCES_EXTRA,
            ["今天天气很好，我们出去走走吧"],
        )
        response = LLMResponse(role="assistant", completion_text="今天天气很好，我们出去走走吧")

        await plugin.handle_echo_reply(event, response)

        self.assertEqual(response.completion_text, "今天天气很好，我们出去走走吧")

    async def test_strip_mode_removes_marker_only(self):
        plugin = make_plugin(config={"anti_echo": {"mode": "strip"}})
        plugin.anti_echo_mode = "strip"
        event = FakeEvent(message_str="今天天气很好，我们出去走走吧")
        event.set_extra(main.POINTING_EXTRA, {1: object()})
        event.set_extra(main.ECHO_SOURCES_EXTRA, ["今天天气很好，我们出去走走吧"])
        response = LLMResponse(
            role="assistant",
            completion_text="[[quote:m1]]今天天气很好，我们出去走走吧",
        )

        await plugin.handle_echo_reply(event, response)

        self.assertNotIn("[[", response.completion_text)
        self.assertIn("天气", response.completion_text)

    async def test_rewrite_mode_rate_limited(self):
        plugin = make_plugin(config={"anti_echo": {"mode": "rewrite"}})
        plugin.anti_echo_mode = "rewrite"
        plugin.rewrite_limit = 0
        event = FakeEvent(message_str="今天天气很好，我们出去走走吧")
        event.set_extra(main.POINTING_EXTRA, {1: object()})
        event.set_extra(main.ECHO_SOURCES_EXTRA, ["今天天气很好，我们出去走走吧"])
        response = LLMResponse(role="assistant", completion_text="今天天气很好，我们出去走走吧")

        await plugin.handle_echo_reply(event, response)

        self.assertEqual(response.completion_text, "今天天气很好，我们出去走走吧")


class ConfigMergeTests(unittest.TestCase):
    def test_merge_fills_missing_keys(self):
        merged = main.merge_config(main.DEFAULT_CONFIG, {"mention": {"passive_probability": 0.5}})
        self.assertEqual(merged["mention"]["passive_probability"], 0.5)
        self.assertEqual(
            merged["mention"]["active_probability"],
            main.DEFAULT_CONFIG["mention"]["active_probability"],
        )

    def test_merge_ignores_broken_structure(self):
        merged = main.merge_config(main.DEFAULT_CONFIG, {"mention": "坏值"})
        self.assertEqual(
            merged["mention"]["passive_probability"],
            main.DEFAULT_CONFIG["mention"]["passive_probability"],
        )

    def test_probability_is_clamped(self):
        self.assertEqual(main.as_probability(3, 0.5), 1.0)
        self.assertEqual(main.as_probability(-1, 0.5), 0.0)
        self.assertEqual(main.as_probability("坏值", 0.5), 0.5)


class GroupMessageRecordingTests(unittest.IsolatedAsyncioTestCase):
    async def test_records_group_message(self):
        plugin = make_plugin()
        event = FakeEvent(message_str="大家看看这个", message_id="100")
        await plugin.record_group_message(event)
        entries = plugin.ledger.visible_window("g1")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].message_id, "100")
        self.assertIn("大家看看这个", entries[0].summary)

    async def test_skips_own_message(self):
        plugin = make_plugin()
        event = FakeEvent(message_str="我自己发的", sender_id="bot1")
        await plugin.record_group_message(event)
        self.assertEqual(plugin.ledger.visible_window("g1"), [])

    async def test_anchor_advanced_after_request(self):
        plugin = make_plugin()
        event = FakeEvent(message_str="你好", message_id="100")
        await plugin.record_group_message(event)
        await plugin.inject_pointing_context(event, ProviderRequest(prompt="你好"))
        self.assertEqual(plugin.ledger.visible_window("g1"), [])


if __name__ == "__main__":
    unittest.main()
