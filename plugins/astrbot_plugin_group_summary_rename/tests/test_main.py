"""群聊总结改名插件的纯函数单元测试。"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from main import (  # noqa: E402
    build_messages_text,
    extract_message_text,
    is_trigger_time,
    parse_llm_output,
    parse_trigger_time,
    sanitize_group_name,
)


class TestExtractMessageText:
    def test_plain_text(self):
        msg = [{"type": "text", "data": {"text": " 你好 "}}]
        assert extract_message_text(msg) == "你好"

    def test_mixed_segments(self):
        msg = [
            {"type": "text", "data": {"text": "看这张图"}},
            {"type": "image", "data": {"url": "http://x"}},
            {"type": "text", "data": {"text": "怎么样"}},
        ]
        assert extract_message_text(msg) == "看这张图 [图片] 怎么样"

    def test_at_and_face(self):
        msg = [
            {"type": "at", "data": {"qq": "12345"}},
            {"type": "face", "data": {"id": "1"}},
        ]
        assert extract_message_text(msg) == "@12345 [表情]"

    def test_string_message(self):
        assert extract_message_text("直接文本") == "直接文本"

    def test_empty_and_invalid(self):
        assert extract_message_text([]) == ""
        assert extract_message_text(None) == ""
        assert extract_message_text("") == ""


class TestBuildMessagesText:
    def test_basic(self):
        messages = [
            {
                "user_id": "111",
                "nickname": "小明",
                "time": 1700000000,
                "message": [{"type": "text", "data": {"text": "晚上好"}}],
            },
            {
                "user_id": "222",
                "nickname": "",
                "time": 1700000060,
                "message": [{"type": "text", "data": {"text": "你好呀"}}],
            },
        ]
        text = build_messages_text(messages)
        lines = text.split("\n")
        assert len(lines) == 2
        assert "小明" in lines[0] and "晚上好" in lines[0]
        assert "222" in lines[1] and "你好呀" in lines[1]

    def test_skips_empty(self):
        messages = [
            {
                "user_id": "111",
                "nickname": "小明",
                "time": 0,
                "message": [{"type": "image", "data": {}}],
            }
        ]
        assert build_messages_text(messages) == ""


class TestParseLlOutput:
    def test_plain_json(self):
        data = parse_llm_output('{"summary": "群聊总结", "name": "新群名"}')
        assert data == {"summary": "群聊总结", "name": "新群名"}

    def test_json_in_markdown(self):
        text = '```json\n{"summary": "总结内容", "name": "测试群"}\n```'
        data = parse_llm_output(text)
        assert data["summary"] == "总结内容"
        assert data["name"] == "测试群"

    def test_json_with_preamble(self):
        text = '好的，以下是结果：\n{"summary": "a", "name": "b"}\n完毕'
        data = parse_llm_output(text)
        assert data["summary"] == "a" and data["name"] == "b"

    def test_invalid(self):
        with pytest.raises(ValueError):
            parse_llm_output("没有 JSON")

    def test_malformed_json(self):
        with pytest.raises(ValueError):
            parse_llm_output('{"summary": "未闭合"')


class TestSanitizeGroupName:
    def test_strip_quotes(self):
        assert sanitize_group_name('"测试群"', 30) == "测试群"
        assert sanitize_group_name("“测试群”", 30) == "测试群"

    def test_remove_newlines(self):
        assert sanitize_group_name("第一行\n第二行", 30) == "第一行 第二行"

    def test_length_limit(self):
        name = sanitize_group_name(
            "这是一个非常长的群名称超过三十个字符限制的测试用例文本", 10
        )
        assert len(name) <= 10

    def test_collapse_spaces(self):
        assert sanitize_group_name("  hello   world  ", 30) == "hello world"

    def test_empty(self):
        assert sanitize_group_name("", 30) == ""


class TestTriggerTime:
    def test_valid(self):
        assert parse_trigger_time("20:00") == "20:00"
        assert parse_trigger_time(" 08:05 ") == "08:05"

    def test_invalid(self):
        for bad in ("25:00", "20:60", "8:00", "abc", "20:0"):
            with pytest.raises(ValueError):
                parse_trigger_time(bad)

    def test_is_trigger_time(self):
        now = datetime(2026, 8, 14, 20, 0, 0)
        assert is_trigger_time(now, "20:00")
        assert not is_trigger_time(now, "20:01")
        assert not is_trigger_time(now, "bad")
