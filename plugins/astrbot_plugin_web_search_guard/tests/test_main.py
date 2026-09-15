from __future__ import annotations

import importlib.util
import sys
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.core.agent.tool import FunctionTool, ToolSet
from mcp.types import CallToolResult, TextContent

MODULE_PATH = Path(__file__).resolve().parents[1] / "main.py"
spec = importlib.util.spec_from_file_location("web_search_guard_main", MODULE_PATH)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

NOW = datetime(2026, 8, 13, 19, 0).astimezone()
RELEASE = {
    "repository": "AstrBotDevs/AstrBot",
    "tag_name": "v4.27.3",
    "published_at": "2026-08-12T17:03:15Z",
    "html_url": "https://github.com/AstrBotDevs/AstrBot/releases/tag/v4.27.3",
    "api_url": "https://api.github.com/repos/AstrBotDevs/AstrBot/releases/latest",
}


class FakeEvent:
    def __init__(self, message_str="原神最新版本是多少"):
        self.message_str = message_str
        self.extras = {}

    def set_extra(self, key, value):
        self.extras[key] = value

    def get_extra(self, key, default=None):
        return self.extras.get(key, default)


class FakeContext:
    def __init__(self, event):
        self.context = SimpleNamespace(event=event)


class FakeSearchTool(FunctionTool):
    def __init__(self, name="web_search_exa", result=None):
        super().__init__(
            name=name,
            description="search",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "start_published_date": {"type": "string"},
                    "end_published_date": {"type": "string"},
                },
            },
        )
        self.result = result or (
            '{"results":[{"title":"官方公告","url":"https://example.com/official",'
            '"snippet":"发布于 2026-08-12"}]}'
        )
        self.received = None

    async def call(self, context, **kwargs):
        self.received = kwargs
        return self.result


class RecencyTests(unittest.TestCase):
    def test_classifies_recency_phrases(self):
        self.assertEqual(module.recency_kind("今天发生了什么"), "today")
        self.assertEqual(module.recency_kind("最近刚发布的版本"), "recent")
        self.assertEqual(module.recency_kind("最新版本是多少"), "current")
        self.assertEqual(module.recency_kind("Python 列表怎么排序"), "")

    def test_as_of_today_is_current_not_single_day(self):
        cases = (
            "严格按今天的日期判断当前最新版本",
            "截至今天的正式版本",
            "as of today, what is the latest release",
        )
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(module.recency_kind(text), "current")

    def test_extracts_years(self):
        self.assertEqual(module.extract_years("2025 与 2026"), {2025, 2026})


