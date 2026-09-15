"""指向协议：让模型用标记表达「引用哪条消息」或「@ 谁」。

模型在本轮请求中会收到一张参考表（``mN = 昵称 HH:MM 摘要``）与使用说明；需要
明确指向时在回复开头输出 ``[[quote:mN]]``（引用该消息）或 ``[[at:mN]]``（@ 该消息
发送者），自然接话时不输出标记。插件在发送前解析标记并剥离文本，把标记转换成
平台消息段。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from .message_ledger import LedgerEntry

MARKER_PATTERN = re.compile(r"\[\[\s*(at|quote)\s*:\s*m?(\d+)\s*\]\]", re.IGNORECASE)

POINTING_INSTRUCTIONS = (
    "<pointing_protocol>"
    "下面的指向参考表列出了你本轮能够看到内容的群消息，格式为 `mN = 昵称 时间 摘要`。"
    "需要明确指向某条消息时，在回复的最开头输出一个标记："
    "`[[quote:mN]]` 表示引用该消息（适合回应稍早的消息或需要对方看到上下文时），"
    "`[[at:mN]]` 表示 @ 该消息的发送者（适合点名回应某人）。"
    "自然接话、无需明确指向时不要输出任何标记。"
    "标记只能出现在回复开头，一次最多一个，不要输出其他任何格式标记。"
    "</pointing_protocol>"
)


@dataclass
class PointingMarker:
    """解析出的指向标记。"""

    kind: str
    seq: int


@dataclass
class PointingPlan:
    """指向决策：引用目标、@ 目标，以及剥离标记后的正文。"""

    text: str
    quote_target: LedgerEntry | None = None
    at_target: LedgerEntry | None = None
    dropped_reason: str = field(default="")

    @property
    def has_target(self) -> bool:
        return self.quote_target is not None or self.at_target is not None


def parse_marker(text: str) -> tuple[PointingMarker | None, str]:
    """解析正文开头的指向标记，并剥离全部标记文本。

    只有正文最前面的首个标记参与指向决策，正文中残留的标记一律剥离，避免平台
    收到带标记的原始文本。
    """
    if not isinstance(text, str):
        return None, ""

    stripped = text.lstrip()
    marker: PointingMarker | None = None
    match = MARKER_PATTERN.match(stripped)
    if match:
        marker = PointingMarker(kind=match.group(1).lower(), seq=int(match.group(2)))
        stripped = stripped[match.end() :]

    cleaned = MARKER_PATTERN.sub("", stripped).strip()
    return marker, cleaned


def strip_markers(text: str) -> str:
    """仅剥离标记，不做指向解析。"""
    if not isinstance(text, str):
        return ""
    return MARKER_PATTERN.sub("", text).strip()


def resolve_plan(
    text: str,
    *,
    visible: list[LedgerEntry],
    self_id: str = "",
) -> PointingPlan:
    """把标记解析成具体指向目标。

    标记序号必须落在本轮可见范围内，且目标不是机器人自己的消息，否则丢弃标记，
    避免引用模型看不到内容的消息。
    """
    marker, cleaned = parse_marker(text)
    plan = PointingPlan(text=cleaned)
    if marker is None:
        return plan

    entry = next((item for item in visible if item.seq == marker.seq), None)
    if entry is None:
        plan.dropped_reason = "标记序号不在本轮可见范围内"
        return plan
    if self_id and str(entry.sender_id) == str(self_id):
        plan.dropped_reason = "标记指向机器人自己的消息"
        return plan
    if not entry.message_id:
        plan.dropped_reason = "目标消息缺少 message_id"
        return plan

    if marker.kind == "quote":
        plan.quote_target = entry
    else:
        plan.at_target = entry
    return plan


def render_reference_table(entries: list[LedgerEntry]) -> str:
    """把可见消息渲染成参考表文本。"""
    lines: list[str] = []
    for entry in entries:
        clock = time.strftime("%H:%M", time.localtime(entry.timestamp))
        summary = entry.summary or "(无文本)"
        lines.append(f"m{entry.seq} = {entry.nickname} {clock} {summary}")
    return "\n".join(lines)
