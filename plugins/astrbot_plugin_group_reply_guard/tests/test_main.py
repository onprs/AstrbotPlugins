from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

from astrbot.api.message_components import Plain
from astrbot.api.platform import MessageType
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.core.agent.message import TextPart
from astrbot.core.agent.tool import FunctionTool, ToolSet

MODULE_PATH = Path(__file__).resolve().parents[1] / "main.py"
spec = importlib.util.spec_from_file_location("group_reply_guard_main", MODULE_PATH)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class FakeEvent:
    def __init__(self, message_type=MessageType.GROUP_MESSAGE):
        self._message_type = message_type
        self.original_meta = SimpleNamespace(support_proactive_message=True)
        self.platform_meta = self.original_meta
        self.platform = self.original_meta
        self.extras = {}
        self.result = None
        self.is_at_or_wake_command = False
        self.is_wake = False

    def get_message_type(self):
        return self._message_type

    def set_extra(self, key, value):
        self.extras[key] = value

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)

    def get_result(self):
        return self.result


class GroupReplyGuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.plugin = object.__new__(module.GroupReplyGuard)

    async def test_group_request_hides_proactive_tool_and_marks_event(self):
        req = ProviderRequest(system_prompt="base")
        req.func_tool = ToolSet(
            [
                FunctionTool(
                    name=module.SEND_MESSAGE_TOOL,
                    description="send",
                    parameters={"type": "object", "properties": {}},
                ),
                FunctionTool(
                    name="other_tool",
                    description="other",
                    parameters={"type": "object", "properties": {}},
                ),
            ]
        )
        event = FakeEvent()

        await self.plugin.guard_group_request(event, req)
        await self.plugin.guard_group_request(event, req)

        self.assertIsNone(req.func_tool.get_tool(module.SEND_MESSAGE_TOOL))
        self.assertIsNotNone(req.func_tool.get_tool("other_tool"))
        self.assertFalse(event.platform_meta.support_proactive_message)
        self.assertTrue(event.original_meta.support_proactive_message)
        self.assertEqual(req.system_prompt.count(module.GROUP_REPLY_RULE), 1)
        self.assertIn("`send_message_to_user`", req.system_prompt)
        self.assertIn("media or reaction tools", req.system_prompt)
        self.assertNotIn("proactive message-sending tool", req.system_prompt)
        self.assertTrue(event.get_extra("_group_reply_guard_active"))
        self.assertFalse(event.get_extra(module._AGENT_FINAL_EXTRA))

    async def test_automatic_group_reply_gets_temporary_context_hint_once(self):
        req = ProviderRequest()
        req.extra_user_content_parts.append(TextPart(text="existing"))
        event = FakeEvent()
        event.is_wake = True

        await self.plugin.guide_automatic_group_reply(event, req)
        await self.plugin.guide_automatic_group_reply(event, req)

        hints = [
            part
            for part in req.extra_user_content_parts
            if getattr(part, "text", "") == module._ACTIVE_GROUP_REPLY_HINT
        ]
        self.assertEqual(len(hints), 1)
        self.assertIn("当前消息只是抽样触发点", hints[0].text)
        self.assertTrue(hints[0].model_dump_for_context()["_no_save"])
        self.assertEqual(req.extra_user_content_parts[0].text, "existing")

    async def test_wake_and_command_requests_do_not_get_active_reply_hint(self):
        wake_event = FakeEvent()
        wake_event.is_wake = True
        wake_event.is_at_or_wake_command = True
        wake_req = ProviderRequest()

        command_event = FakeEvent()
        command_event.set_extra("handlers_parsed_params", {"plugin.command": {}})
        command_req = ProviderRequest()

        await self.plugin.guide_automatic_group_reply(wake_event, wake_req)
        await self.plugin.guide_automatic_group_reply(command_event, command_req)

        self.assertEqual(wake_req.extra_user_content_parts, [])
        self.assertEqual(command_req.extra_user_content_parts, [])

    async def test_private_request_does_not_get_active_reply_hint(self):
        event = FakeEvent(MessageType.FRIEND_MESSAGE)
        req = ProviderRequest()

        await self.plugin.guide_automatic_group_reply(event, req)

        self.assertEqual(req.extra_user_content_parts, [])

    async def test_agent_begin_removes_late_tool(self):
        req = ProviderRequest()
        req.func_tool = ToolSet(
            [
                FunctionTool(
                    name=module.SEND_MESSAGE_TOOL,
                    description="send",
                    parameters={"type": "object", "properties": {}},
                )
            ]
        )
        event = FakeEvent()
        event.set_extra("_group_reply_guard_active", True)
        event.set_extra("provider_request", req)

        await self.plugin.guard_agent_begin(event, None)

        self.assertIsNone(req.func_tool.get_tool(module.SEND_MESSAGE_TOOL))

    async def test_response_removes_tagged_reasoning(self):
        event = FakeEvent()
        event.set_extra("_group_reply_guard_active", True)
        event.set_extra("_llm_reasoning_content", "internal")
        response = LLMResponse(
            role="assistant",
            completion_text="<think>internal plan</think>实际回复",
        )

        await self.plugin.sanitize_group_response(event, response)

        self.assertEqual(response.completion_text, "实际回复")
        self.assertEqual(event.get_extra("_llm_reasoning_content"), "")
        self.assertTrue(event.get_extra(module._AGENT_FINAL_EXTRA))

    async def test_intermediate_plain_text_is_suppressed_until_agent_finishes(self):
        event = FakeEvent()
        request = ProviderRequest(system_prompt="base")
        await self.plugin.guard_group_request(event, request)

        event.result = SimpleNamespace(chain=[Plain("正在调用工具")])
        await self.plugin.suppress_intermediate_group_text(event)
        self.assertEqual(event.result.chain, [])

        response = LLMResponse(role="assistant", completion_text="最终回复")
        await self.plugin.sanitize_group_response(event, response)
        event.result = SimpleNamespace(chain=[Plain("最终回复")])
        await self.plugin.suppress_intermediate_group_text(event)
        self.assertEqual(event.result.chain[0].text, "最终回复")

    async def test_private_request_is_untouched(self):
        req = ProviderRequest(system_prompt="base")
        req.func_tool = ToolSet(
            [
                FunctionTool(
                    name=module.SEND_MESSAGE_TOOL,
                    description="send",
                    parameters={"type": "object", "properties": {}},
                )
            ]
        )
        event = FakeEvent(MessageType.FRIEND_MESSAGE)

        await self.plugin.guard_group_request(event, req)

        self.assertIsNotNone(req.func_tool.get_tool(module.SEND_MESSAGE_TOOL))
        self.assertTrue(event.platform_meta.support_proactive_message)
        self.assertEqual(req.system_prompt, "base")


if __name__ == "__main__":
    unittest.main()