class ConstraintTests(unittest.TestCase):
    def test_exa_current_query_replaces_wrong_year_and_adds_dates(self):
        args, changed = module.constrain_search_args(
            "web_search_exa",
            {"query": "原神最新版本 2025年"},
            now=NOW,
            request_kind="current",
            explicit_years=set(),
        )

        self.assertTrue(changed)
        self.assertEqual(args["query"], "原神最新版本 2026年")
        self.assertEqual(
            args["start_published_date"],
            "2026-01-01T00:00:00.000Z",
        )
        self.assertEqual(args["end_published_date"], "2026-08-14T00:00:00.000Z")

    def test_wrong_year_month_is_aligned_to_current_month(self):
        args, _ = module.constrain_search_args(
            "web_search_exa",
            {"query": "DeepSeek 2025年8月 最新消息"},
            now=NOW,
            request_kind="current",
            explicit_years=set(),
        )
        self.assertEqual(args["query"], "DeepSeek 2026年8月 最新消息")

    def test_as_of_today_uses_current_year_scope(self):
        args, _ = module.constrain_search_args(
            "web_search_exa",
            {"query": "AstrBot latest release 2026"},
            now=NOW,
            request_kind=module.recency_kind("严格按今天的日期判断当前最新版"),
            explicit_years=set(),
        )

        self.assertEqual(
            args["start_published_date"],
            "2026-01-01T00:00:00.000Z",
        )
        self.assertEqual(args["end_published_date"], "2026-08-14T00:00:00.000Z")

    def test_explicit_historical_year_is_preserved(self):
        args, _ = module.constrain_search_args(
            "web_search_exa",
            {"query": "回顾 2025 年发布的模型"},
            now=NOW,
            request_kind="current",
            explicit_years={2025},
        )

        self.assertIn("2025", args["query"])
        self.assertNotIn("2026", args["query"])
        self.assertEqual(
            args["start_published_date"],
            "2025-01-01T00:00:00.000Z",
        )
        self.assertEqual(args["end_published_date"], "2026-01-01T00:00:00.000Z")

    def test_explicit_historical_year_replaces_model_added_year(self):
        args, _ = module.constrain_search_args(
            "web_search_exa",
            {"query": "回顾 2024 年发布的模型"},
            now=NOW,
            request_kind="current",
            explicit_years={2025},
        )

        self.assertEqual(args["query"], "回顾 2025 年发布的模型")
        self.assertNotIn("2024", args["query"])

    def test_non_current_query_is_untouched(self):
        original = {"query": "Python 列表排序"}
        args, changed = module.constrain_search_args(
            "web_search_exa",
            original,
            now=NOW,
        )
        self.assertFalse(changed)
        self.assertEqual(args, original)

    def test_supported_provider_filters(self):
        cases = {
            "web_search_tavily": ("start_date", "2026-01-01"),
            "web_search_brave": ("freshness", "year"),
            "web_search_bocha": ("freshness", "oneYear"),
            "web_search_baidu": ("search_recency_filter", "year"),
            "web_search_searxng": ("time_range", "year"),
        }
        for tool_name, (key, expected) in cases.items():
            with self.subTest(tool_name=tool_name):
                args, _ = module.constrain_search_args(
                    tool_name,
                    {"query": "最新消息"},
                    now=NOW,
                )
                self.assertEqual(args[key], expected)
                self.assertIn("2026", args["query"])


class ResultTests(unittest.TestCase):
    def test_extracts_urls_from_json_and_call_result(self):
        payload = '{"results":[{"url":"https://a.example/x"}]}'
        self.assertEqual(module.extract_source_urls(payload), ["https://a.example/x"])

        result = CallToolResult(
            content=[TextContent(type="text", text="URL: https://b.example/y")]
        )
        self.assertEqual(module.extract_source_urls(result), ["https://b.example/y"])

    def test_extracts_and_selects_matching_github_release_repository(self):
        urls = [
            "https://github.com/Other/Project/releases/tag/v9.9.9",
            "https://newreleases.io/project/github/AstrBotDevs/AstrBot/release/v4.24.2",
            "https://github.com/AstrBotDevs/AstrBot/releases/tag/v4.24.1",
            "https://github.com/AstrBotDevs/AstrBot/issues/1",
            "https://newreleases.io/project/gitlab/Evil/Project/release/v1.0.0",
            "https://example.com/project/github/Fake/Repo/release/v1.0.0",
        ]
        repositories = module.extract_github_release_repositories(urls)

        self.assertEqual(repositories, ["Other/Project", "AstrBotDevs/AstrBot"])
        self.assertEqual(
            module.select_github_release_repository(
                "AstrBot latest stable version 2026",
                repositories,
            ),
            "AstrBotDevs/AstrBot",
        )
        self.assertIsNone(
            module.select_github_release_repository("unknown latest release", repositories)
        )

    def test_current_release_detection_skips_historical_scope(self):
        self.assertTrue(
            module.is_current_release_request(
                "AstrBot latest stable release 2026",
                now=NOW,
                explicit_years=set(),
            )
        )
        self.assertFalse(
            module.is_current_release_request(
                "AstrBot 2025 年最新版",
                now=NOW,
                explicit_years={2025},
            )
        )

    def test_official_release_correction_replaces_wrong_version_block(self):
        text = (
            "暗号是霜糖-E2E-813-K4M2\n\n"
            "AstrBot 最新正式版是 v4.24.2。\n"
            "来源：https://github.com/AstrBotDevs/AstrBot/releases/tag/v4.24.2"
        )

        corrected = module.apply_official_release_correction(
            text,
            RELEASE,
            now=NOW,
        )

        self.assertIn("暗号是霜糖-E2E-813-K4M2", corrected)
        self.assertIn("v4.27.3", corrected)
        self.assertIn(RELEASE["html_url"], corrected)
        self.assertNotIn("v4.24.2", corrected)

    def test_official_release_correction_removes_conflict_even_if_tag_is_present(self):
        text = "AstrBot 最新版可能是 v4.24.2，也有人提到 v4.27.3。"

        corrected = module.apply_official_release_correction(
            text,
            RELEASE,
            now=NOW,
        )

        self.assertIn("v4.27.3", corrected)
        self.assertNotIn("v4.24.2", corrected)

    def test_stale_result_note_rejects_old_only_evidence(self):
        note = module.build_result_guard_note(
            now=NOW,
            query="最新版本 2026",
            kind="current",
            result="发布于 2025-07-30",
        )
        self.assertIn("do not establish current status", note)
        self.assertIn("2026-08-13", note)


class GuardedToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_proxy_constrains_args_and_appends_result_note(self):
        original = FakeSearchTool()
        wrapped = module.GuardedSearchTool(
            original,
            now=NOW,
            request_kind="current",
            explicit_years=set(),
            recent_days=90,
            max_source_urls=2,
        )
        event = FakeEvent()

        result = await wrapped.call(
            FakeContext(event),
            query="原神最新版本 2025年",
        )

        self.assertEqual(original.received["query"], "原神最新版本 2026年")
        self.assertIn("<web_search_guard>", result)
        self.assertEqual(
            event.get_extra(module._SOURCE_URLS_EXTRA),
            ["https://example.com/official"],
        )

    async def test_proxy_verifies_official_github_latest_release(self):
        indexed = (
            '{"results":[{"title":"v4.24.2",'
            '"url":"https://github.com/AstrBotDevs/AstrBot/releases/tag/v4.24.2",'
            '"snippet":"Published 2026-05-03"}]}'
        )
        original = FakeSearchTool(result=indexed)
        wrapped = module.GuardedSearchTool(
            original,
            now=NOW,
            request_kind="current",
            explicit_years=set(),
            recent_days=90,
            max_source_urls=2,
        )
        event = FakeEvent("AstrBot 当前最新正式版本")

        with patch.object(
            module,
            "fetch_github_latest_release",
            AsyncMock(return_value=dict(RELEASE)),
        ) as fetch:
            module._GITHUB_RELEASE_PROCESS_CACHE.clear()
            output = await wrapped.call(
                FakeContext(event),
                query="AstrBot latest stable release 2026",
            )
            again = await wrapped.call(
                FakeContext(event),
                query="AstrBot latest release 2026",
            )

        fetch.assert_awaited_once_with("AstrBotDevs/AstrBot")
        self.assertIn("<official_release_verification>", output)
        self.assertIn("v4.27.3", output)
        self.assertIn("v4.27.3", again)
        self.assertEqual(
            event.get_extra(module._SOURCE_URLS_EXTRA)[0],
            RELEASE["html_url"],
        )
        self.assertEqual(
            event.get_extra(module._OFFICIAL_RELEASES_EXTRA)[0]["tag_name"],
            "v4.27.3",
        )

    async def test_proxy_verifies_newreleases_github_mirror_result(self):
        indexed = (
            '{"results":[{"title":"AstrBotDevs/AstrBot v4.27.2 on GitHub",'
            '"url":"https://newreleases.io/project/github/'
            'AstrBotDevs/AstrBot/release/v4.27.2",'
            '"snippet":"2 hours ago; 4.27.2 - 2026-08-05"}]}'
        )
        wrapped = module.GuardedSearchTool(
            FakeSearchTool(result=indexed),
            now=NOW,
            request_kind="current",
            explicit_years=set(),
            recent_days=90,
            max_source_urls=2,
        )
        event = FakeEvent("AstrBot 当前最新正式版本")

        with patch.object(
            module,
            "fetch_github_latest_release",
            AsyncMock(return_value=dict(RELEASE)),
        ) as fetch:
            module._GITHUB_RELEASE_PROCESS_CACHE.clear()
            output = await wrapped.call(
                FakeContext(event),
                query="AstrBot latest official release 2026",
            )

        fetch.assert_awaited_once_with("AstrBotDevs/AstrBot")
        self.assertIn("<official_release_verification>", output)
        self.assertIn("v4.27.3", output)
        self.assertEqual(
            event.get_extra(module._SOURCE_URLS_EXTRA)[0],
            RELEASE["html_url"],
        )

    async def test_failed_official_verification_can_retry(self):
        event = FakeEvent("AstrBot 当前最新正式版本")
        module._GITHUB_RELEASE_PROCESS_CACHE.clear()
        fetch = AsyncMock(side_effect=[None, dict(RELEASE)])

        with patch.object(module, "fetch_github_latest_release", fetch):
            first = await module.get_official_github_release(
                event,
                "AstrBotDevs/AstrBot",
            )
            second = await module.get_official_github_release(
                event,
                "AstrBotDevs/AstrBot",
            )

        self.assertIsNone(first)
        self.assertEqual(second["tag_name"], "v4.27.3")
        self.assertEqual(fetch.await_count, 2)

    async def test_proxy_does_not_verify_historical_release(self):
        indexed = (
            '{"results":[{"url":'
            '"https://github.com/AstrBotDevs/AstrBot/releases/tag/v4.20.0"}]}'
        )
        original = FakeSearchTool(result=indexed)
        wrapped = module.GuardedSearchTool(
            original,
            now=NOW,
            request_kind="current",
            explicit_years={2025},
            recent_days=90,
            max_source_urls=2,
        )

        with patch.object(
            module,
            "fetch_github_latest_release",
            AsyncMock(return_value=dict(RELEASE)),
        ) as fetch:
            await wrapped.call(
                FakeContext(FakeEvent()),
                query="AstrBot 2025 年最新版本",
            )

        fetch.assert_not_awaited()

    async def test_proxy_handles_call_tool_result(self):
        result = CallToolResult(
            content=[TextContent(type="text", text="URL: https://example.com/x")]
        )
        original = FakeSearchTool(result=result)
        wrapped = module.GuardedSearchTool(
            original,
            now=NOW,
            request_kind="current",
            explicit_years=set(),
            recent_days=90,
            max_source_urls=2,
        )

        output = await wrapped.call(
            FakeContext(FakeEvent()),
            query="最新信息",
        )

        self.assertIs(output, result)
        self.assertIn("<web_search_guard>", output.content[-1].text)


