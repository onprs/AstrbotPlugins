from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import math
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import url2pathname

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
import astrbot.api.message_components as Comp
from astrbot.api.star import Context, Star


PLUGIN_NAME = "astrbot_plugin_server_monitor"
SUPPORTED_PLATFORMS = {"aiocqhttp"}


DASHBOARD_TEMPLATE = """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <style>
    * {
      box-sizing: border-box;
    }
    html,
    body {
      width: {{ layout.width }}px;
      min-height: {{ layout.height }}px;
      margin: 0;
      overflow: hidden;
      background: #03050a;
      color: #f6f7fb;
      font-family: "Inter", "SF Pro Display", "Segoe UI", "Noto Sans SC", "Microsoft YaHei", Arial, sans-serif;
      letter-spacing: 0;
    }
    .dashboard {
      position: relative;
      width: {{ layout.width }}px;
      min-height: {{ layout.height }}px;
      padding: 48px 64px 64px;
      background:
        radial-gradient(ellipse 80% 50% at 20% -10%, rgba(54, 245, 198, 0.07) 0%, transparent 60%),
        radial-gradient(ellipse 60% 40% at 85% 10%, rgba(76, 201, 240, 0.06) 0%, transparent 55%),
        radial-gradient(ellipse 50% 30% at 50% 100%, rgba(139, 92, 246, 0.04) 0%, transparent 50%),
        linear-gradient(180deg, rgba(255, 255, 255, 0.02) 0, rgba(255, 255, 255, 0) 480px),
        linear-gradient(160deg, #03050a 0%, #080c14 40%, #0d1220 100%);
      isolation: isolate;
    }
    .dashboard::before {
      content: "";
      position: absolute;
      inset: 0;
      z-index: -1;
      opacity: 0.18;
      background-image:
        linear-gradient(rgba(148, 163, 184, 0.10) 1px, transparent 1px),
        linear-gradient(90deg, rgba(148, 163, 184, 0.08) 1px, transparent 1px);
      background-size: 40px 40px;
      mask-image: linear-gradient(180deg, black 0%, transparent 80%);
    }
    .topbar {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 36px;
      align-items: start;
      margin-bottom: 32px;
    }
    .kicker {
      display: inline-flex;
      align-items: center;
      gap: 10px;
      height: 30px;
      padding: 0 10px;
      border: 1px solid rgba(76, 201, 240, 0.32);
      border-radius: 6px;
      background: rgba(6, 182, 212, 0.10);
      color: #8ee7f5;
      font-size: 15px;
      font-weight: 700;
      text-transform: uppercase;
    }
    .kicker-mark {
      width: 8px;
      height: 8px;
      border-radius: 2px;
      background: #36f5c6;
      box-shadow: 0 0 18px rgba(54, 245, 198, 0.68);
    }
    h1 {
      margin: 16px 0 9px;
      max-width: 920px;
      overflow-wrap: anywhere;
      color: #ffffff;
      font-size: 52px;
      line-height: 1.04;
      font-weight: 820;
      letter-spacing: 0;
    }
    .subtitle {
      margin: 0;
      color: #98a6b7;
      font-size: 22px;
      line-height: 1.34;
    }
    .summary-grid {
      display: grid;
      grid-template-columns: repeat(4, 160px);
      gap: 12px;
    }
    .summary-item {
      min-height: 92px;
      padding: 14px 16px;
      border: 1px solid rgba(148, 163, 184, 0.18);
      border-radius: 12px;
      background:
        linear-gradient(180deg, rgba(255, 255, 255, 0.08) 0%, rgba(255, 255, 255, 0.02) 100%),
        rgba(12, 20, 38, 0.45);
      box-shadow:
        0 8px 32px rgba(0, 0, 0, 0.25),
        inset 0 1px 0 rgba(255, 255, 255, 0.12),
        inset 0 -1px 0 rgba(0, 0, 0, 0.15);
      backdrop-filter: blur(20px) saturate(150%);
      position: relative;
      overflow: hidden;
    }
    .summary-item::after {
      content: "";
      position: absolute;
      inset: 1px;
      border-radius: 11px;
      background: linear-gradient(180deg, rgba(255, 255, 255, 0.06) 0%, rgba(255, 255, 255, 0) 40%);
      pointer-events: none;
    }
    .summary-label {
      color: #8896a8;
      font-size: 14px;
      font-weight: 700;
      text-transform: uppercase;
    }
    .summary-value {
      margin-top: 9px;
      color: #ffffff;
      font-size: 34px;
      line-height: 1;
      font-weight: 800;
    }
    .summary-value.ok {
      color: #5df2b5;
    }
    .summary-value.warn {
      color: #f6c55d;
    }
    .summary-value.bad {
      color: #ff6b82;
    }
    .server-grid {
      display: grid;
      grid-template-columns: repeat({{ layout.columns }}, minmax(0, 1fr));
      gap: {{ layout.gap }}px;
    }
    .server-card {
      position: relative;
      height: {{ layout.card_height }}px;
      padding: 22px;
      border: 1px solid rgba(148, 163, 184, 0.16);
      border-radius: 14px;
      background:
        linear-gradient(165deg, rgba(255, 255, 255, 0.07) 0%, rgba(255, 255, 255, 0.015) 45%, rgba(255, 255, 255, 0) 100%),
        rgba(10, 16, 30, 0.42);
      box-shadow:
        0 20px 60px rgba(0, 0, 0, 0.35),
        inset 0 1px 0 rgba(255, 255, 255, 0.14),
        inset 0 -1px 0 rgba(0, 0, 0, 0.2);
      backdrop-filter: blur(24px) saturate(160%);
      overflow: hidden;
    }
    .server-card.online {
      border-color: rgba(54, 245, 198, 0.18);
      box-shadow:
        0 20px 60px rgba(0, 0, 0, 0.35),
        0 0 40px rgba(54, 245, 198, 0.06),
        inset 0 1px 0 rgba(255, 255, 255, 0.14),
        inset 0 -1px 0 rgba(0, 0, 0, 0.2);
    }
    .server-card.offline {
      border-color: rgba(255, 107, 130, 0.18);
      box-shadow:
        0 20px 60px rgba(0, 0, 0, 0.35),
        0 0 40px rgba(255, 107, 130, 0.06),
        inset 0 1px 0 rgba(255, 255, 255, 0.14),
        inset 0 -1px 0 rgba(0, 0, 0, 0.2);
    }
    .server-card.degraded {
      border-color: rgba(246, 197, 93, 0.18);
      box-shadow:
        0 20px 60px rgba(0, 0, 0, 0.35),
        0 0 40px rgba(246, 197, 93, 0.06),
        inset 0 1px 0 rgba(255, 255, 255, 0.14),
        inset 0 -1px 0 rgba(0, 0, 0, 0.2);
    }
    .server-card::before {
      content: "";
      position: absolute;
      inset: 1px 1px auto 1px;
      height: 100px;
      border-radius: 13px 13px 0 0;
      background: linear-gradient(180deg, rgba(255, 255, 255, 0.08) 0%, rgba(255, 255, 255, 0) 75%);
      pointer-events: none;
    }
    .server-card::after {
      content: "";
      position: absolute;
      inset: 0;
      border-radius: 14px;
      background: linear-gradient(135deg, rgba(255, 255, 255, 0.04) 0%, rgba(255, 255, 255, 0) 50%);
      pointer-events: none;
    }
    .server-head {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 14px;
      align-items: start;
    }
    .server-name {
      margin: 0;
      overflow: hidden;
      color: #ffffff;
      font-size: 26px;
      line-height: 1.12;
      font-weight: 800;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .server-remark {
      margin-top: 7px;
      min-height: 22px;
      overflow: hidden;
      color: #92a0b3;
      font-size: 16px;
      line-height: 1.25;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .status {
      min-width: 86px;
      padding: 8px 12px;
      border-radius: 8px;
      color: #06110d;
      font-size: 13px;
      font-weight: 820;
      text-align: center;
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }
    .status.online {
      background: linear-gradient(135deg, #59f0b0 0%, #36d399 100%);
      box-shadow: 0 0 24px rgba(89, 240, 176, 0.35), inset 0 1px 0 rgba(255, 255, 255, 0.25);
    }
    .status.offline {
      background: linear-gradient(135deg, #ff6b82 0%, #ff4757 100%);
      color: #21060b;
      box-shadow: 0 0 24px rgba(255, 107, 130, 0.30), inset 0 1px 0 rgba(255, 255, 255, 0.25);
    }
    .status.degraded {
      background: linear-gradient(135deg, #f6c55d 0%, #f0b429 100%);
      color: #1c1203;
      box-shadow: 0 0 24px rgba(246, 197, 93, 0.32), inset 0 1px 0 rgba(255, 255, 255, 0.25);
    }
    .meta {
      display: grid;
      grid-template-columns: minmax(0, 1fr);
      gap: 7px;
      margin: 16px 0 18px;
      color: #a9b5c4;
      font-size: 15px;
      line-height: 1.24;
    }
    .meta-line {
      display: grid;
      grid-template-columns: 76px minmax(0, 1fr);
      gap: 10px;
      min-width: 0;
    }
    .meta-label {
      color: #68778b;
      font-weight: 760;
    }
    .meta-value {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .metrics {
      display: grid;
      grid-template-columns: 118px minmax(0, 1fr);
      gap: 19px;
      align-items: center;
    }
    .cpu-ring {
      display: grid;
      place-items: center;
      width: 118px;
      height: 118px;
      border-radius: 50%;
      background: #080c12;
      box-shadow:
        inset 0 0 0 10px rgba(6, 10, 16, 0.95),
        0 8px 24px rgba(0, 0, 0, 0.35),
        0 0 0 1px rgba(255, 255, 255, 0.06);
      position: relative;
    }
    .cpu-ring::after {
      content: "";
      position: absolute;
      inset: 10px;
      border-radius: 50%;
      box-shadow: 0 0 20px var(--cpu-color, rgba(54, 245, 198, 0.15));
      pointer-events: none;
    }
    .cpu-inner {
      display: grid;
      place-items: center;
      width: 78px;
      height: 78px;
      border-radius: 50%;
      background: linear-gradient(180deg, #0d1219 0%, #0a0e14 100%);
      color: #ffffff;
      font-size: 24px;
      font-weight: 850;
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.08);
    }
    .bar-stack {
      display: grid;
      gap: 14px;
    }
    .bar-row {
      min-width: 0;
    }
    .bar-top {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 12px;
      margin-bottom: 8px;
      color: #c4ccd8;
      font-size: 15px;
      font-weight: 740;
    }
    .bar-label {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .bar-value {
      color: #f5f7fb;
    }
    .bar-track {
      height: 10px;
      border-radius: 5px;
      background: rgba(148, 163, 184, 0.12);
      overflow: hidden;
      box-shadow: inset 0 1px 2px rgba(0, 0, 0, 0.25);
    }
    .bar-fill {
      height: 100%;
      border-radius: 5px;
      background: linear-gradient(90deg, #36f5c6 0%, #4cc9f0 100%);
      box-shadow: 0 0 12px rgba(54, 245, 198, 0.25);
      position: relative;
    }
    .bar-fill::after {
      content: "";
      position: absolute;
      inset: 0;
      background: linear-gradient(180deg, rgba(255, 255, 255, 0.25) 0%, rgba(255, 255, 255, 0) 60%);
      border-radius: 5px;
    }
    .bar-fill.storage {
      background: linear-gradient(90deg, #8bde66 0%, #f6c55d 100%);
      box-shadow: 0 0 12px rgba(139, 222, 102, 0.25);
    }
    .io-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
      margin-top: 20px;
    }
    .io-cell {
      min-height: 70px;
      padding: 12px 14px;
      border: 1px solid rgba(148, 163, 184, 0.12);
      border-radius: 10px;
      background:
        linear-gradient(180deg, rgba(255, 255, 255, 0.05) 0%, rgba(255, 255, 255, 0.015) 100%),
        rgba(12, 20, 38, 0.35);
      box-shadow:
        inset 0 1px 0 rgba(255, 255, 255, 0.08),
        inset 0 -1px 0 rgba(0, 0, 0, 0.1);
      position: relative;
      overflow: hidden;
    }
    .io-cell::after {
      content: "";
      position: absolute;
      inset: 1px;
      border-radius: 9px;
      background: linear-gradient(180deg, rgba(255, 255, 255, 0.04) 0%, rgba(255, 255, 255, 0) 50%);
      pointer-events: none;
    }
    .io-label {
      color: #728196;
      font-size: 13px;
      font-weight: 800;
      text-transform: uppercase;
    }
    .io-value {
      margin-top: 8px;
      overflow: hidden;
      color: #ffffff;
      font-size: 22px;
      line-height: 1;
      font-weight: 780;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .error {
      margin-top: 14px;
      padding: 12px 16px;
      border: 1px solid rgba(255, 107, 130, 0.25);
      border-radius: 10px;
      background:
        linear-gradient(180deg, rgba(255, 107, 130, 0.08) 0%, rgba(255, 107, 130, 0.03) 100%),
        rgba(20, 10, 14, 0.35);
      color: #ffb1be;
      font-size: 15px;
      line-height: 1.28;
      max-height: 58px;
      overflow: hidden;
      box-shadow:
        inset 0 1px 0 rgba(255, 255, 255, 0.06),
        0 4px 16px rgba(255, 107, 130, 0.08);
    }
    .error.degraded {
      border-color: rgba(246, 197, 93, 0.28);
      background:
        linear-gradient(180deg, rgba(246, 197, 93, 0.08) 0%, rgba(246, 197, 93, 0.03) 100%),
        rgba(20, 16, 8, 0.35);
      color: #ffe1a0;
      box-shadow:
        inset 0 1px 0 rgba(255, 255, 255, 0.06),
        0 4px 16px rgba(246, 197, 93, 0.08);
    }
    .footer {
      margin-top: 36px;
      display: flex;
      align-items: center;
      justify-content: flex-end;
      color: #627087;
      font-size: 15px;
    }
    .empty {
      display: grid;
      place-items: center;
      min-height: 420px;
      border: 1px solid rgba(148, 163, 184, 0.15);
      border-radius: 14px;
      color: #9aa7b8;
      font-size: 28px;
      font-weight: 760;
      background:
        linear-gradient(180deg, rgba(255, 255, 255, 0.04) 0%, rgba(255, 255, 255, 0.01) 100%),
        rgba(12, 20, 38, 0.3);
      box-shadow:
        0 20px 60px rgba(0, 0, 0, 0.25),
        inset 0 1px 0 rgba(255, 255, 255, 0.08);
      backdrop-filter: blur(20px) saturate(140%);
    }
  </style>
</head>
<body>
  <main class="dashboard">
    <section class="topbar">
      <div>
        <div class="kicker"><span class="kicker-mark"></span>Onprs</div>
        <h1>{{ title | e }}</h1>
        <p class="subtitle">{{ generated_at | e }} · {{ summary.total }} 台服务器 · 自适应 SaaS 监控面板</p>
      </div>
      <div class="summary-grid">
        <div class="summary-item">
          <div class="summary-label">在线</div>
          <div class="summary-value ok">{{ summary.online }}</div>
        </div>
        <div class="summary-item">
          <div class="summary-label">离线</div>
          <div class="summary-value bad">{{ summary.offline }}</div>
        </div>
        <div class="summary-item">
          <div class="summary-label">平均CPU</div>
          <div class="summary-value">{{ summary.avg_cpu }}</div>
        </div>
        <div class="summary-item">
          <div class="summary-label">告警</div>
          <div class="summary-value warn">{{ summary.alerts }}</div>
        </div>
      </div>
    </section>

    {% if servers %}
    <section class="server-grid">
      {% for server in servers %}
      <article class="server-card {{ server.status | e }}">
        <header class="server-head">
          <div>
            <h2 class="server-name">{{ server.name | e }}</h2>
            <div class="server-remark">{{ server.remark | e }}</div>
          </div>
          <div class="status {{ server.status | e }}">{{ server.status_label | e }}</div>
        </header>

        <div class="meta">
          <div class="meta-line">
            <span class="meta-label">位置</span>
            <span class="meta-value">{{ server.location | e }}</span>
          </div>
          <div class="meta-line">
            <span class="meta-label">系统</span>
            <span class="meta-value">{{ server.system | e }}</span>
          </div>
          <div class="meta-line">
            <span class="meta-label">运行时长</span>
            <span class="meta-value">{{ server.uptime | e }}</span>
          </div>
        </div>

        <div class="metrics">
          <div class="cpu-ring" style="--cpu-color: {{ server.cpu_color }}; background: conic-gradient({{ server.cpu_color }} {{ server.cpu_percent }}%, rgba(148, 163, 184, 0.12) 0), #080c12;">
            <div class="cpu-inner">{{ server.cpu_label | e }}</div>
          </div>
          <div class="bar-stack">
            <div class="bar-row">
              <div class="bar-top">
                <span class="bar-label">内存</span>
                <span class="bar-value">{{ server.memory.label | e }}</span>
              </div>
              <div class="bar-track"><div class="bar-fill" style="width: {{ server.memory.percent }}%;"></div></div>
            </div>
            <div class="bar-row">
              <div class="bar-top">
                <span class="bar-label">存储</span>
                <span class="bar-value">{{ server.storage.label | e }}</span>
              </div>
              <div class="bar-track"><div class="bar-fill storage" style="width: {{ server.storage.percent }}%;"></div></div>
            </div>
          </div>
        </div>

        <div class="io-grid">
          <div class="io-cell">
            <div class="io-label">入网</div>
            <div class="io-value">{{ server.network.rx_label | e }}</div>
          </div>
          <div class="io-cell">
            <div class="io-label">出网</div>
            <div class="io-value">{{ server.network.tx_label | e }}</div>
          </div>
          <div class="io-cell">
            <div class="io-label">磁盘读取</div>
            <div class="io-value">{{ server.disk_io.read_label | e }}</div>
          </div>
          <div class="io-cell">
            <div class="io-label">磁盘写入</div>
            <div class="io-value">{{ server.disk_io.write_label | e }}</div>
          </div>
        </div>
        {% if server.error %}
        <div class="error {{ server.status | e }}">{{ server.error | e }}</div>
        {% endif %}
      </article>
      {% endfor %}
    </section>
    {% else %}
    <section class="empty">尚未配置服务器探针</section>
    {% endif %}

    <footer class="footer">
      <span>{{ footer_brand | e }}</span>
    </footer>
  </main>
</body>
</html>
"""


