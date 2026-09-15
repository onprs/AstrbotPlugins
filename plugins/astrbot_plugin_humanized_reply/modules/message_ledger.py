"""群消息台账：保存群消息的 message_id、发送者、时间与文本摘要。

台账为拟人化指向提供两类信息：

- 模型侧：本轮 LLM 能看到内容的群消息索引（``mN`` 序号、昵称、时间、摘要）。
- 插件侧：序号到 ``message_id`` 与发送者的映射，用于在发送前构造引用或 @ 组件。

可见范围与 AstrBot 群上下文的注入行为对齐：核心会把「上次 LLM 请求之后」
累积的群消息作为上下文注入，注入后即清空，因此模型每轮只能看到这一窗口内
的消息。台账以同样的窗口对外提供引用目标，避免模型指向它看不到内容的消息。
"""

from __future__ import annotations

import random
import sqlite3
import threading
import time
from collections import defaultdict, deque
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SUMMARY_MAX_LENGTH = 40
DEFAULT_MAX_MESSAGES_PER_GROUP = 200
DEFAULT_FLUSH_INTERVAL_SECONDS = 30.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS group_messages (
    group_id   TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    message_id TEXT NOT NULL,
    sender_id  TEXT NOT NULL,
    nickname   TEXT NOT NULL,
    timestamp  REAL NOT NULL,
    summary    TEXT NOT NULL,
    PRIMARY KEY (group_id, seq)
);
"""


@dataclass(frozen=True)
class LedgerEntry:
    """台账中的一条群消息。"""

    seq: int
    message_id: str
    group_id: str
    sender_id: str
    nickname: str
    timestamp: float
    summary: str


def summarize_chain(chain: list[Any], limit: int = SUMMARY_MAX_LENGTH) -> str:
    """把消息链压缩成一行摘要，用于给模型提供定位线索。"""
    parts: list[str] = []
    for comp in chain or []:
        name = type(comp).__name__
        if name == "Plain":
            text = getattr(comp, "text", "") or ""
            if text.strip():
                parts.append(text.strip())
        elif name == "Image":
            parts.append("[图片]")
        elif name == "Record":
            parts.append("[语音]")
        elif name == "Video":
            parts.append("[视频]")
        elif name == "File":
            parts.append("[文件]")
        elif name == "Json":
            parts.append("[卡片]")
        elif name == "Face":
            parts.append("[表情]")
        elif name == "At":
            # 适配器可能拿不到群昵称，此时用 QQ 号保证摘要不丢信息。
            label = getattr(comp, "name", "") or getattr(comp, "qq", "")
            parts.append(f"@{label}")
        elif name == "Reply":
            parts.append("[引用]")
    summary = " ".join(" ".join(parts).split())
    if len(summary) > limit:
        summary = summary[:limit] + "…"
    return summary


class MessageLedger:
    """按群维护的群消息台账。"""

    def __init__(
        self,
        *,
        data_path: Path | None = None,
        max_messages_per_group: int = DEFAULT_MAX_MESSAGES_PER_GROUP,
        persist: bool = True,
        flush_interval_seconds: float = DEFAULT_FLUSH_INTERVAL_SECONDS,
    ) -> None:
        self.max_messages_per_group = max(1, int(max_messages_per_group))
        self.persist = bool(persist)
        self.flush_interval_seconds = max(1.0, float(flush_interval_seconds))
        self.data_path = data_path
        self._entries: dict[str, deque[LedgerEntry]] = defaultdict(deque)
        # 每群已作为上下文提供给模型的最后一条消息序号。
        self._anchors: dict[str, int] = {}
        self._next_seq: dict[str, int] = defaultdict(lambda: 1)
        self._dirty = False
        self._last_flush_at = time.monotonic()
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- 持久化

    def load(self) -> bool:
        """从磁盘载入台账，返回是否成功。"""
        if not self.persist or self.data_path is None:
            return True
        if not self.data_path.exists():
            return True
        try:
            with closing(sqlite3.connect(self.data_path)) as conn, conn:
                conn.executescript(_SCHEMA)
                cur = conn.execute(
                    "SELECT group_id, seq, message_id, sender_id, nickname, "
                    "timestamp, summary FROM group_messages ORDER BY group_id, seq",
                )
                for row in cur.fetchall():
                    group_id, seq, message_id, sender_id, nickname, ts, summary = row
                    self._entries[str(group_id)].append(
                        LedgerEntry(
                            seq=int(seq),
                            message_id=str(message_id),
                            group_id=str(group_id),
                            sender_id=str(sender_id),
                            nickname=str(nickname),
                            timestamp=float(ts),
                            summary=str(summary),
                        ),
                    )
                    self._next_seq[str(group_id)] = max(
                        self._next_seq[str(group_id)],
                        int(seq) + 1,
                    )
            for group_id in list(self._entries):
                self._trim(group_id)
                # 重启后核心的群上下文为空，已载入的历史消息不能作为引用目标。
                entries = self._entries[group_id]
                if entries:
                    self._anchors[group_id] = entries[-1].seq
            return True
        except sqlite3.Error:
            return False

    def flush(self, force: bool = False) -> None:
        """把台账批量落盘。"""
        if not self.persist or self.data_path is None:
            return
        with self._lock:
            now = time.monotonic()
            if not force:
                if not self._dirty or now - self._last_flush_at < self.flush_interval_seconds:
                    return
            if not self._dirty:
                return
            self._last_flush_at = now
            self._dirty = False
            rows = [
                (
                    entry.group_id,
                    entry.seq,
                    entry.message_id,
                    entry.sender_id,
                    entry.nickname,
                    entry.timestamp,
                    entry.summary,
                )
                for entries in self._entries.values()
                for entry in entries
            ]

        try:
            self.data_path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.data_path)) as conn, conn:
                conn.executescript(_SCHEMA)
                conn.execute("DELETE FROM group_messages")
                if rows:
                    conn.executemany(
                        "INSERT OR REPLACE INTO group_messages (group_id, seq, "
                        "message_id, sender_id, nickname, timestamp, summary) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        rows,
                    )
                conn.commit()
        except (sqlite3.Error, OSError):
            # 落盘失败不影响发送流程，下个周期重试。
            with self._lock:
                self._dirty = True

    def close(self) -> None:
        """退出前把剩余记录写入磁盘。"""
        self.flush(force=True)

    # ---------------------------------------------------------------- 写入

    def record(
        self,
        *,
        group_id: str,
        message_id: str,
        sender_id: str,
        nickname: str,
        summary: str,
        timestamp: float | None = None,
    ) -> LedgerEntry | None:
        """记录一条群消息，返回台账条目。"""
        if not group_id or not message_id:
            return None
        with self._lock:
            entries = self._entries[group_id]
            if entries and entries[-1].message_id == str(message_id):
                return entries[-1]
            seq = self._next_seq[group_id]
            self._next_seq[group_id] = seq + 1
            entry = LedgerEntry(
                seq=seq,
                message_id=str(message_id),
                group_id=str(group_id),
                sender_id=str(sender_id),
                nickname=nickname or "",
                timestamp=float(timestamp if timestamp is not None else time.time()),
                summary=summary,
            )
            entries.append(entry)
            self._dirty = True
            self._trim_locked(group_id)
        return entry

    def advance_visible_anchor(self, group_id: str, upto_seq: int) -> None:
        """把可见范围锚点推进到指定序号。

        核心在每次 LLM 请求时把累积的群消息注入上下文并清空，因此锚点也在请求时
        推进，保证参考表列出的消息与模型在本轮上下文中真正看到的消息一致。
        """
        if not group_id or upto_seq <= 0:
            return
        with self._lock:
            current = self._anchors.get(group_id)
            if current is None or upto_seq > current:
                self._anchors[group_id] = upto_seq

    def _trim(self, group_id: str) -> None:
        with self._lock:
            self._trim_locked(group_id)

    def _trim_locked(self, group_id: str) -> None:
        entries = self._entries.get(group_id)
        if entries is None:
            return
        while len(entries) > self.max_messages_per_group:
            entries.popleft()

    # ---------------------------------------------------------------- 读取

    def visible_window(self, group_id: str, limit: int = 30) -> list[LedgerEntry]:
        """返回模型本轮能看到内容的群消息。

        即锚点之后累积的消息，最后一条为当前触发消息。``limit`` 为正数时只保留
        最近的若干条，其余消息虽在模型上下文中，但索引过长会干扰模型。
        """
        entries = list(self._entries.get(group_id, ()))
        if not entries:
            return []
        anchor = self._anchors.get(group_id)
        if anchor is not None:
            entries = [entry for entry in entries if entry.seq > anchor]
        if limit > 0:
            entries = entries[-limit:]
        return entries

    def target(self, group_id: str, seq: int) -> LedgerEntry | None:
        """按序号查找条目。"""
        for entry in self._entries.get(group_id, ()):
            if entry.seq == seq:
                return entry
        return None

    def stats(self, group_id: str) -> dict[str, int]:
        """返回台账状态，用于日志与自检。"""
        entries = list(self._entries.get(group_id, ()))
        return {
            "count": len(entries),
            "anchor": self._anchors.get(group_id, 0),
            "visible": len(self.visible_window(group_id, limit=0)),
        }


def pick_weighted(
    candidates: list[LedgerEntry],
    *,
    max_age_seconds: float,
    exclude_message_id: str = "",
    now: float | None = None,
) -> LedgerEntry | None:
    """在候选消息中抽取一条引用目标，越新的消息权重越高。"""
    current = time.time() if now is None else now
    eligible = [
        entry
        for entry in candidates
        if entry.sender_id
        and entry.message_id
        and entry.message_id != exclude_message_id
        and current - entry.timestamp <= max_age_seconds
    ]
    if not eligible:
        return None
    # 指数权重：越新权重越高。
    weights = [0.5 ** (len(eligible) - idx) for idx in range(len(eligible))]
    threshold = random.random() * sum(weights)
    acc = 0.0
    for entry, weight in zip(eligible, weights, strict=False):
        acc += weight
        if threshold <= acc:
            return entry
    return eligible[-1]


def latest_member(
    candidates: list[LedgerEntry],
    exclude: str = "",
) -> LedgerEntry | None:
    """在候选消息中找出最近一条来自其他成员的消息。"""
    for entry in reversed(candidates):
        if not entry.sender_id or (exclude and entry.sender_id == exclude):
            continue
        return entry
    return None
