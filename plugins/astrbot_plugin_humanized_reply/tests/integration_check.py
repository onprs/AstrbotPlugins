"""本地集成脚本：验证插件的请求注入、响应检测与发送前装饰的完整链路。

使用伪事件对象，不连接任何平台。运行方式（仓库根目录）：

    PYTHONPATH=reference/AstrBot \\
        .venv/Scripts/python.exe plugins/astrbot_plugin_humanized_reply/tests/integration_check.py
"""

from __future__ import annotations

import asyncio
import importlib.util
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

from astrbot.api.message_components import At, Plain, Reply
from astrbot.api.platform import MessageType
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.core.message.message_event_result import (
    MessageEventResult,
    ResultContentType,
)

PLUGIN_DIR = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "astrbot_plugin_humanized_reply_e2e"

_spec = importlib.util.spec_from_file_location(
    PACKAGE_NAME,
    PLUGIN_DIR / "main.py",
    submodule_search_locations=[str(PLUGIN_DIR)],
)
assert _spec and _spec.loader
_module = importlib.util.module_from_spec(_spec)
sys.modules[PACKAGE_NAME] = _module
_spec.loader.exec_module(_module)
main = _module

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    line = f"[{status}] {label}"
    if detail and not condition:
        line += f" -> {detail}"
    print(line)
    if not condition:
        failures.append(label)


class FakeEvent:
    """覆盖插件使用到的全部事件接口。"""

    def __init__(self, **kwargs) -> None:
        self.message_str = kwargs.get("message_str", "")
        self.message_obj = SimpleNamespace(
            message_id=kwargs.get("message_id", "1"),
            timestamp=kwargs.get("timestamp", 1000.0),
            group_id=kwargs.get("group_id", "g1"),
            self_id="bot1",
            sender=SimpleNamespace(
                user_id=kwargs.get("sender_id", "u1"),
                nickname=kwargs.get("nickname", "甲"),
            ),
            message=[Plain(kwargs.get("message_str", ""))],
        )
        self.platform_meta = SimpleNamespace(name="aiocqhttp", id="p1")
        self.is_at_or_wake_command = kwargs.get("is_at_or_wake_command", False)
        self.is_wake = self.is_at_or_wake_command
        self.unified_msg_origin = f"aiocqhttp:GroupMessage:{self.message_obj.group_id}"
        self._extras: dict = {}
        self._result = None

    def get_message_type(self):
        return MessageType.GROUP_MESSAGE

    def get_platform_name(self):
        return "aiocqhttp"

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
        return False

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


def build_plugin(config: dict | None = None) -> main.HumanizedReply:
    plugin = object.__new__(main.HumanizedReply)
    plugin.config = main.merge_config(main.DEFAULT_CONFIG, config or {})
    plugin.enabled = True
    plugin.scope_groups = []
    plugin.scope_platforms = ["aiocqhttp"]
    plugin.mention_passive = main.as_probability(
        plugin.config["mention"]["passive_probability"],
        main.DEFAULT_CONFIG["mention"]["passive_probability"],
    )
    plugin.mention_active = main.as_probability(
        plugin.config["mention"]["active_probability"],
        main.DEFAULT_CONFIG["mention"]["active_probability"],
    )
    plugin.quote_passive = main.as_probability(
        plugin.config["quote"]["passive_probability"],
        main.DEFAULT_CONFIG["quote"]["passive_probability"],
    )
    plugin.quote_active = main.as_probability(
        plugin.config["quote"]["active_probability"],
        main.DEFAULT_CONFIG["quote"]["active_probability"],
    )
    plugin.quote_max_age = 600.0
    plugin.pointing_enabled = True
    plugin.max_recent_messages = 30
    plugin.style_enabled = True
    plugin.anti_echo_mode = str(
        plugin.config["anti_echo"].get("mode", "observe"),
    ).lower()
    plugin.anti_echo_threshold = float(
        plugin.config["anti_echo"].get("similarity_threshold") or 0.6,
    )
    plugin.rewrite_limit = int(
        plugin.config["anti_echo"].get("rewrite_rate_limit_per_session") or 0,
    )
    plugin.apply_to_non_llm = False

    from astrbot_plugin_humanized_reply_e2e.modules.message_ledger import MessageLedger

    plugin.ledger = MessageLedger(persist=False)
    import random

    plugin._rng = random.Random(0)
    return plugin