class PluginHookTests(unittest.IsolatedAsyncioTestCase):
    def make_plugin(self, enabled=True):
        plugin = object.__new__(module.WebSearchGuard)
        plugin.enabled = enabled
        plugin.recent_days = 90
        plugin.max_source_urls = 2
        return plugin

    async def test_request_hook_wraps_search_tool_and_adds_temp_runtime(self):
        plugin = self.make_plugin()
        req = ProviderRequest(
            prompt="原神最新版本是多少",
            system_prompt="base",
            func_tool=ToolSet(
                [
                    FakeSearchTool(),
                    FunctionTool(
                        name="other_tool",
                        description="other",
                        parameters={"type": "object", "properties": {}},
                    ),
                ]
            ),
        )
        event = FakeEvent()

        await plugin.guard_search_request(event, req)

        self.assertIsInstance(
            req.func_tool.get_tool("web_search_exa"),
            module.GuardedSearchTool,
        )
        self.assertNotIsInstance(
            req.func_tool.get_tool("other_tool"),
            module.GuardedSearchTool,
        )
        self.assertIn(module.SEARCH_RULE, req.system_prompt)
        self.assertEqual(len(req.extra_user_content_parts), 1)
        self.assertTrue(
            req.extra_user_content_parts[0].model_dump_for_context()["_no_save"]
        )

    async def test_request_hook_treats_as_of_today_as_current(self):
        plugin = self.make_plugin()
        original = FakeSearchTool()
        req = ProviderRequest(
            prompt="严格按今天的日期判断 AstrBot 当前最新版本",
            system_prompt="base",
            func_tool=ToolSet([original]),
        )
        event = FakeEvent(req.prompt)

        await plugin.guard_search_request(event, req)
        wrapped = req.func_tool.get_tool("web_search_exa")
        await wrapped.call(FakeContext(event), query="AstrBot latest release 2026")

        self.assertEqual(
            original.received["start_published_date"],
            "2026-01-01T00:00:00.000Z",
        )

    async def test_request_without_search_tool_is_untouched(self):
        plugin = self.make_plugin()
        req = ProviderRequest(
            prompt="普通问题",
            system_prompt="base",
            func_tool=ToolSet(
                [
                    FunctionTool(
                        name="other_tool",
                        description="other",
                        parameters={"type": "object", "properties": {}},
                    )
                ]
            ),
        )
        event = FakeEvent("普通问题")

        await plugin.guard_search_request(event, req)

        self.assertEqual(req.system_prompt, "base")
        self.assertEqual(req.extra_user_content_parts, [])

    async def test_tool_hook_updates_late_search_args_in_place(self):
        plugin = self.make_plugin()
        event = FakeEvent()
        event.set_extra(
            module._POLICY_EXTRA,
            {"now": NOW, "request_kind": "current", "explicit_years": set()},
        )
        args = {"query": "原神最新版本 2025年"}

        await plugin.constrain_late_search_call(
            event,
            FakeSearchTool(),
            args,
        )

        self.assertEqual(args["query"], "原神最新版本 2026年")
        self.assertIn("start_published_date", args)

    async def test_response_corrects_wrong_release_from_official_evidence(self):
        plugin = self.make_plugin()
        event = FakeEvent()
        event.set_extra(
            module._POLICY_EXTRA,
            {"now": NOW, "request_kind": "current", "explicit_years": set()},
        )
        event.set_extra(module._OFFICIAL_RELEASES_EXTRA, [dict(RELEASE)])
        event.set_extra(module._SOURCE_URLS_EXTRA, [RELEASE["html_url"]])
        response = LLMResponse(
            role="assistant",
            completion_text=(
                "暗号是霜糖-E2E-813-K4M2\n\n"
                "AstrBot 最新正式版是 v4.24.2。\n"
                "来源：https://github.com/AstrBotDevs/AstrBot/releases/tag/v4.24.2"
            ),
        )

        await plugin.ensure_source_urls(event, response)

        self.assertIn("暗号是霜糖-E2E-813-K4M2", response.completion_text)
        self.assertIn("v4.27.3", response.completion_text)
        self.assertNotIn("v4.24.2", response.completion_text)
        self.assertIn(RELEASE["html_url"], response.completion_text)

    async def test_response_appends_real_urls_only_when_missing(self):
        plugin = self.make_plugin()
        event = FakeEvent()
        event.set_extra(
            module._SOURCE_URLS_EXTRA,
            ["https://a.example", "https://b.example"],
        )
        response = LLMResponse(role="assistant", completion_text="结论")

        await plugin.ensure_source_urls(event, response)

        self.assertIn("来源：", response.completion_text)
        self.assertIn("https://a.example", response.completion_text)
        self.assertIn("https://b.example", response.completion_text)


if __name__ == "__main__":
    unittest.main()