@dataclass(frozen=True, slots=True)
class ServerConfig:
    name: str
    endpoint: str
    token: str = ""
    location_hint: str = ""
    remark: str = ""
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def display_name(self) -> str:
        if self.name.strip():
            return self.name.strip()
        parsed = urlparse(self.endpoint)
        return parsed.netloc or self.endpoint


@dataclass(slots=True)
class UsageMetric:
    used_bytes: float = 0.0
    total_bytes: float = 0.0
    percent: float = 0.0
    label: str = "n/a"


@dataclass(slots=True)
class NetworkMetric:
    rx_bps: float = 0.0
    tx_bps: float = 0.0
    rx_label: str = "0 B/s"
    tx_label: str = "0 B/s"


@dataclass(slots=True)
class DiskIoMetric:
    read_bps: float = 0.0
    write_bps: float = 0.0
    read_label: str = "0 B/s"
    write_label: str = "0 B/s"


@dataclass(slots=True)
class ServerSnapshot:
    name: str
    endpoint: str
    status: str
    cpu_percent: float = 0.0
    cpu_label: str = "n/a"
    cpu_cores: int = 0
    cpu_model: str = ""
    memory: UsageMetric = field(default_factory=UsageMetric)
    storage: UsageMetric = field(default_factory=UsageMetric)
    network: NetworkMetric = field(default_factory=NetworkMetric)
    disk_io: DiskIoMetric = field(default_factory=DiskIoMetric)
    location: str = "Unknown"
    system: str = "Unknown"
    uptime: str = "n/a"
    remark: str = ""
    timestamp: str = ""
    error: str = ""


class ServerMonitorPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        self.config = config or {}
        self._push_task: asyncio.Task | None = None
        self._checking = False

    @filter.command_group("srvmon", alias={"servermon"})
    def srvmon(self):
        """多服务器性能监控面板。"""

    @srvmon.command("bind")
    async def bind(self, event: AstrMessageEvent):
        """绑定当前 OneBot v11 会话为自动推送目标。"""
        if not self._is_allowed(event):
            yield event.plain_result("此命令默认仅 AstrBot 管理员可用。")
            return
        if not self._is_onebot_event(event):
            yield event.plain_result("请在 OneBot v11(aiocqhttp) 群聊或私聊中使用 /srvmon bind。")
            return
        await self.put_kv_data("target_session", str(event.unified_msg_origin))
        yield event.plain_result(f"已绑定服务器监控推送目标：{event.unified_msg_origin}")

    @srvmon.command("unbind")
    async def unbind(self, event: AstrMessageEvent):
        """清除自动推送目标。"""
        if not self._is_allowed(event):
            yield event.plain_result("此命令默认仅 AstrBot 管理员可用。")
            return
        await self.delete_kv_data("target_session")
        yield event.plain_result("已清除服务器监控推送目标。")

    @srvmon.command("status")
    async def status(self, event: AstrMessageEvent):
        """查看插件配置和最近一次采集状态。"""
        if not self._is_allowed(event):
            yield event.plain_result("此命令默认仅 AstrBot 管理员可用。")
            return
        servers = self._server_configs()
        target = await self._load_target()
        last = await self.get_kv_data("last_report", {}) or {}
        if not isinstance(last, dict):
            last = {}
        lines = [
            "服务器监控状态：",
            f"- enable: {self._cfg_bool('enable', True)}",
            f"- servers: {len(servers)}",
            f"- target: {target or '未绑定'}",
            f"- auto_push: {self._cfg_bool('enable_auto_push', False)}",
            f"- interval: {self._cfg_int('auto_push_interval_seconds', 300, 60, 86400)}s",
        ]
        if last:
            lines.extend(
                [
                    f"- last_at: {last.get('generated_at', '')}",
                    f"- online: {last.get('online', 0)}",
                    f"- offline: {last.get('offline', 0)}",
                    f"- alerts: {last.get('alerts', 0)}",
                ],
            )
        yield event.plain_result("\n".join(lines))

    @srvmon.command("report")
    async def report(self, event: AstrMessageEvent):
        """立即采集服务器性能并发送监控图片。"""
        if not self._is_allowed(event):
            yield event.plain_result("此命令默认仅 AstrBot 管理员可用。")
            return
        if not self._is_onebot_event(event):
            yield event.plain_result("此命令仅支持 OneBot v11(aiocqhttp) 会话。")
            return
        try:
            image_ref = await self._render_report(return_url=self._image_send_mode() == "onebot_url")
            await self._send_image_to_session(str(event.unified_msg_origin), image_ref)
        except Exception as exc:
            logger.warning("[%s] manual report failed: %s", PLUGIN_NAME, exc, exc_info=True)
            yield event.plain_result(f"服务器监控图片发送失败：{_truncate(str(exc), 260)}")
            return
        if False:
            yield event.plain_result("")

    @srvmon.command("once")
    async def once(self, event: AstrMessageEvent):
        """report 的别名，立即发送一次监控图片。"""
        async for item in self.report(event):
            yield item

    async def initialize(self) -> None:
        if not self._cfg_bool("enable", True):
            return
        if not self._cfg_bool("enable_auto_push", False):
            return
        if self._push_task is not None and not self._push_task.done():
            return
        self._push_task = asyncio.create_task(self._auto_push_loop())

    async def terminate(self) -> None:
        task = self._push_task
        self._push_task = None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _auto_push_loop(self) -> None:
        interval = self._cfg_int("auto_push_interval_seconds", 300, 60, 86400)
        await asyncio.sleep(self._cfg_int("initial_push_delay_seconds", 10, 0, 3600))
        while True:
            try:
                target = await self._load_target()
                if target:
                    image_ref = await self._render_report(
                        return_url=self._image_send_mode() == "onebot_url",
                    )
                    if not self._cfg_bool("push_only_on_alert", False) or await self._last_report_has_alert():
                        await self._send_image_to_session(target, image_ref)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("[%s] auto push failed: %s", PLUGIN_NAME, exc, exc_info=True)
            await asyncio.sleep(interval)

    async def _render_report(self, *, return_url: bool) -> str:
        snapshots = await self.collect_snapshots()
        generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        data = build_dashboard_data(
            snapshots,
            title=str(self._cfg("dashboard_title", "Server Performance Control Tower") or ""),
            generated_at=generated_at,
            cpu_alert_percent=self._cfg_float("cpu_alert_percent", 85.0, 1.0, 100.0),
            memory_alert_percent=self._cfg_float("memory_alert_percent", 90.0, 1.0, 100.0),
            storage_alert_percent=self._cfg_float("storage_alert_percent", 90.0, 1.0, 100.0),
        )
        await self._save_report_stats(data)
        quality = self._cfg_int("image_quality", 92, 30, 95)
        backend = self._render_backend()
        font_path = str(self._cfg("local_font_path", "") or "").strip()
        if backend == "local_pillow":
            return await self._render_report_local_pillow(data, quality, font_path)
        try:
            return await self.html_render(
                DASHBOARD_TEMPLATE,
                data,
                return_url=return_url,
                options={
                    "type": "jpeg",
                    "quality": quality,
                    "full_page": True,
                    "scale": "css",
                    "animations": "disabled",
                },
            )
        except Exception as exc:
            if backend != "auto":
                raise
            logger.warning(
                "[%s] remote html render failed, falling back to local image: %s",
                PLUGIN_NAME,
                exc,
                exc_info=True,
            )
            return await self._render_report_local_pillow(data, quality, font_path)

    async def _render_report_local_pillow(
        self,
        data: dict[str, Any],
        quality: int,
        font_path: str = "",
    ) -> str:
        render_scale = self._cfg_int("local_render_scale", 2, 1, 3)
        return render_dashboard_image_local(
            data,
            quality=quality,
            font_path=font_path,
            render_scale=render_scale,
        )

    async def collect_snapshots(self) -> list[ServerSnapshot]:
        servers = self._server_configs()
        if not servers:
            return []
        timeout = aiohttp.ClientTimeout(
            total=self._cfg_float("request_timeout_seconds", 8.0, 1.0, 120.0),
        )
        connector_limit = self._cfg_int("max_concurrent_requests", 12, 1, 64)
        semaphore = asyncio.Semaphore(connector_limit)
        async with aiohttp.ClientSession(trust_env=True, timeout=timeout) as session:
            tasks = [
                self._fetch_snapshot(session, server, semaphore)
                for server in servers
            ]
            return list(await asyncio.gather(*tasks))

    async def _fetch_snapshot(
        self,
        session: aiohttp.ClientSession,
        server: ServerConfig,
        semaphore: asyncio.Semaphore,
    ) -> ServerSnapshot:
        async with semaphore:
            attempts = self._cfg_int("probe_retry_count", 2, 1, 6)
            retry_delay = self._cfg_float(
                "probe_retry_delay_seconds",
                0.45,
                0.0,
                10.0,
            )
            last_error = "probe unreachable"
            retryable = True
            for attempt in range(attempts):
                if attempt > 0 and retry_delay > 0:
                    await asyncio.sleep(retry_delay * attempt)
                snapshot, last_error, retryable = await self._fetch_snapshot_once(session, server)
                if snapshot is not None:
                    return snapshot
                if not retryable:
                    break

            if await self._tcp_fallback_alive(server):
                return degraded_snapshot(server, last_error)
            return offline_snapshot(server, last_error)

    async def _fetch_snapshot_once(
        self,
        session: aiohttp.ClientSession,
        server: ServerConfig,
    ) -> tuple[ServerSnapshot | None, str, bool]:
        try:
            async with session.get(server.endpoint, headers=server.headers) as response:
                text = await response.text()
                if response.status != 200:
                    return (
                        None,
                        f"HTTP {response.status}: {_truncate(text, 180)}",
                        response.status in {408, 429, 500, 502, 503, 504},
                    )
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError as exc:
                    return None, f"invalid JSON: {exc}", False
                if not isinstance(payload, dict):
                    return None, "probe payload is not an object", False
                return normalize_probe_payload(server, payload), "", False
        except asyncio.TimeoutError:
            return None, "request timeout", True
        except Exception as exc:
            return None, f"{type(exc).__name__}: {exc}", True

    async def _tcp_fallback_alive(self, server: ServerConfig) -> bool:
        if not self._cfg_bool("enable_tcp_fallback_detection", True):
            return False
        parsed = urlparse(server.endpoint)
        host = parsed.hostname
        if not host:
            return False
        timeout = self._cfg_float("tcp_fallback_timeout_seconds", 1.5, 0.2, 10.0)
        for port in self._tcp_fallback_ports_for_server(server):
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port),
                    timeout=timeout,
                )
            except Exception:
                continue
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            return True
        return False

    def _tcp_fallback_ports_for_server(self, server: ServerConfig) -> list[int]:
        ports: list[int] = []
        for port in _parse_ports(self._cfg("tcp_fallback_ports", "22")):
            if port not in ports:
                ports.append(port)
        parsed = urlparse(server.endpoint)
        if parsed.port and parsed.port not in ports:
            ports.append(parsed.port)
        return ports

    async def _send_image_to_session(self, target_session: str, image_ref: str) -> None:
        mode = self._image_send_mode()
        if mode in {"onebot_url", "onebot_file"}:
            await self._send_onebot_image_raw(target_session, image_ref)
            return
        chain = MessageChain([Comp.Image.fromFileSystem(image_ref)])
        sent = await self.context.send_message(target_session, chain)
        if sent is False:
            raise RuntimeError(f"target session not found: {target_session}")

    async def _send_onebot_image_raw(self, target_session: str, image_ref: str) -> None:
        platform_id, message_type, session_id = _parse_target_session(target_session)
        bot = self._find_onebot_bot(platform_id)
        payload = [
            {
                "type": "image",
                "data": {
                    "file": _onebot_image_file_value(image_ref),
                    "cache": 0,
                },
            },
        ]
        if message_type == "GroupMessage":
            await bot.send_group_msg(
                group_id=int(_session_numeric_id(session_id)),
                message=payload,
            )
            return
        if message_type == "FriendMessage":
            await bot.send_private_msg(
                user_id=int(_session_numeric_id(session_id)),
                message=payload,
            )
            return
        raise RuntimeError(f"unsupported OneBot message type: {message_type}")

    def _find_onebot_bot(self, platform_id: str) -> Any:
        platform_manager = getattr(self.context, "platform_manager", None)
        platforms = getattr(platform_manager, "platform_insts", []) or []
        for platform in platforms:
            meta_func = getattr(platform, "meta", None)
            if not callable(meta_func):
                continue
            meta = meta_func()
            meta_id = str(getattr(meta, "id", "") or "")
            meta_name = str(getattr(meta, "name", "") or "")
            if platform_id not in {meta_id, meta_name}:
                continue
            bot = getattr(platform, "bot", None)
            if bot is not None:
                return bot
            get_client = getattr(platform, "get_client", None)
            if callable(get_client):
                return get_client()
        raise RuntimeError(f"OneBot platform not found: {platform_id}")

    async def _save_report_stats(self, data: dict[str, Any]) -> None:
        summary = data.get("summary", {})
        if not isinstance(summary, dict):
            summary = {}
        await self.put_kv_data(
            "last_report",
            {
                "generated_at": data.get("generated_at", ""),
                "online": summary.get("online", 0),
                "offline": summary.get("offline", 0),
                "degraded": summary.get("degraded", 0),
                "alerts": summary.get("alerts", 0),
            },
        )

    async def _last_report_has_alert(self) -> bool:
        last = await self.get_kv_data("last_report", {}) or {}
        if not isinstance(last, dict):
            return False
        return int(last.get("offline", 0) or 0) > 0 or int(last.get("alerts", 0) or 0) > 0

    async def _load_target(self) -> str:
        return str(await self.get_kv_data("target_session", "") or "")

    def _server_configs(self) -> list[ServerConfig]:
        servers = parse_server_configs(self._cfg("servers", []))
        if servers:
            return servers
        return parse_server_configs(self._cfg("servers_json", "[]"))

    def _image_send_mode(self) -> str:
        mode = str(self._cfg("image_send_mode", "onebot_url") or "onebot_url")
        mode = mode.strip().lower()
        if mode in {"onebot_url", "onebot_file", "astrbot"}:
            return mode
        return "onebot_url"

    def _render_backend(self) -> str:
        backend = str(self._cfg("render_backend", "local_pillow") or "local_pillow")
        backend = backend.strip().lower()
        if backend in {"local_pillow", "html_remote", "auto"}:
            return backend
        return "local_pillow"

    def _is_allowed(self, event: AstrMessageEvent) -> bool:
        if not self._cfg_bool("admin_only", True):
            return True
        is_admin = getattr(event, "is_admin", None)
        if callable(is_admin):
            try:
                return bool(is_admin())
            except Exception:
                return False
        return False

    def _is_onebot_event(self, event: AstrMessageEvent) -> bool:
        get_platform_name = getattr(event, "get_platform_name", None)
        if not callable(get_platform_name):
            return False
        try:
            return str(get_platform_name() or "") in SUPPORTED_PLATFORMS
        except Exception:
            return False

    def _cfg(self, key: str, default: Any) -> Any:
        if hasattr(self.config, "get"):
            return self.config.get(key, default)
        return default

    def _cfg_bool(self, key: str, default: bool) -> bool:
        value = self._cfg(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def _cfg_int(self, key: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(self._cfg(key, default))
        except Exception:
            value = default
        return max(minimum, min(value, maximum))

    def _cfg_float(
        self,
        key: str,
        default: float,
        minimum: float,
        maximum: float,
    ) -> float:
        try:
            value = float(self._cfg(key, default))
        except Exception:
            value = default
        return max(minimum, min(value, maximum))


def parse_server_configs(value: Any) -> list[ServerConfig]:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"servers_json is not valid JSON: {exc}") from exc
    if isinstance(value, dict):
        if isinstance(value.get("servers"), list):
            value = value["servers"]
        else:
            value = [value]
    if not isinstance(value, list):
        return []

    servers: list[ServerConfig] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        endpoint = str(item.get("endpoint") or item.get("url") or "").strip()
        if not endpoint:
            continue
        headers = _parse_headers(item.get("headers", {}))
        token = str(item.get("token") or "").strip()
        if token and "Authorization" not in headers:
            headers["Authorization"] = f"Bearer {token}"
        servers.append(
            ServerConfig(
                name=str(item.get("name") or item.get("display_name") or "").strip(),
                endpoint=endpoint,
                token=token,
                location_hint=str(
                    item.get("location") or item.get("location_hint") or "",
                ).strip(),
                remark=str(item.get("remark") or item.get("note") or "").strip(),
                headers=headers,
            ),
        )
    return servers


def normalize_probe_payload(config: ServerConfig, payload: dict[str, Any]) -> ServerSnapshot:
    memory = _normalize_usage(payload.get("memory") or payload.get("mem") or {})
    storage = _normalize_storage(payload.get("storage") or payload.get("disks") or {})
    network = _normalize_network(payload.get("network") or payload.get("net") or {})
    disk_io = _normalize_disk_io(payload.get("disk_io") or payload.get("io") or {})
    cpu_percent = _clamp_percent(
        _first_number(
            payload.get("cpu"),
            ("usage_percent", "percent", "usage", "cpu_percent"),
        ),
    )
    system = _normalize_system(payload.get("system") or payload.get("os") or "")
    location = config.location_hint or _normalize_location(payload.get("location"), "")
    uptime = _format_duration(_first_number(payload, ("uptime_seconds", "uptime", "uptime_sec")))
    cpu = payload.get("cpu") if isinstance(payload.get("cpu"), dict) else {}
    cpu_cores = int(_first_number(cpu, ("cores", "count", "logical_count")) or 0)
    cpu_model = str(cpu.get("model") or cpu.get("name") or "").strip()
    name = config.display_name
    if not config.name.strip():
        name = str(payload.get("hostname") or payload.get("name") or name).strip() or name

    return ServerSnapshot(
        name=name,
        endpoint=config.endpoint,
        status="online",
        cpu_percent=cpu_percent,
        cpu_label=_format_percent(cpu_percent),
        cpu_cores=cpu_cores,
        cpu_model=cpu_model,
        memory=memory,
        storage=storage,
        network=network,
        disk_io=disk_io,
        location=location,
        system=system,
        uptime=uptime,
        remark=config.remark or str(payload.get("remark") or payload.get("note") or "").strip(),
        timestamp=str(payload.get("timestamp") or ""),
    )


def offline_snapshot(config: ServerConfig, error: str) -> ServerSnapshot:
    return ServerSnapshot(
        name=config.display_name,
        endpoint=config.endpoint,
        status="offline",
        cpu_percent=0.0,
        cpu_label="n/a",
        location=config.location_hint or "Unknown",
        system="Unreachable",
        uptime="n/a",
        remark=config.remark,
        error=_truncate(error or "probe unreachable", 220),
    )


def degraded_snapshot(config: ServerConfig, error: str) -> ServerSnapshot:
    return ServerSnapshot(
        name=config.display_name,
        endpoint=config.endpoint,
        status="degraded",
        cpu_percent=0.0,
        cpu_label="n/a",
        location=config.location_hint or "Unknown",
        system="探针异常",
        uptime="n/a",
        remark=config.remark,
        error=_truncate(error or "probe temporarily unavailable", 220),
    )


def build_dashboard_data(
    snapshots: list[ServerSnapshot],
    *,
    title: str,
    generated_at: str,
    cpu_alert_percent: float = 85.0,
    memory_alert_percent: float = 90.0,
    storage_alert_percent: float = 90.0,
) -> dict[str, Any]:
    layout = _adaptive_layout(len(snapshots))
    online = [snapshot for snapshot in snapshots if snapshot.status == "online"]
    offline_count = sum(1 for snapshot in snapshots if snapshot.status == "offline")
    degraded_count = sum(1 for snapshot in snapshots if snapshot.status == "degraded")
    avg_cpu = (
        sum(snapshot.cpu_percent for snapshot in online) / len(online)
        if online
        else 0.0
    )
    alerts = offline_count + degraded_count
    server_rows = []
    for snapshot in snapshots:
        is_alert = (
            snapshot.status != "online"
            or snapshot.cpu_percent >= cpu_alert_percent
            or snapshot.memory.percent >= memory_alert_percent
            or snapshot.storage.percent >= storage_alert_percent
        )
        if is_alert and snapshot.status == "online":
            alerts += 1
        server_rows.append(_snapshot_to_render_dict(snapshot, cpu_alert_percent))

    return {
        "title": title or "Server Performance Control Tower",
        "generated_at": generated_at,
        "layout": layout,
        "summary": {
            "total": len(snapshots),
            "online": len(online),
            "offline": offline_count,
            "degraded": degraded_count,
            "avg_cpu": _format_percent(avg_cpu),
            "alerts": alerts,
        },
        "servers": server_rows,
        "footer_brand": "made by onprs",
    }


def _adaptive_layout(count: int) -> dict[str, int]:
    if count <= 1:
        columns = 1
    elif count <= 4:
        columns = 2
    else:
        columns = 3
    rows = max(1, math.ceil(max(count, 1) / columns))
    width = 1600
    gap = 30
    header_height = 316
    card_height = 560
    bottom = 134
    height = header_height + rows * card_height + max(0, rows - 1) * gap + bottom
    return {
        "width": width,
        "height": max(900, height),
        "columns": columns,
        "rows": rows,
        "gap": gap,
        "card_height": card_height,
    }


def _snapshot_to_render_dict(
    snapshot: ServerSnapshot,
    cpu_alert_percent: float,
) -> dict[str, Any]:
    cpu_color = "#36f5c6"
    if snapshot.status == "offline":
        cpu_color = "#ff6b82"
    elif snapshot.status == "degraded":
        cpu_color = "#f6c55d"
    elif snapshot.cpu_percent >= cpu_alert_percent:
        cpu_color = "#ff6b82"
    elif snapshot.cpu_percent >= max(65.0, cpu_alert_percent - 20.0):
        cpu_color = "#f6c55d"
    return {
        "name": snapshot.name,
        "status": snapshot.status,
        "status_label": _status_label(snapshot.status),
        "endpoint": snapshot.endpoint,
        "cpu_percent": round(snapshot.cpu_percent, 1),
        "cpu_label": snapshot.cpu_label,
        "cpu_color": cpu_color,
        "memory": _usage_to_render_dict(snapshot.memory),
        "storage": _usage_to_render_dict(snapshot.storage),
        "network": {
            "rx_label": snapshot.network.rx_label,
            "tx_label": snapshot.network.tx_label,
        },
        "disk_io": {
            "read_label": snapshot.disk_io.read_label,
            "write_label": snapshot.disk_io.write_label,
        },
        "location": snapshot.location or "Unknown",
        "system": snapshot.system or "Unknown",
        "uptime": snapshot.uptime or "n/a",
        "remark": snapshot.remark or snapshot.endpoint,
        "error": snapshot.error,
    }


def _usage_to_render_dict(metric: UsageMetric) -> dict[str, Any]:
    return {
        "percent": round(_clamp_percent(metric.percent), 1),
        "label": metric.label or "n/a",
    }


def _status_label(status: str) -> str:
    labels = {
        "online": "在线",
        "degraded": "探针异常",
        "offline": "离线",
    }
    return labels.get(str(status or "").strip().lower(), "未知")


def render_dashboard_image_local(
    data: dict[str, Any],
    *,
    quality: int = 90,
    font_path: str = "",
    render_scale: int = 2,
) -> str:
    """Render a self-contained fallback dashboard with Pillow."""
    from PIL import Image

    layout = data.get("layout", {}) if isinstance(data.get("layout"), dict) else {}
    width = int(layout.get("width") or 1600)
    height = int(layout.get("height") or 900)
    quality = max(30, min(int(quality or 90), 95))
    render_scale = max(1, min(int(render_scale or 1), 3))

    image = _render_dashboard_image_local_canvas(
        data,
        width=width,
        height=height,
        font_path=font_path,
        scale=render_scale,
    )
    if render_scale > 1:
        image = image.resize((width, height), Image.Resampling.LANCZOS)

    temp_dir = Path(tempfile.gettempdir()) / "astrbot_server_monitor"
    temp_dir.mkdir(parents=True, exist_ok=True)
    output = temp_dir / f"dashboard_{int(time.time())}_{uuid.uuid4().hex[:8]}.jpg"
    image.save(output, format="JPEG", quality=quality, optimize=True)
    return str(output)


def _render_dashboard_image_local_canvas(
    data: dict[str, Any],
    *,
    width: int,
    height: int,
    font_path: str,
    scale: int,
) -> Any:
    from PIL import Image, ImageDraw

    def s(value: float) -> int:
        return int(round(float(value) * scale))

    render_width = s(width)
    render_height = s(height)
    image = Image.new("RGB", (render_width, render_height), "#03050a")
    draw = ImageDraw.Draw(image)
    _draw_local_background(image, draw, render_width, render_height, scale=scale)

    fonts = _local_fonts(font_path, scale=scale)
    title_font = fonts["title"]
    subtitle_font = fonts["subtitle"]
    label_font = fonts["label"]
    value_font = fonts["value"]
    body_font = fonts["body"]

    x = s(64)
    y = s(48)
    # Premium badge with glass effect
    badge_box = (x, y, x + s(116), y + s(32))
    _draw_local_glass_panel(
        image, draw, badge_box,
        radius=s(8), fill="#061e24", outline="#1a6b75",
        scale=scale, alpha=140, shadow=False,
    )
    draw.rectangle((x + s(12), y + s(12), x + s(20), y + s(20)), fill="#36f5c6")
    draw.text((x + s(28), y + s(7)), "Onprs", fill="#7ee8f0", font=label_font)

    title = str(data.get("title") or "服务器性能监控")
    draw.text((x, y + s(52)), _fit_text(title, title_font, s(820)), fill="#ffffff", font=title_font)
    summary = data.get("summary", {}) if isinstance(data.get("summary"), dict) else {}
    subtitle = f"{data.get('generated_at', '')} · {summary.get('total', 0)} 台服务器 · 商业级 SaaS 监控面板"
    draw.text((x, y + s(116)), subtitle, fill="#8896a8", font=subtitle_font)

    summary_items = (
        ("在线", str(summary.get("online", 0)), "#5df2b5"),
        ("离线", str(summary.get("offline", 0)), "#ff6b82"),
        ("平均CPU", str(summary.get("avg_cpu", "0%")), "#ffffff"),
        ("告警", str(summary.get("alerts", 0)), "#f6c55d"),
    )
    sx = render_width - s(64) - 4 * s(160) - 3 * s(12)
    for idx, (label, value, color) in enumerate(summary_items):
        left = sx + idx * s(172)
        top = s(54)
        _draw_local_glass_panel(
            image,
            draw,
            (left, top, left + s(160), top + s(92)),
            radius=s(12),
            fill="#0a1420",
            outline="#3d5068",
            scale=scale,
            alpha=115,
        )
        draw.text((left + s(16), top + s(14)), label, fill="#6a7d94", font=label_font)
        draw.text((left + s(16), top + s(44)), value, fill=color, font=value_font)

    servers = data.get("servers", []) if isinstance(data.get("servers"), list) else []
    if not servers:
        empty_top = s(320)
        box = (s(64), empty_top, render_width - s(64), empty_top + s(420))
        _draw_local_glass_panel(
            image,
            draw,
            box,
            radius=s(14),
            fill="#0c1420",
            outline="#3d5068",
            scale=scale,
            alpha=125,
        )
        _draw_centered_text(draw, "尚未配置服务器探针", box, fonts["empty"], "#8896a8")
    else:
        layout = data.get("layout", {}) if isinstance(data.get("layout"), dict) else {}
        columns = int(layout.get("columns") or 1)
        gap = s(int(layout.get("gap") or 30))
        card_height = s(int(layout.get("card_height") or 560))
        grid_left = s(64)
        grid_top = s(348)
        card_width = int((render_width - grid_left * 2 - gap * max(columns - 1, 0)) / max(columns, 1))
        for idx, server in enumerate(servers):
            col = idx % columns
            row = idx // columns
            left = grid_left + col * (card_width + gap)
            top = grid_top + row * (card_height + gap)
            _draw_local_server_card(
                image,
                draw,
                server,
                (left, top, left + card_width, top + card_height),
                fonts,
                scale=scale,
            )

    footer_y = max(render_height - s(46), 0)
    footer = str(data.get("footer_brand") or "made by onprs")
    footer_width = draw.textbbox((0, 0), footer, font=body_font)[2]
    draw.text((render_width - s(64) - footer_width, footer_y), footer, fill="#627087", font=body_font)
    return image


def _local_fonts(font_path: str = "", scale: int = 1) -> dict[str, Any]:
    def size(value: int) -> int:
        return max(1, int(round(value * max(1, scale))))

    return {
        "title": _load_local_font(size(48), bold=True, font_path=font_path),
        "subtitle": _load_local_font(size(22), font_path=font_path),
        "label": _load_local_font(size(14), bold=True, font_path=font_path),
        "value": _load_local_font(size(34), bold=True, font_path=font_path),
        "name": _load_local_font(size(26), bold=True, font_path=font_path),
        "body": _load_local_font(size(15), font_path=font_path),
        "body_bold": _load_local_font(size(17), bold=True, font_path=font_path),
        "metric": _load_local_font(size(24), bold=True, font_path=font_path),
        "empty": _load_local_font(size(28), bold=True, font_path=font_path),
        "status": _load_local_font(size(13), bold=True, font_path=font_path),
    }


def _load_local_font(size: int, *, bold: bool = False, font_path: str = "") -> Any:
    from PIL import ImageFont

    preferred = _font_candidates(font_path, bold=bold)
    fallback = None
    for candidate in preferred:
        try:
            font = ImageFont.truetype(str(candidate), size)
        except Exception:
            continue
        if _font_supports_text(font, "服务器监控中文"):
            return font
        if fallback is None:
            fallback = font
    if fallback is not None:
        logger.warning(
            "[%s] no configured CJK font found; local rendered Chinese may show as boxes",
            PLUGIN_NAME,
        )
        return fallback
    logger.warning(
        "[%s] no TrueType font found; local rendered Chinese may show as boxes",
        PLUGIN_NAME,
    )
    return ImageFont.load_default()


def _font_candidates(font_path: str = "", *, bold: bool = False) -> list[Path | str]:
    names = (
        (
            "NotoSansCJK-Bold.ttc",
            "NotoSansCJK-Bold.otf",
            "NotoSansSC-Bold.otf",
            "SourceHanSansSC-Bold.otf",
            "SourceHanSansCN-Bold.otf",
            "WenQuanYi Micro Hei Bold.ttf",
            "wqy-microhei.ttc",
            "msyhbd.ttc",
            "simhei.ttf",
        )
        if bold
        else (
            "NotoSansCJK-Regular.ttc",
            "NotoSansCJK-Regular.otf",
            "NotoSansSC-Regular.otf",
            "NotoSansSC-VF.ttf",
            "SourceHanSansSC-Regular.otf",
            "SourceHanSansCN-Regular.otf",
            "WenQuanYi Micro Hei.ttf",
            "wqy-microhei.ttc",
            "DroidSansFallbackFull.ttf",
            "DroidSansFallback.ttf",
            "msyh.ttc",
            "simsun.ttc",
            "simhei.ttf",
            "PingFang.ttc",
        )
    )
    roots = (
        Path("/usr/share/fonts"),
        Path("/usr/local/share/fonts"),
        Path("/usr/share/fonts/opentype"),
        Path("/usr/share/fonts/truetype"),
        Path("/usr/share/fonts/truetype/noto"),
        Path("/usr/share/fonts/opentype/noto"),
        Path("/usr/share/fonts/truetype/wqy"),
        Path("/usr/share/fonts/wenquanyi"),
        Path("/System/Library/Fonts"),
        Path("/Library/Fonts"),
        Path("C:/Windows/Fonts"),
    )
    candidates: list[Path | str] = []
    if font_path:
        candidates.append(Path(font_path))
    for root in roots:
        for name in names:
            candidates.append(root / name)
    candidates.extend(names)
    candidates.extend(("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf", "Arial.ttf"))
    seen: set[str] = set()
    result: list[Path | str] = []
    for candidate in candidates:
        key = str(candidate).lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(candidate)
    return result


def _font_supports_text(font: Any, text: str) -> bool:
    try:
        for char in text:
            if not char.strip():
                continue
            mask = font.getmask(char)
            if mask.getbbox() is None:
                return False
    except Exception:
        return False
    return True


def _draw_local_background(image: Any, draw: Any, width: int, height: int, *, scale: int = 1) -> None:
    from PIL import Image, ImageDraw

    # Rich dark gradient background
    for y in range(height):
        ratio = y / max(height - 1, 1)
        r = int(3 + 10 * ratio)
        g = int(5 + 12 * ratio)
        b = int(10 + 18 * ratio)
        draw.line((0, y, width, y), fill=(r, g, b))

    # Ambient glow orbs for premium SaaS feel
    def _draw_glow_orb(cx: int, cy: int, radius: int, color: tuple[int, int, int]) -> None:
        orb = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        orb_draw = ImageDraw.Draw(orb)
        for r_ in range(radius, 0, -3):
            alpha = int(28 * (1 - r_ / radius))
            if alpha > 0:
                orb_draw.ellipse(
                    (cx - r_, cy - r_, cx + r_, cy + r_),
                    fill=(*color, alpha),
                )
        image.paste(orb, (0, 0), orb)

    # Top-left cyan glow
    _draw_glow_orb(int(width * 0.20), int(height * -0.02), int(max(width, height) * 0.40), (54, 245, 198))
    # Top-right blue glow
    _draw_glow_orb(int(width * 0.85), int(height * 0.08), int(max(width, height) * 0.32), (76, 201, 240))
    # Bottom-center purple glow (very subtle, far below)
    _draw_glow_orb(int(width * 0.50), int(height * 1.12), int(max(width, height) * 0.32), (139, 92, 246))

    # Subtle grid (only top portion)
    grid = max(1, int(40 * max(1, scale)))
    for x in range(0, width, grid):
        draw.line((x, 0, x, int(height * 0.50)), fill=(18, 26, 38))
    for y in range(0, int(height * 0.50), grid):
        draw.line((0, y, width, y), fill=(18, 26, 38))


def _draw_local_glass_panel(
    image: Any,
    draw: Any,
    box: tuple[int, int, int, int],
    *,
    radius: int,
    fill: str,
    outline: str,
    scale: int = 1,
    shadow: bool = True,
    alpha: int = 130,
    glow_color: str | None = None,
) -> None:
    from PIL import Image, ImageDraw, ImageFilter

    left, top, right, bottom = box
    shadow_offset = max(1, int(6 * max(1, scale)))
    line_width = max(1, int(max(1, scale)))

    # Colored ambient glow shadow instead of plain black
    if shadow:
        if glow_color:
            glow_rgb = _hex_to_rgb(glow_color)
            glow = Image.new("RGBA", (right - left + shadow_offset * 2, bottom - top + shadow_offset * 2), (0, 0, 0, 0))
            glow_draw = ImageDraw.Draw(glow)
            glow_draw.rounded_rectangle(
                (shadow_offset, shadow_offset, shadow_offset + right - left, shadow_offset + bottom - top),
                radius=radius,
                fill=(*glow_rgb, 20),
            )
            glow = glow.filter(ImageFilter.GaussianBlur(radius=max(4.0, 16.0 * scale)))
            image.paste(glow, (left - shadow_offset, top - shadow_offset), glow)
        else:
            draw.rounded_rectangle(
                (left + shadow_offset, top + shadow_offset, right + shadow_offset, bottom + shadow_offset),
                radius=radius,
                fill="#060a10",
            )

    panel_width = max(1, right - left)
    panel_height = max(1, bottom - top)
    crop = image.crop((left, top, right, bottom)).convert("RGBA")
    crop = crop.filter(ImageFilter.GaussianBlur(radius=max(1.0, 12.0 * max(1, scale))))

    mask = Image.new("L", (panel_width, panel_height), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.rounded_rectangle((0, 0, panel_width - 1, panel_height - 1), radius=radius, fill=255)

    panel = Image.new("RGBA", (panel_width, panel_height), (0, 0, 0, 0))
    panel.alpha_composite(crop)
    tint = Image.new("RGBA", (panel_width, panel_height), (*_hex_to_rgb(fill), alpha))
    panel.alpha_composite(tint)

    # Enhanced sheen: top horizontal gradient + diagonal highlight
    sheen = Image.new("RGBA", (panel_width, panel_height), (0, 0, 0, 0))
    sheen_draw = ImageDraw.Draw(sheen)
    sheen_height = max(1, int(panel_height * 0.28))
    for row in range(sheen_height):
        opacity = int(38 * (1 - row / sheen_height))
        sheen_draw.line((0, row, panel_width, row), fill=(255, 255, 255, opacity))
    # Diagonal glass reflection
    sheen_draw.polygon(
        [
            (0, 0),
            (int(panel_width * 0.55), 0),
            (int(panel_width * 0.20), panel_height),
            (0, panel_height),
        ],
        fill=(255, 255, 255, 5),
    )
    panel.alpha_composite(sheen)

    # Inner edge glow (subtle white top edge)
    inner_glow = Image.new("RGBA", (panel_width, panel_height), (0, 0, 0, 0))
    inner_glow_draw = ImageDraw.Draw(inner_glow)
    inner_glow_draw.rounded_rectangle(
        (1, 1, panel_width - 2, max(2, int(panel_height * 0.18))),
        radius=max(1, radius - 1),
        fill=(255, 255, 255, 10),
    )
    panel.alpha_composite(inner_glow)

    panel.putalpha(mask)
    image.paste(panel.convert("RGB"), (left, top), mask)

    # Outer border with subtle luminance
    draw.rounded_rectangle(box, radius=radius, outline=outline, width=line_width)
    # Subtle inner border highlight
    draw.rounded_rectangle(
        (left + line_width, top + line_width, right - line_width, bottom - line_width),
        radius=max(1, radius - line_width),
        outline="#2a3a50",
        width=line_width,
    )
    # Top highlight for glass edge
    highlight_bottom = top + max(line_width, int((bottom - top) * 0.15))
    draw.rounded_rectangle(
        (left + line_width, top + line_width, right - line_width, highlight_bottom),
        radius=max(1, radius - line_width),
        outline="#6d809c",
        width=line_width,
    )


def _draw_local_rounded_overlay(
    image: Any,
    box: tuple[int, int, int, int],
    *,
    radius: int,
    fill: str,
    alpha: int,
) -> None:
    from PIL import Image, ImageDraw

    left, top, right, bottom = box
    width = max(1, right - left)
    height = max(1, bottom - top)
    overlay = Image.new("RGBA", (width, height), (*_hex_to_rgb(fill), max(0, min(alpha, 255))))
    mask = Image.new("L", (width, height), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.rounded_rectangle((0, 0, width - 1, height - 1), radius=radius, fill=255)
    image.paste(overlay.convert("RGB"), (left, top), mask)


def _draw_local_server_card(
    image: Any,
    draw: Any,
    server: dict[str, Any],
    box: tuple[int, int, int, int],
    fonts: dict[str, Any],
    *,
    scale: int = 1,
) -> None:
    def s(value: float) -> int:
        return int(round(float(value) * max(1, scale)))

    left, top, right, bottom = box
    status = str(server.get("status") or "")
    glow_color = None
    if status == "online":
        glow_color = "#36f5c6"
    elif status == "offline":
        glow_color = "#ff6b82"
    elif status == "degraded":
        glow_color = "#f6c55d"

    _draw_local_glass_panel(
        image,
        draw,
        box,
        radius=s(14),
        fill="#0c1422",
        outline="#3d5068",
        scale=scale,
        alpha=120,
        glow_color=glow_color,
    )
    # Top highlight overlay for premium glass feel
    _draw_local_rounded_overlay(
        image,
        (left + s(2), top + s(2), right - s(2), top + s(100)),
        radius=s(12),
        fill="#1a2840",
        alpha=72,
    )
    pad = s(22)
    x = left + pad
    y = top + pad
    name = _fit_text(str(server.get("name") or "Unknown"), fonts["name"], right - left - s(170))
    draw.text((x, y), name, fill="#ffffff", font=fonts["name"])
    draw.text(
        (x, y + s(40)),
        _fit_text(str(server.get("remark") or ""), fonts["body"], right - left - s(120)),
        fill="#8896a8",
        font=fonts["body"],
    )

    status_label = str(server.get("status_label") or _status_label(status))
    if status == "online":
        status_fill = "#59f0b0"
        status_text = "#06110d"
    elif status == "degraded":
        status_fill = "#f6c55d"
        status_text = "#1c1203"
    else:
        status_fill = "#ff6b82"
        status_text = "#21060b"
    status_box = (right - s(112), y, right - s(22), y + s(34))
    draw.rounded_rectangle(status_box, radius=s(8), fill=status_fill)
    _draw_centered_text(draw, status_label, status_box, fonts["status"], status_text)

    from PIL import Image, ImageDraw

    meta_y = y + s(92)
    for idx, (label, value) in enumerate(
        (
            ("位置", server.get("location") or "Unknown"),
            ("系统", server.get("system") or "Unknown"),
            ("运行时长", server.get("uptime") or "n/a"),
        ),
    ):
        line_y = meta_y + idx * s(26)
        draw.text((x, line_y), label, fill="#68778b", font=fonts["body_bold"])
        draw.text(
            (x + s(86), line_y),
            _fit_text(str(value), fonts["body"], right - left - s(130)),
            fill="#c4ccd8",
            font=fonts["body"],
        )

    metric_top = meta_y + s(96)
    cx = x + s(60)
    cy = metric_top + s(58)
    cpu_percent = float(server.get("cpu_percent") or 0)
    cpu_color = str(server.get("cpu_color") or "#36f5c6")

    # CPU ring outer glow
    if cpu_percent > 0:
        glow_r = s(62)
        glow = Image.new("RGBA", (glow_r * 2, glow_r * 2), (0, 0, 0, 0))
        glow_draw = ImageDraw.Draw(glow)
        rgb = _hex_to_rgb(cpu_color)
        for gr in range(glow_r, s(54), -2):
            alpha = int(28 * (1 - (gr - s(54)) / (glow_r - s(54))))
            if alpha > 0:
                glow_draw.ellipse((glow_r - gr, glow_r - gr, glow_r + gr, glow_r + gr), fill=(*rgb, alpha))
        image.paste(glow, (cx - glow_r, cy - glow_r), glow)

    # Outer track
    draw.ellipse(
        (cx - s(58), cy - s(58), cx + s(58), cy + s(58)),
        fill="#060a0f",
        outline="#1a2030",
        width=s(10),
    )
    if cpu_percent > 0:
        draw.arc(
            (cx - s(58), cy - s(58), cx + s(58), cy + s(58)),
            -90,
            -90 + int(cpu_percent * 3.6),
            fill=cpu_color,
            width=s(10),
        )
    inner = (cx - s(38), cy - s(38), cx + s(38), cy + s(38))
    draw.ellipse(inner, fill="#0a0e14")
    _draw_centered_text(draw, str(server.get("cpu_label") or "n/a"), inner, fonts["metric"], "#ffffff")

    bar_x = x + s(142)
    bar_right = right - pad
    _draw_local_bar(
        image,
        draw,
        "内存",
        server.get("memory", {}),
        bar_x,
        metric_top + s(18),
        bar_right,
        fonts,
        "#4cc9f0",
        scale=scale,
    )
    _draw_local_bar(
        image,
        draw,
        "存储",
        server.get("storage", {}),
        bar_x,
        metric_top + s(76),
        bar_right,
        fonts,
        "#f6c55d",
        scale=scale,
    )

    io_top = metric_top + s(152)
    cell_gap = s(10)
    cell_w = int((right - left - pad * 2 - cell_gap) / 2)
    network = server.get("network", {}) if isinstance(server.get("network"), dict) else {}
    disk_io = server.get("disk_io", {}) if isinstance(server.get("disk_io"), dict) else {}
    cells = (
        ("入网", network.get("rx_label", "0 B/s")),
        ("出网", network.get("tx_label", "0 B/s")),
        ("磁盘读取", disk_io.get("read_label", "0 B/s")),
        ("磁盘写入", disk_io.get("write_label", "0 B/s")),
    )
    for idx, (label, value) in enumerate(cells):
        col = idx % 2
        row = idx // 2
        cell_left = x + col * (cell_w + cell_gap)
        cell_top = io_top + row * s(84)
        _draw_local_io_cell(
            image,
            draw,
            cell_left,
            cell_top,
            cell_w,
            s(70),
            label,
            str(value),
            fonts,
            scale=scale,
        )

    if server.get("error"):
        err_top = bottom - s(72)
        if status == "degraded":
            error_fill = "#31250f"
            error_outline = "#86662b"
            error_text = "#ffe1a0"
        else:
            error_fill = "#2b151c"
            error_outline = "#7f3342"
            error_text = "#ffb1be"
        draw.rounded_rectangle(
            (x, err_top, right - pad, err_top + s(50)),
            radius=s(8),
            fill=error_fill,
            outline=error_outline,
            width=max(1, s(1)),
        )
        draw.text(
            (x + s(12), err_top + s(15)),
            _fit_text(str(server.get("error")), fonts["body"], right - left - s(70)),
            fill=error_text,
            font=fonts["body"],
        )


def _draw_local_bar(
    image: Any,
    draw: Any,
    label: str,
    metric: Any,
    x: int,
    y: int,
    right: int,
    fonts: dict[str, Any],
    fill: str,
    *,
    scale: int = 1,
) -> None:
    from PIL import Image, ImageDraw, ImageFilter

    def s(value: float) -> int:
        return int(round(float(value) * max(1, scale)))

    metric = metric if isinstance(metric, dict) else {}
    value = str(metric.get("label") or "n/a")
    percent = _clamp_percent(_to_float(metric.get("percent")))
    draw.text((x, y), label, fill="#b0bac8", font=fonts["body_bold"])
    value_width = draw.textbbox((0, 0), value, font=fonts["body_bold"])[2]
    draw.text((right - value_width, y), value, fill="#ffffff", font=fonts["body_bold"])
    track_top = y + s(28)
    draw.rounded_rectangle((x, track_top, right, track_top + s(10)), radius=s(5), fill="#151b26")
    fill_right = x + int((right - x) * percent / 100)
    if fill_right > x:
        # Subtle glow behind the bar fill
        rgb = _hex_to_rgb(fill)
        glow = Image.new("RGBA", (right - x, s(10)), (0, 0, 0, 0))
        glow_draw = ImageDraw.Draw(glow)
        glow_draw.rounded_rectangle((0, 0, fill_right - x, s(10)), radius=s(5), fill=(*rgb, 55))
        glow = glow.filter(ImageFilter.GaussianBlur(radius=max(2.0, 6.0 * scale)))
        image.paste(glow, (x, track_top), glow)
        # Bar fill
        draw.rounded_rectangle((x, track_top, fill_right, track_top + s(10)), radius=s(5), fill=fill)
        # Top shine overlay
        shine_h = max(1, s(4))
        shine = Image.new("RGBA", (fill_right - x, shine_h), (255, 255, 255, 35))
        shine_mask = Image.new("L", (fill_right - x, shine_h), 0)
        shine_mask_draw = ImageDraw.Draw(shine_mask)
        shine_mask_draw.rounded_rectangle((0, 0, fill_right - x - 1, shine_h - 1), radius=max(1, s(2)), fill=255)
        image.paste(shine, (x, track_top + max(1, s(1))), shine_mask)


def _draw_local_io_cell(
    image: Any,
    draw: Any,
    left: int,
    top: int,
    width: int,
    height: int,
    label: str,
    value: str,
    fonts: dict[str, Any],
    *,
    scale: int = 1,
) -> None:
    def s(value_: float) -> int:
        return int(round(float(value_) * max(1, scale)))

    _draw_local_glass_panel(
        image,
        draw,
        (left, top, left + width, top + height),
        radius=s(10),
        fill="#0c1520",
        outline="#344a60",
        scale=scale,
        shadow=False,
        alpha=85,
    )
    draw.text((left + s(14), top + s(12)), label, fill="#6a7d94", font=fonts["label"])
    draw.text(
        (left + s(14), top + s(38)),
        _fit_text(value, fonts["metric"], width - s(28)),
        fill="#e8ecf1",
        font=fonts["metric"],
    )


def _draw_centered_text(draw: Any, text: str, box: tuple[int, int, int, int], font: Any, fill: str) -> None:
    left, top, right, bottom = box
    text_box = draw.textbbox((0, 0), text, font=font)
    text_width = text_box[2] - text_box[0]
    text_height = text_box[3] - text_box[1]
    x = left + max(0, (right - left - text_width) // 2)
    y = top + max(0, (bottom - top - text_height) // 2) - 1
    draw.text((x, y), text, fill=fill, font=font)


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    text = str(value or "").strip().lstrip("#")
    if len(text) == 3:
        text = "".join(char * 2 for char in text)
    if len(text) != 6:
        return (23, 36, 58)
    try:
        return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))
    except ValueError:
        return (23, 36, 58)


def _fit_text(text: str, font: Any, max_width: int) -> str:
    from PIL import Image, ImageDraw

    value = str(text or "")
    if max_width <= 0:
        return ""
    scratch = Image.new("RGB", (1, 1))
    draw = ImageDraw.Draw(scratch)
    if draw.textbbox((0, 0), value, font=font)[2] <= max_width:
        return value
    suffix = "..."
    for length in range(len(value), 0, -1):
        candidate = value[:length].rstrip() + suffix
        if draw.textbbox((0, 0), candidate, font=font)[2] <= max_width:
            return candidate
    return suffix


def _normalize_usage(value: Any) -> UsageMetric:
    if not isinstance(value, dict):
        percent = _clamp_percent(_to_float(value))
        return UsageMetric(percent=percent, label=_format_percent(percent))
    used = _bytes_value(value, ("used_bytes", "used", "used_memory"))
    total = _bytes_value(value, ("total_bytes", "total", "total_memory"))
    percent = _clamp_percent(_first_number(value, ("percent", "usage_percent", "used_percent")))
    if percent <= 0 and total > 0:
        percent = _clamp_percent(used / total * 100.0)
    label = (
        f"{_format_bytes(used)} / {_format_bytes(total)}"
        if total > 0
        else _format_percent(percent)
    )
    return UsageMetric(used_bytes=used, total_bytes=total, percent=percent, label=label)


def _normalize_storage(value: Any) -> UsageMetric:
    if isinstance(value, list):
        used = 0.0
        total = 0.0
        for item in value:
            if not isinstance(item, dict):
                continue
            used += _bytes_value(item, ("used_bytes", "used"))
            total += _bytes_value(item, ("total_bytes", "total"))
        percent = _clamp_percent(used / total * 100.0) if total > 0 else 0.0
        return UsageMetric(
            used_bytes=used,
            total_bytes=total,
            percent=percent,
            label=f"{_format_bytes(used)} / {_format_bytes(total)}" if total else "n/a",
        )
    return _normalize_usage(value)


def _normalize_network(value: Any) -> NetworkMetric:
    if not isinstance(value, dict):
        return NetworkMetric()
    rx = _first_number(value, ("rx_bps", "in_bps", "download_bps", "rx_bytes_per_sec"))
    tx = _first_number(value, ("tx_bps", "out_bps", "upload_bps", "tx_bytes_per_sec"))
    return NetworkMetric(
        rx_bps=rx,
        tx_bps=tx,
        rx_label=f"{_format_bytes(rx)}/s",
        tx_label=f"{_format_bytes(tx)}/s",
    )


def _normalize_disk_io(value: Any) -> DiskIoMetric:
    if not isinstance(value, dict):
        return DiskIoMetric()
    read = _first_number(value, ("read_bps", "read_bytes_per_sec", "read_rate"))
    write = _first_number(value, ("write_bps", "write_bytes_per_sec", "write_rate"))
    return DiskIoMetric(
        read_bps=read,
        write_bps=write,
        read_label=f"{_format_bytes(read)}/s",
        write_label=f"{_format_bytes(write)}/s",
    )


def _normalize_location(value: Any, fallback: str = "") -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, dict):
        city = str(value.get("city") or "").strip()
        region = str(value.get("region") or value.get("province") or "").strip()
        country = str(value.get("country") or value.get("country_code") or "").strip()
        parts = [part for part in (city, region, country) if part]
        if parts:
            return ", ".join(parts)
    return fallback or "Unknown"


def _normalize_system(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if not isinstance(value, dict):
        return "Unknown"
    os_name = str(value.get("os") or value.get("name") or value.get("distro") or "").strip()
    version = str(value.get("version") or value.get("release") or "").strip()
    kernel = str(value.get("kernel") or "").strip()
    left = " ".join(part for part in (os_name, version) if part).strip()
    if left and kernel:
        return f"{left} / {kernel}"
    return left or kernel or "Unknown"


def _parse_headers(value: Any) -> dict[str, str]:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            return {}
    if not isinstance(value, dict):
        return {}
    return {
        str(key): str(val)
        for key, val in value.items()
        if str(key).strip() and str(val).strip()
    }


def _parse_ports(value: Any) -> list[int]:
    if isinstance(value, int):
        value = [value]
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            value = []
        else:
            if text.startswith("["):
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    parsed = text
                value = parsed
            if isinstance(value, str):
                value = value.replace(";", ",").replace(" ", ",").split(",")
    if not isinstance(value, list | tuple | set):
        return []

    ports: list[int] = []
    for item in value:
        try:
            port = int(str(item).strip())
        except Exception:
            continue
        if 1 <= port <= 65535 and port not in ports:
            ports.append(port)
    return ports


def _first_number(value: Any, keys: tuple[str, ...]) -> float:
    if isinstance(value, dict):
        for key in keys:
            if key in value:
                return _to_float(value[key])
    return _to_float(value)


def _bytes_value(value: dict[str, Any], keys: tuple[str, ...]) -> float:
    for key in keys:
        if key in value:
            return _to_float(value[key])
    for suffix, multiplier in (
        ("_gb", 1024**3),
        ("_mb", 1024**2),
        ("_kb", 1024),
    ):
        for key in keys:
            derived = key.removesuffix("_bytes") + suffix
            if derived in value:
                return _to_float(value[derived]) * multiplier
    return 0.0


def _to_float(value: Any) -> float:
    try:
        if value is None or isinstance(value, dict | list):
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _clamp_percent(value: float) -> float:
    return max(0.0, min(float(value or 0.0), 100.0))


def _format_percent(value: float) -> str:
    if value <= 0:
        return "0%"
    if value >= 99.95:
        return "100%"
    return f"{value:.1f}%"


def _format_bytes(value: float) -> str:
    size = max(0.0, float(value or 0.0))
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    idx = 0
    while size >= 1024 and idx < len(units) - 1:
        size /= 1024
        idx += 1
    if idx == 0:
        return f"{size:.0f} {units[idx]}"
    if size >= 100:
        return f"{size:.0f} {units[idx]}"
    if size >= 10:
        return f"{size:.1f} {units[idx]}"
    return f"{size:.2f} {units[idx]}"


def _format_duration(seconds: float) -> str:
    total = int(seconds or 0)
    if total <= 0:
        return "n/a"
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _truncate(value: str, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _parse_target_session(target_session: str) -> tuple[str, str, str]:
    try:
        platform_id, message_type, session_id = str(target_session or "").split(":", 2)
    except ValueError as exc:
        raise RuntimeError(f"invalid target session: {target_session}") from exc
    if not platform_id or not message_type or not session_id:
        raise RuntimeError(f"invalid target session: {target_session}")
    return platform_id, message_type, session_id


def _session_numeric_id(session_id: str) -> str:
    value = str(session_id or "").rsplit("_", 1)[-1].strip()
    if not value.isdigit():
        raise RuntimeError(f"session id is not numeric: {session_id}")
    return value


def _onebot_image_file_value(image_ref: str) -> str:
    value = str(image_ref or "").strip()
    if value.startswith(("http://", "https://", "base64://")):
        return value
    path = _path_from_file_uri(value) if value.startswith("file://") else Path(value)
    if path.is_file():
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"base64://{encoded}"
    if value.startswith("file://"):
        return value
    return path.resolve().as_uri()


def _path_from_file_uri(value: str) -> Path:
    parsed = urlparse(value)
    path_text = url2pathname(parsed.path or "")
    if parsed.netloc:
        path_text = f"//{parsed.netloc}{path_text}"
    return Path(path_text)