def make_llm_result(text: str) -> MessageEventResult:
    result = MessageEventResult().message(text)
    result.set_result_content_type(ResultContentType.LLM_RESULT)
    return result


async def scenario_passive_quote_marker() -> None:
    """被动回复：模型用标记引用较早的消息。"""
    print("\n--- 场景 1：被动回复 + 模型引用标记 ---")
    plugin = build_plugin()
    # 较早的消息已不在可见范围内（锚点之后只有本次请求窗口内的消息）。
    plugin.ledger.record(
        group_id="g1",
        message_id="90",
        sender_id="u2",
        nickname="乙",
        summary="方案我看了",
    )
    plugin.ledger.advance_visible_anchor("g1", 1)
    trigger = FakeEvent(
        message_str="你觉得呢",
        message_id="100",
        sender_id="u1",
        nickname="甲",
        is_at_or_wake_command=True,
    )
    await plugin.record_group_message(trigger)

    req = ProviderRequest(prompt="你觉得呢")
    await plugin.inject_pointing_context(trigger, req)
    check("被动请求注入指向参考表", len(req.extra_user_content_parts) == 1)
    part = req.extra_user_content_parts[0]
    check("参考表只包含可见消息", "m2 = 甲" in part.text and "m1 =" not in part.text)
    check(
        "参考表标记为临时内容",
        part.model_dump_for_context()["_no_save"],
    )
    # 请求时即推进锚点，可见范围与模型本轮看到的消息一致。
    check("请求后可见范围清空", plugin.ledger.visible_window("g1") == [])

    # 模型指向本轮可见消息（m2 为触发消息）。
    trigger.set_result(make_llm_result("[[quote:m2]]我觉得可行"))
    await plugin.decorate_group_reply(trigger)
    chain = trigger.get_result().chain
    check("引用标记生成 Reply 段", isinstance(chain[0], Reply) and chain[0].id == "100")
    check("标记文本已剥离", chain[1].text == "我觉得可行")


async def scenario_invalid_marker() -> None:
    """模型指向不可见的旧消息时丢弃标记。"""
    print("\n--- 场景 2：模型指向不可见消息 ---")
    plugin = build_plugin()
    plugin.ledger.record(
        group_id="g1",
        message_id="90",
        sender_id="u2",
        nickname="乙",
        summary="旧消息",
    )
    plugin.ledger.advance_visible_anchor("g1", 1)
    event = FakeEvent(message_str="在吗", message_id="100")
    await plugin.record_group_message(event)

    req = ProviderRequest(prompt="在吗")
    await plugin.inject_pointing_context(event, req)
    event.set_result(make_llm_result("[[quote:m1]]在的"))
    await plugin.decorate_group_reply(event)

    chain = event.get_result().chain
    check("丢弃不可见标记且不添加指向", len(chain) == 1 and chain[0].text == "在的")


async def scenario_active_style() -> None:
    """主动回复：注入表达风格指令，并按概率兜底引用非触发消息。"""
    print("\n--- 场景 3：主动回复表达风格 + 兜底引用 ---")
    plugin = build_plugin({"quote": {"active_probability": 1.0}})
    plugin.quote_active = 1.0
    plugin.ledger.record(
        group_id="g1",
        message_id="90",
        sender_id="u2",
        nickname="乙",
        summary="周末想去爬山",
    )
    trigger = FakeEvent(message_str="哈哈哈哈", message_id="100")
    await plugin.record_group_message(trigger)

    req = ProviderRequest(prompt="哈哈哈哈")
    await plugin.inject_pointing_context(trigger, req)
    await plugin.inject_active_reply_style(trigger, req)
    texts = [part.text for part in req.extra_user_content_parts]
    check("主动回复注入了表达风格指令", any("<active_reply_style>" in t for t in texts))
    check(
        "表达风格指令与指向协议各自独立",
        sum(1 for t in texts if "<active_reply_style>" in t) == 1,
    )

    trigger.set_result(make_llm_result("我倒觉得露营更合适"))
    await plugin.decorate_group_reply(trigger)
    chain = trigger.get_result().chain
    check("主动兜底引用不指向触发消息", isinstance(chain[0], Reply) and chain[0].id == "90")


async def scenario_no_duplicate_pointing() -> None:
    """链中已有指向时跳过装饰。"""
    print("\n--- 场景 4：链中已有指向 ---")
    plugin = build_plugin({"mention": {"passive_probability": 1.0}})
    plugin.mention_passive = 1.0
    event = FakeEvent(message_str="在吗", message_id="100", is_at_or_wake_command=True)
    await plugin.record_group_message(event)
    req = ProviderRequest(prompt="在吗")
    await plugin.inject_pointing_context(event, req)

    result = make_llm_result("在的")
    result.chain.insert(0, At(qq="u1", name="甲"))
    event.set_result(result)
    await plugin.decorate_group_reply(event)
    check("已有 At 时不再追加", len(event.get_result().chain) == 2)


async def scenario_probability_zero() -> None:
    """概率为 0 时不添加任何指向。"""
    print("\n--- 场景 5：概率为 0 ---")
    plugin = build_plugin()
    event = FakeEvent(message_str="今天天气不错", message_id="100")
    await plugin.record_group_message(event)
    req = ProviderRequest(prompt="今天天气不错")
    await plugin.inject_pointing_context(event, req)
    event.set_result(make_llm_result("是挺好的"))
    await plugin.decorate_group_reply(event)
    chain = event.get_result().chain
    check("保持纯文本回复", len(chain) == 1 and isinstance(chain[0], Plain))


async def scenario_echo_pipeline() -> None:
    """附和检测链路：观察模式不改文本，strip 模式剥离标记。"""
    print("\n--- 场景 6：附和检测链路 ---")
    plugin = build_plugin()
    event = FakeEvent(message_str="这个方案成本太高了", message_id="100")
    await plugin.record_group_message(event)
    req = ProviderRequest(prompt="这个方案成本太高了")
    await plugin.inject_pointing_context(event, req)

    response = LLMResponse(
        role="assistant",
        completion_text="这个方案成本太高了",
    )
    await plugin.handle_echo_reply(event, response)
    check("observe 模式不改动文本", response.completion_text == "这个方案成本太高了")

    strip_plugin = build_plugin({"anti_echo": {"mode": "strip"}})
    strip_plugin.anti_echo_mode = "strip"
    strip_event = FakeEvent(message_str="这个方案成本太高了", message_id="100")
    await strip_plugin.record_group_message(strip_event)
    await strip_plugin.inject_pointing_context(
        strip_event,
        ProviderRequest(prompt="这个方案成本太高了"),
    )
    strip_response = LLMResponse(
        role="assistant",
        completion_text="[[quote:m1]]这个方案成本太高了",
    )
    await strip_plugin.handle_echo_reply(strip_event, strip_response)
    check(
        "strip 模式剥离标记",
        "[[" not in strip_response.completion_text
        and "成本太高" in strip_response.completion_text,
    )


async def scenario_persistence() -> None:
    """台账持久化：写入、重载、可见范围重置。"""
    print("\n--- 场景 7：台账持久化 ---")
    from astrbot_plugin_humanized_reply_e2e.modules.message_ledger import MessageLedger

    tmpdir = Path(tempfile.mkdtemp())
    try:
        path = tmpdir / "ledger.db"
        ledger = MessageLedger(data_path=path)
        ledger.record(
            group_id="g1",
            message_id="m1",
            sender_id="u1",
            nickname="甲",
            summary="你好",
        )
        ledger.close()
        reloaded = MessageLedger(data_path=path)
        check("台账载入成功", reloaded.load())
        check("载入后不作为引用目标", reloaded.visible_window("g1") == [])

        reloaded.record(
            group_id="g1",
            message_id="m2",
            sender_id="u2",
            nickname="乙",
            summary="在",
        )
        reloaded.close()
        final = MessageLedger(data_path=path)
        final.load()
        # 重启后模型上下文为空，重启前记录的消息同样不作为引用目标。
        check(
            "再次重启后不引用重启前的消息",
            final.visible_window("g1") == [],
            str([entry.message_id for entry in final.visible_window("g1")]),
        )
        final.record(
            group_id="g1",
            message_id="m3",
            sender_id="u3",
            nickname="丙",
            summary="新消息",
        )
        check(
            "重启后的新消息可引用",
            [entry.message_id for entry in final.visible_window("g1")] == ["m3"],
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


async def main_async() -> int:
    await scenario_passive_quote_marker()
    await scenario_invalid_marker()
    await scenario_active_style()
    await scenario_no_duplicate_pointing()
    await scenario_probability_zero()
    await scenario_echo_pipeline()
    await scenario_persistence()

    print("\n================ 汇总 ================")
    if failures:
        print(f"失败 {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("全部检查通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async()))
