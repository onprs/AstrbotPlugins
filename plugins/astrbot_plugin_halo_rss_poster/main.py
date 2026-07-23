from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
from io import BytesIO
import json
import re
import tempfile
import time
from dataclasses import dataclass, replace
from datetime import datetime
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
import astrbot.api.message_components as Comp
from astrbot.api.star import Context, Star


TAG_RE = re.compile(r"<[^>]+>")
SCRIPT_STYLE_RE = re.compile(
    r"<(script|style)\b[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
PARAGRAPH_RE = re.compile(r"<p\b[^>]*>(.*?)</p>", re.IGNORECASE | re.DOTALL)
IMG_SRC_RE = re.compile(
    r"<img\b[^>]*\bsrc=[\"']([^\"']+)[\"']",
    re.IGNORECASE | re.DOTALL,
)
SPACE_RE = re.compile(r"\s+")
CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
WORD_RE = re.compile(r"[A-Za-z0-9]+(?:[-_'][A-Za-z0-9]+)*")
INT_RE = re.compile(r"\d[\d,]*")
PLUGIN_NAME = "astrbot_plugin_halo_rss_poster"
DEFAULT_RSS_URL = "http://any.onprs.top:8090/rss.xml"
DEFAULT_SITE_NAME = "ONPRS"
SUPPORTED_PLATFORMS = {"aiocqhttp"}
CANVAS_SIZE = (1600, 900)
CARD_TEMPLATE = """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <style>
    * {
      box-sizing: border-box;
    }
    body {
      width: 1600px;
      height: 900px;
      margin: 0;
      overflow: hidden;
      font-family: "Inter", "Noto Sans SC", "Microsoft YaHei", Arial, sans-serif;
      color: #f8fafc;
      background:
        radial-gradient(circle at 14% 12%, rgba(56, 189, 248, 0.42), transparent 28%),
        radial-gradient(circle at 84% 18%, rgba(244, 114, 182, 0.28), transparent 30%),
        linear-gradient(135deg, #0b1020 0%, #13251f 52%, #261622 100%);
    }
    .scene {
      display: flex;
      align-items: center;
      justify-content: center;
      position: relative;
      width: 1600px;
      height: 900px;
      padding: 72px 96px;
      isolation: isolate;
    }
    .cover {
      position: absolute;
      inset: 0;
      z-index: -3;
      background-image:
        linear-gradient(115deg, rgba(8, 13, 29, 0.82), rgba(8, 13, 29, 0.36) 50%, rgba(8, 13, 29, 0.76)),
        url("{{ cover_url | e }}");
      background-size: cover;
      background-position: center;
      transform: scale(1.02);
      filter: saturate(1.16) contrast(1.08);
    }
    .grain {
      position: absolute;
      inset: 0;
      z-index: -2;
      opacity: 0.13;
      background-image:
        linear-gradient(rgba(255,255,255,0.08) 1px, transparent 1px),
        linear-gradient(90deg, rgba(255,255,255,0.06) 1px, transparent 1px);
      background-size: 42px 42px;
      mask-image: linear-gradient(115deg, black, transparent 76%);
    }
    .card {
      position: relative;
      width: 1060px;
      min-height: 598px;
      padding: 48px 56px 50px;
      border: 1px solid rgba(255, 255, 255, 0.26);
      border-radius: 34px;
      background:
        linear-gradient(145deg, rgba(255, 255, 255, 0.24), rgba(255, 255, 255, 0.08)),
        rgba(10, 18, 34, 0.46);
      box-shadow:
        0 30px 90px rgba(0, 0, 0, 0.48),
        inset 0 1px 0 rgba(255, 255, 255, 0.30);
      backdrop-filter: blur(30px) saturate(1.38);
    }
    .site {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 18px;
      margin-bottom: 28px;
      color: rgba(226, 232, 240, 0.88);
      font-size: 28px;
      font-weight: 700;
      letter-spacing: 0;
    }
    .site span:last-child {
      max-width: 430px;
      overflow: hidden;
      color: rgba(203, 213, 225, 0.74);
      font-size: 22px;
      font-weight: 500;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    h1 {
      display: -webkit-box;
      margin: 0 0 26px;
      overflow: hidden;
      -webkit-box-orient: vertical;
      -webkit-line-clamp: 2;
      color: #ffffff;
      font-size: 76px;
      line-height: 1.04;
      font-weight: 840;
      letter-spacing: 0;
      text-wrap: balance;
    }
    .meta {
      display: flex;
      flex-wrap: wrap;
      gap: 14px;
      margin-bottom: 18px;
    }
    .pill {
      max-width: 100%;
      padding: 10px 16px;
      border: 1px solid rgba(255, 255, 255, 0.20);
      border-radius: 999px;
      background: rgba(15, 23, 42, 0.34);
      color: rgba(241, 245, 249, 0.88);
      font-size: 23px;
      line-height: 1.1;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .taxonomy {
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      margin: 0 0 24px;
      max-height: 98px;
      overflow: hidden;
    }
    .tax-chip {
      max-width: 300px;
      padding: 10px 15px;
      border: 1px solid rgba(255, 255, 255, 0.18);
      border-radius: 999px;
      background: rgba(15, 23, 42, 0.26);
      color: rgba(226, 232, 240, 0.84);
      font-size: 21px;
      line-height: 1.08;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .tax-chip.category {
      border-color: rgba(125, 211, 252, 0.38);
      background: rgba(14, 165, 233, 0.22);
      color: rgba(240, 249, 255, 0.94);
      font-weight: 700;
    }
    .tax-chip.tag {
      color: rgba(226, 232, 240, 0.78);
    }
    .summary {
      display: -webkit-box;
      margin: 0;
      overflow: hidden;
      -webkit-box-orient: vertical;
      -webkit-line-clamp: 3;
      color: rgba(226, 232, 240, 0.88);
      font-size: 32px;
      line-height: 1.42;
      letter-spacing: 0;
    }
  </style>
</head>
<body>
  <main class="scene">
    <div class="cover"></div>
    <div class="grain"></div>
    <section class="card">
      <div class="site">
        <span>{{ site_name | e }}</span>
        <span>{{ domain | e }}</span>
      </div>
      <h1>{{ title | e }}</h1>
      <div class="meta">
        <div class="pill">{{ author | e }}</div>
        <div class="pill">{{ word_count_label | e }}</div>
        <div class="pill">{{ published_label | e }}</div>
      </div>
      {% if taxonomy_chips %}
      <div class="taxonomy">
        {% for chip in taxonomy_chips %}
        <div class="tax-chip {{ chip.kind | e }}">{{ chip.label | e }}</div>
        {% endfor %}
      </div>
      {% endif %}
      <p class="summary">{{ summary | e }}</p>
    </section>
  </main>
</body>
</html>
"""


class FeedParseError(ValueError):
    """Raised when a fetched document is not a usable RSS or Atom feed."""


@dataclass(slots=True)
class ArticlePreview:
    article_id: str
    title: str
    link: str
    author: str
    categories: list[str]
    tags: list[str]
    published_raw: str
    published_dt: datetime | None
    summary: str
    content_text: str
    word_count: int
    word_count_estimated: bool
    cover_url: str


@dataclass(slots=True)
class CheckOutcome:
    found_count: int = 0
    new_count: int = 0
    pushed_count: int = 0
    recorded_count: int = 0
    error: str = ""


class HaloRssPosterPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        self.config = config or {}
        self._poll_task = None
        self._checking = False

    @filter.command_group("halorss")
    def halorss(self):
        """Halo RSS article preview poster."""

    @halorss.command("bind")
    async def bind(self, event: AstrMessageEvent):
        """Bind current OneBot v11 session as the push target."""
        if not self._cfg_bool("enable", True):
            yield event.plain_result("Halo RSS 推送插件未启用。")
            return
        if not self._is_allowed(event):
            yield event.plain_result("此命令默认仅 AstrBot 管理员可用。")
            return
        if not self._is_onebot_event(event):
            yield event.plain_result(
                "请在 OneBot v11(aiocqhttp) 群聊或私聊中使用 /halorss bind。"
            )
            return

        target = str(getattr(event, "unified_msg_origin", "") or "")
        await self.put_kv_data("target_session", target)
        await self.put_kv_data(
            "target_info",
            {
                "platform_name": self._event_platform_name(event),
                "sender_id": str(event.get_sender_id() or ""),
            },
        )
        yield event.plain_result(
            "已绑定当前 OneBot v11 会话为 Halo 新文章推送目标。\n"
            f"- target: {target}"
        )

    @halorss.command("reset")
    async def reset(self, event: AstrMessageEvent):
        """Clear target and polling state."""
        if not self._is_allowed(event):
            yield event.plain_result("此命令默认仅 AstrBot 管理员可用。")
            return
        await self.delete_kv_data("target_session")
        await self.delete_kv_data("target_info")
        await self.delete_kv_data("seen_ids")
        await self.delete_kv_data("stats")
        yield event.plain_result("已清空 Halo RSS 推送绑定和状态。")

    @halorss.command("status")
    async def status(self, event: AstrMessageEvent):
        """Show current binding, RSS, and polling state."""
        if not self._is_allowed(event):
            yield event.plain_result("此命令默认仅 AstrBot 管理员可用。")
            return
        target = await self._load_target()
        seen = await self._load_seen()
        stats = await self.get_kv_data("stats", {}) or {}
        if not isinstance(stats, dict):
            stats = {}

        lines = [
            "Halo RSS 推送状态：",
            f"- enable: {self._cfg_bool('enable', True)}",
            f"- rss_url: {self._rss_url()}",
            f"- target: {target or '未绑定'}",
            f"- seen: {len(seen)}",
            f"- total_checks: {int(stats.get('total_checks', 0) or 0)}",
            f"- total_pushed: {int(stats.get('total_pushed', 0) or 0)}",
        ]
        last_check_at = str(stats.get("last_check_at", "") or "")
        if last_check_at:
            lines.append(f"- last_check_at: {last_check_at}")
        last_error = str(stats.get("last_error", "") or "")
        if last_error:
            lines.append(f"- last_error: {_truncate(last_error, 240)}")
        yield event.plain_result("\n".join(lines))

    @halorss.command("check")
    async def check(self, event: AstrMessageEvent):
        """Run one check immediately."""
        if not self._is_allowed(event):
            yield event.plain_result("此命令默认仅 AstrBot 管理员可用。")
            return
        outcome = await self.check_once()
        yield event.plain_result(self._format_outcome(outcome))

    @halorss.command("latest")
    async def latest(self, event: AstrMessageEvent):
        """Push the newest RSS article preview immediately."""
        if not self._is_allowed(event):
            yield event.plain_result("此命令默认仅 AstrBot 管理员可用。")
            return
        target = await self._load_target()
        if not target:
            yield event.plain_result("尚未绑定推送目标，请先在目标会话发送 /halorss bind。")
            return
        try:
            articles = await self._fetch_articles()
            if not articles:
                yield event.plain_result("RSS 中没有可推送的文章。")
                return
            article = articles[0]
            await self._send_article(str(target), article)
            await self._save_seen(self._merge_seen(await self._load_seen(), [article.article_id]))
        except Exception as exc:
            logger.warning("[%s] latest push failed: %s", PLUGIN_NAME, exc, exc_info=True)
            yield event.plain_result(f"最新文章推送失败：{_truncate(str(exc), 240)}")
            return
        if False:
            yield event.plain_result("")

    async def initialize(self) -> None:
        if not self._cfg_bool("enable", True):
            return
        if self._poll_task is not None and not self._poll_task.done():
            return
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def terminate(self) -> None:
        task = self._poll_task
        self._poll_task = None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def check_once(self, *, push: bool = True) -> CheckOutcome:
        if not self._cfg_bool("enable", True):
            outcome = CheckOutcome(error="plugin disabled")
            await self._save_stats(outcome)
            return outcome
        if self._checking:
            return CheckOutcome(error="check already running")

        self._checking = True
        try:
            target = await self._load_target()
            if push and not target:
                outcome = CheckOutcome(error="no target session bound")
                await self._save_stats(outcome)
                return outcome

            articles = await self._fetch_articles()
            seen = await self._load_seen()
            seen_set = set(seen)
            new_articles = [
                article for article in articles if article.article_id not in seen_set
            ]
            outcome = CheckOutcome(
                found_count=len(articles),
                new_count=len(new_articles),
            )

            if not new_articles:
                await self._save_stats(outcome)
                return outcome

            is_first_run = not seen
            if is_first_run and not self._cfg_bool("push_existing_on_first_run", False):
                article_ids = [article.article_id for article in articles]
                await self._save_seen(self._merge_seen(seen, article_ids))
                outcome.recorded_count = len(article_ids)
                await self._save_stats(outcome)
                return outcome

            limit = self._cfg_int("max_articles_per_check", 3, 1, 20)
            to_push = list(reversed(new_articles[:limit]))
            pushed_ids: list[str] = []
            for article in to_push:
                await self._send_article(str(target), article)
                pushed_ids.append(article.article_id)

            if pushed_ids:
                await self._save_seen(self._merge_seen(seen, pushed_ids))
            outcome.pushed_count = len(pushed_ids)
            outcome.recorded_count = len(pushed_ids)
            await self._save_stats(outcome)
            return outcome
        except Exception as exc:
            logger.warning("[%s] RSS check failed: %s", PLUGIN_NAME, exc, exc_info=True)
            outcome = CheckOutcome(error=f"{type(exc).__name__}: {exc}")
            await self._save_stats(outcome)
            return outcome
        finally:
            self._checking = False

    async def _fetch_rss(self) -> str:
        timeout = aiohttp.ClientTimeout(
            total=self._cfg_int("request_timeout_seconds", 10, 1, 120)
        )
        headers = self._cfg_dict("request_headers") or None
        async with aiohttp.ClientSession(trust_env=True, timeout=timeout) as session:
            async with session.get(self._rss_url(), headers=headers) as response:
                text = await response.text()
                if response.status != 200:
                    detail = _truncate(_clean_html(text), 320)
                    raise FeedParseError(
                        f"RSS request failed with HTTP {response.status}: {detail}"
                    )
                return text

    async def _fetch_articles(self) -> list[ArticlePreview]:
        feed_text = await self._fetch_rss()
        return parse_rss_articles(
            feed_text,
            self._rss_url(),
            default_author=self._site_name(),
        )

    async def _poll_loop(self) -> None:
        interval = self._cfg_int("poll_interval_seconds", 300, 30, 86400)
        while True:
            try:
                if await self._load_target():
                    await self.check_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "[%s] background polling failed: %s",
                    PLUGIN_NAME,
                    exc,
                    exc_info=True,
                )
            await asyncio.sleep(interval)

    async def _send_article(
        self,
        target_session: str,
        article: ArticlePreview,
    ) -> None:
        article = await self._enrich_article_taxonomy(article)
        send_mode = self._image_send_mode()
        image_ref = await self._render_article_preview(
            article,
            return_url=send_mode == "onebot_url",
        )
        if send_mode in {"onebot_url", "onebot_file", "onebot_base64"}:
            await self._send_onebot_image_raw(target_session, image_ref)
            return

        chain = MessageChain(
            [
                Comp.Image.fromFileSystem(image_ref),
            ]
        )
        sent = await self.context.send_message(target_session, chain)
        if sent is False:
            raise RuntimeError(f"target session not found: {target_session}")

    async def _render_article_preview(
        self,
        article: ArticlePreview,
        *,
        return_url: bool = False,
    ) -> str:
        if self._render_mode() == "local_pillow":
            data = self._build_render_data(article)
            cover_image = await self._fetch_cover_image(data["cover_url"])
            return self._render_article_preview_local(data, cover_image)

        return await self.html_render(
            CARD_TEMPLATE,
            await self._build_render_data_for_render(article),
            return_url=return_url,
            options={
                "type": "jpeg",
                "quality": self._cfg_int("image_quality", 88, 30, 95),
                "full_page": True,
                "clip": {"x": 0, "y": 0, "width": 1600, "height": 900},
                "scale": "css",
            },
        )

    async def _fetch_cover_image(self, cover_url: str) -> bytes | None:
        cover_url = str(cover_url or "").strip()
        if not cover_url:
            return None
        retries = self._cfg_int("cover_download_retries", 2, 1, 5)
        last_error: Exception | None = None
        for attempt in range(retries):
            try:
                return await self._fetch_cover_image_once(cover_url)
            except Exception as exc:
                last_error = exc
                if attempt + 1 < retries:
                    await asyncio.sleep(min(0.2 * (attempt + 1), 0.8))
        if last_error is not None and self._cfg_bool("debug_log", False):
            logger.warning(
                "[%s] cover image fetch failed: %s",
                PLUGIN_NAME,
                last_error,
                exc_info=True,
            )
        return None

    async def _fetch_cover_image_once(self, cover_url: str) -> bytes:
        timeout = aiohttp.ClientTimeout(
            total=self._cfg_int("cover_download_timeout_seconds", 8, 1, 60)
        )
        headers = self._cfg_dict("request_headers") or None
        async with aiohttp.ClientSession(trust_env=True, timeout=timeout) as session:
            async with session.get(cover_url, headers=headers) as response:
                if response.status != 200:
                    raise FeedParseError(
                        f"cover image request failed with HTTP {response.status}"
                    )
                image_bytes = await response.read()

        max_bytes = self._cfg_int(
            "max_cover_bytes",
            5 * 1024 * 1024,
            64 * 1024,
            12 * 1024 * 1024,
        )
        if len(image_bytes) > max_bytes:
            raise FeedParseError(
                f"cover image is too large: {len(image_bytes)} bytes"
            )
        if not _image_mime_type("", image_bytes):
            raise FeedParseError("cover response is not a supported image")
        return image_bytes

    def _render_article_preview_local(
        self,
        data: dict[str, Any],
        cover_image: bytes | None,
    ) -> str:
        try:
            from PIL import Image, ImageDraw, ImageFilter, ImageFont
        except Exception as exc:
            raise RuntimeError(
                "local_pillow render requires Pillow; install pillow or set render_mode=remote_html"
            ) from exc

        base_width, base_height = CANVAS_SIZE
        scale = self._local_render_scale()
        width, height = base_width * scale, base_height * scale
        draw_data = _scale_render_data(data, scale)
        background = _make_local_background(Image, cover_image, width, height, data)
        overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        _draw_soft_spots(draw, width, height)
        _draw_grid(draw, width, height)

        card = _make_glass_card(Image, width=1060 * scale, height=620 * scale, radius=34 * scale)
        card_x = (width - card.width) // 2
        card_y = (height - card.height) // 2
        overlay.alpha_composite(card, (card_x, card_y))

        font_path = self._local_render_font_path()
        fonts = _LocalFontSet(ImageFont, font_path)
        content_draw = ImageDraw.Draw(overlay)
        x = card_x + 56 * scale
        y = card_y + 48 * scale
        right = card_x + card.width - 56 * scale
        layout = _compute_local_card_layout(
            content_draw,
            fonts,
            draw_data,
            x=x,
            y=y,
            right=right,
            card_bottom=card_y + card.height - 44 * scale,
            scale=scale,
        )

        _draw_text_fit(
            content_draw,
            str(draw_data.get("site_name", "")),
            (x, layout["site"][0]),
            fonts.bold(28 * scale),
            fill=(226, 232, 240, 230),
            max_width=420 * scale,
        )
        domain = str(draw_data.get("domain", ""))
        domain_font = fonts.regular(22 * scale)
        domain_w = _text_width(content_draw, domain, domain_font)
        _draw_text_fit(
            content_draw,
            domain,
            (max(x, right - domain_w), layout["site"][0] + 5 * scale),
            domain_font,
            fill=(203, 213, 225, 190),
            max_width=430 * scale,
        )

        for line, line_y in zip(layout["title_lines"], layout["title_line_ys"]):
            content_draw.text(
                (x, line_y),
                line,
                font=layout["title_font"],
                fill=(255, 255, 255, 255),
            )

        meta_values = [
            str(draw_data.get("author", "")),
            str(draw_data.get("word_count_label", "")),
            str(draw_data.get("published_label", "")),
        ]
        _draw_pills(content_draw, meta_values, x, layout["meta"][0], right, fonts.regular(23 * scale), scale=scale)

        chips = [
            str(chip.get("label", ""))
            for chip in draw_data.get("taxonomy_chips", [])
            if isinstance(chip, dict)
        ]
        _draw_pills(
            content_draw,
            chips,
            x,
            layout["taxonomy"][0],
            right,
            fonts.regular(21 * scale),
            category_first=True,
            max_height=98 * scale,
            scale=scale,
        )

        for line, line_y in zip(layout["summary_lines"], layout["summary_line_ys"]):
            content_draw.text(
                (x, line_y),
                line,
                font=layout["summary_font"],
                fill=(226, 232, 240, 225),
            )

        result = Image.alpha_composite(background.convert("RGBA"), overlay)
        if scale != 1:
            result = result.resize((base_width, base_height), Image.Resampling.LANCZOS)
        result = result.convert("RGB")
        output_dir = Path(
            str(self._cfg("local_render_output_dir", "") or "").strip()
            or Path(tempfile.gettempdir()) / PLUGIN_NAME
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha1(
            f"{data.get('title','')}|{data.get('published_label','')}|{time.time_ns()}".encode(
                "utf-8",
                "ignore",
            )
        ).hexdigest()[:16]
        path = output_dir / f"halo-rss-preview-{digest}.jpg"
        result.save(
            path,
            format="JPEG",
            quality=self._cfg_int("image_quality", 88, 30, 95),
            optimize=True,
            progressive=True,
        )
        return str(path)

    async def _send_onebot_image_raw(self, target_session: str, image_ref: str) -> None:
        platform_id, message_type, session_id = _parse_target_session(target_session)
        bot = self._find_onebot_bot(platform_id)
        payload = [
            {
                "type": "image",
                "data": {
                    "file": _onebot_image_file_value(image_ref, self._image_send_mode()),
                    "cache": 0,
                },
            }
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
            if getattr(meta, "id", "") != platform_id:
                continue
            bot = getattr(platform, "bot", None)
            if bot is not None:
                return bot
        raise RuntimeError(f"cannot find aiocqhttp bot for platform: {platform_id}")

    def _build_render_data(self, article: ArticlePreview) -> dict[str, Any]:
        return {
            "site_name": self._site_name(),
            "title": article.title,
            "author": article.author,
            "summary": _truncate(article.summary, self._cfg_int("summary_chars", 120, 40, 260)),
            "cover_url": self._cover_url(article),
            "taxonomy_chips": self._taxonomy_chips(article),
            "word_count_label": self._word_count_label(article),
            "published_label": self._format_datetime(article),
            "domain": self._article_domain(article),
        }

    async def _build_render_data_for_render(
        self,
        article: ArticlePreview,
    ) -> dict[str, Any]:
        data = self._build_render_data(article)
        if self._cfg_bool("embed_cover_image", True):
            data["cover_url"] = await self._cover_data_url(data["cover_url"])
        return data

    async def _cover_data_url(self, cover_url: str) -> str:
        cover_url = str(cover_url or "").strip()
        if not cover_url or cover_url.startswith("data:"):
            return cover_url

        try:
            timeout = aiohttp.ClientTimeout(
                total=self._cfg_int("cover_download_timeout_seconds", 8, 1, 60)
            )
            headers = self._cfg_dict("request_headers") or None
            async with aiohttp.ClientSession(trust_env=True, timeout=timeout) as session:
                async with session.get(cover_url, headers=headers) as response:
                    if response.status != 200:
                        raise FeedParseError(
                            f"cover image request failed with HTTP {response.status}"
                        )
                    image_bytes = await response.read()

            max_bytes = self._cfg_int(
                "max_cover_bytes",
                5 * 1024 * 1024,
                64 * 1024,
                12 * 1024 * 1024,
            )
            if len(image_bytes) > max_bytes:
                raise FeedParseError(
                    f"cover image is too large: {len(image_bytes)} bytes"
                )
            mime_type = _image_mime_type(
                str(getattr(response, "headers", {}).get("Content-Type", "")),
                image_bytes,
            )
            if not mime_type:
                raise FeedParseError("cover response is not a supported image")
            encoded = base64.b64encode(image_bytes).decode("ascii")
            return f"data:{mime_type};base64,{encoded}"
        except Exception as exc:
            if self._cfg_bool("debug_log", False):
                logger.warning(
                    "[%s] cover image embed failed: %s",
                    PLUGIN_NAME,
                    exc,
                    exc_info=True,
                )
            return cover_url

    async def _enrich_article_taxonomy(self, article: ArticlePreview) -> ArticlePreview:
        if not self._cfg_bool("fetch_article_taxonomy", True) or not article.link:
            return article
        article_url = _rewrite_url_to_rss_origin(article.link, self._rss_url())
        try:
            html_text = await self._fetch_article_html(article_url)
        except Exception as exc:
            if self._cfg_bool("debug_log", False):
                logger.warning(
                    "[%s] article taxonomy fetch failed: %s",
                    PLUGIN_NAME,
                    exc,
                    exc_info=True,
                )
            return article

        categories, tags = _extract_taxonomy_from_html(html_text)
        content_text, word_count = _extract_article_content_from_html(html_text)
        if not categories and not tags and not content_text and word_count <= 0:
            return article
        return replace(
            article,
            categories=categories or article.categories,
            tags=tags or article.tags,
            content_text=content_text or article.content_text,
            word_count=word_count or (
                _estimate_word_count(content_text)
                if content_text
                else article.word_count
            ),
            word_count_estimated=False if word_count > 0 or content_text else article.word_count_estimated,
        )

    async def _fetch_article_html(self, article_url: str) -> str:
        timeout = aiohttp.ClientTimeout(
            total=self._cfg_int("request_timeout_seconds", 10, 1, 120)
        )
        headers = self._cfg_dict("request_headers") or None
        async with aiohttp.ClientSession(trust_env=True, timeout=timeout) as session:
            async with session.get(article_url, headers=headers) as response:
                text = await response.text()
                if response.status != 200:
                    detail = _truncate(_clean_html(text), 240)
                    raise FeedParseError(
                        f"article page request failed with HTTP {response.status}: {detail}"
                    )
                return text

    def _cover_url(self, article: ArticlePreview) -> str:
        cover_url = str(article.cover_url or "").strip()
        if not cover_url or not self._cfg_bool("rewrite_cover_to_rss_origin", True):
            return _upgrade_halo_thumbnail_size(
                cover_url,
                str(self._cfg("cover_thumbnail_size", "l") or "l"),
            )
        rewritten = _rewrite_cover_to_rss_origin(cover_url, self._rss_url(), article.link)
        return _upgrade_halo_thumbnail_size(
            rewritten,
            str(self._cfg("cover_thumbnail_size", "l") or "l"),
        )

    def _taxonomy_chips(self, article: ArticlePreview) -> list[dict[str, str]]:
        chips: list[dict[str, str]] = []
        categories = _dedupe_texts(article.categories)
        tags = _dedupe_texts(article.tags)
        category_keys = {_taxonomy_key(category) for category in categories}

        for category in categories[:1]:
            chips.append({"kind": "category", "label": f"分类: {category}"})

        shown_tags = 0
        max_tags = self._cfg_int("max_tag_chips", 4, 0, 12)
        for tag in tags:
            if _taxonomy_key(tag) in category_keys:
                continue
            chips.append({"kind": "tag", "label": f"#{tag}"})
            shown_tags += 1
            if shown_tags >= max_tags:
                break
        return chips

    def _word_count_label(self, article: ArticlePreview) -> str:
        prefix = "约 " if article.word_count_estimated else ""
        return f"{prefix}{max(article.word_count, 0)} 字"

    def _format_datetime(self, article: ArticlePreview) -> str:
        if article.published_dt is None:
            return article.published_raw or "未知时间"
        timezone = str(self._cfg("timezone", "Asia/Shanghai") or "Asia/Shanghai")
        zone = None
        try:
            zone = ZoneInfo(timezone)
        except Exception:
            zone = None
        dt = article.published_dt
        if zone is not None and dt.tzinfo is None:
            dt = dt.replace(tzinfo=zone)
        elif zone is not None:
            dt = dt.astimezone(zone)
        return dt.strftime("%Y-%m-%d %H:%M")

    def _article_domain(self, article: ArticlePreview) -> str:
        for value in (article.link, self._rss_url()):
            parsed = urlparse(value)
            if parsed.netloc:
                return parsed.netloc
        return self._site_name()

    async def _load_target(self) -> str:
        return str(await self.get_kv_data("target_session", "") or "")

    async def _load_seen(self) -> list[str]:
        seen = await self.get_kv_data("seen_ids", [])
        if not isinstance(seen, list):
            return []
        return [str(item) for item in seen if str(item).strip()]

    async def _save_seen(self, seen: list[str]) -> None:
        await self.put_kv_data("seen_ids", seen)

    async def _save_stats(self, outcome: CheckOutcome) -> None:
        stats = await self.get_kv_data("stats", {}) or {}
        if not isinstance(stats, dict):
            stats = {}
        stats["last_check_at"] = time.strftime(
            "%Y-%m-%d %H:%M:%S",
            time.localtime(),
        )
        stats["last_found_count"] = outcome.found_count
        stats["last_new_count"] = outcome.new_count
        stats["last_pushed_count"] = outcome.pushed_count
        stats["last_recorded_count"] = outcome.recorded_count
        stats["last_error"] = outcome.error
        stats["total_checks"] = int(stats.get("total_checks", 0) or 0) + 1
        stats["total_pushed"] = (
            int(stats.get("total_pushed", 0) or 0) + outcome.pushed_count
        )
        await self.put_kv_data("stats", stats)

    def _format_outcome(self, outcome: CheckOutcome) -> str:
        lines = [
            "Halo RSS 检查完成。",
            f"- found: {outcome.found_count}",
            f"- new: {outcome.new_count}",
            f"- pushed: {outcome.pushed_count}",
            f"- recorded: {outcome.recorded_count}",
        ]
        if outcome.error:
            lines.append(f"- error: {_truncate(outcome.error, 300)}")
        return "\n".join(lines)

    def _merge_seen(self, existing: list[str], article_ids: list[str]) -> list[str]:
        max_items = self._cfg_int("max_seen_items", 200, 20, 5000)
        merged: list[str] = []
        used: set[str] = set()
        for article_id in [*article_ids, *existing]:
            key = str(article_id or "").strip()
            if not key or key in used:
                continue
            used.add(key)
            merged.append(key)
            if len(merged) >= max_items:
                break
        return merged

    def _rss_url(self) -> str:
        return str(self._cfg("rss_url", DEFAULT_RSS_URL) or DEFAULT_RSS_URL).strip()

    def _site_name(self) -> str:
        return str(self._cfg("site_name", DEFAULT_SITE_NAME) or DEFAULT_SITE_NAME).strip()

    def _image_send_mode(self) -> str:
        mode = str(self._cfg("image_send_mode", "onebot_base64") or "onebot_base64")
        mode = mode.strip().lower()
        if mode in {"onebot_url", "onebot_file", "onebot_base64", "astrbot"}:
            return mode
        return "onebot_base64"

    def _render_mode(self) -> str:
        mode = str(self._cfg("render_mode", "local_pillow") or "local_pillow")
        mode = mode.strip().lower()
        if mode in {"local_pillow", "remote_html"}:
            return mode
        return "local_pillow"

    def _local_render_font_path(self) -> str:
        return str(self._cfg("local_render_font_path", "") or "").strip()

    def _local_render_scale(self) -> int:
        return self._cfg_int("local_render_scale", 2, 1, 3)

    def _is_onebot_event(self, event: AstrMessageEvent) -> bool:
        return self._event_platform_name(event) in SUPPORTED_PLATFORMS

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

    def _event_platform_name(self, event: AstrMessageEvent) -> str:
        get_platform_name = getattr(event, "get_platform_name", None)
        if not callable(get_platform_name):
            return ""
        try:
            return str(get_platform_name() or "")
        except Exception:
            return ""

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

    def _cfg_dict(self, key: str) -> dict[str, str]:
        value = self._cfg(key, {})
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return {}
            try:
                value = json.loads(text)
            except json.JSONDecodeError as exc:
                logger.warning(
                    "[%s] config %s is not valid JSON: %s",
                    PLUGIN_NAME,
                    key,
                    exc,
                )
                return {}
        if not isinstance(value, dict):
            return {}
        return {
            str(k): str(v)
            for k, v in value.items()
            if str(k).strip() and str(v).strip()
        }


def parse_rss_articles(
    xml_text: str,
    feed_url: str,
    default_author: str = "",
) -> list[ArticlePreview]:
    text = str(xml_text or "").strip()
    if not text:
        raise FeedParseError("RSS response is empty.")

    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise FeedParseError(f"RSS response is not valid XML: {exc}") from exc

    root_name = _local_name(root.tag)
    if root_name == "rss":
        items = list(root.findall(".//item"))
    elif root_name == "feed":
        items = [child for child in root if _local_name(child.tag) == "entry"]
    else:
        raise FeedParseError("RSS response is not a valid RSS or Atom feed.")

    articles: list[ArticlePreview] = []
    for item in items:
        article = _parse_item(item, feed_url, default_author)
        if article is not None:
            articles.append(article)
    return articles


def _parse_item(
    item: ElementTree.Element,
    feed_url: str,
    default_author: str,
) -> ArticlePreview | None:
    title = _clean_text(_child_text(item, ("title",)), 180)
    link = _extract_link(item, feed_url)
    published_raw = _clean_text(
        _child_text(item, ("pubDate", "published", "updated")),
        80,
    )
    article_id = _clean_text(_child_text(item, ("guid", "id")), 300)
    if not article_id:
        article_id = link or f"{title}|{published_raw}"
    if not article_id:
        return None

    author = _clean_text(
        _child_text(item, ("creator", "author", "name")),
        80,
    )
    if not author:
        author = default_author or "Unknown"

    content_html = _child_text(item, ("encoded", "content"))
    description_html = _child_text(item, ("description", "summary"))
    source_html = content_html or description_html
    content_text = _clean_html(source_html)
    summary = _first_paragraph(source_html) or _truncate(content_text, 120)
    word_count_text = content_text or _clean_html(description_html)
    word_count = _estimate_word_count(word_count_text)

    return ArticlePreview(
        article_id=article_id,
        title=title or "Untitled",
        link=link,
        author=author,
        categories=_child_texts(item, ("category",)),
        tags=_child_texts(item, ("tag", "tags", "keyword", "keywords")),
        published_raw=published_raw,
        published_dt=_parse_datetime(published_raw),
        summary=summary,
        content_text=content_text,
        word_count=word_count,
        word_count_estimated=not bool(content_html),
        cover_url=_extract_cover_url(item, source_html, link or feed_url),
    )


def _extract_link(item: ElementTree.Element, feed_url: str) -> str:
    link = _clean_text(_child_text(item, ("link",)), 500)
    if link:
        return urljoin(feed_url, link)
    for child in item:
        if _local_name(child.tag) == "link":
            href = str(child.attrib.get("href") or "").strip()
            if href:
                return urljoin(feed_url, href)
    return ""


def _extract_cover_url(
    item: ElementTree.Element,
    html_text: str,
    base_url: str,
) -> str:
    for child in item:
        name = _local_name(child.tag)
        url = str(child.attrib.get("url") or "").strip()
        if not url:
            continue
        media_hint = (
            str(child.attrib.get("medium") or "").lower() == "image"
            or str(child.attrib.get("type") or "").lower().startswith("image/")
        )
        if name in {"thumbnail", "content"} and media_hint:
            return urljoin(base_url, url)
        if name == "enclosure" and media_hint:
            return urljoin(base_url, url)

    match = IMG_SRC_RE.search(str(html_text or ""))
    if match:
        return urljoin(base_url, unescape(match.group(1)).strip())
    return ""


def _child_text(item: ElementTree.Element, names: tuple[str, ...]) -> str:
    wanted = set(names)
    for child in item.iter():
        if child is item:
            continue
        if _local_name(child.tag) not in wanted:
            continue
        text = "".join(child.itertext()).strip()
        if text:
            return unescape(text)
    return ""


def _child_texts(
    item: ElementTree.Element,
    names: tuple[str, ...],
    limit: int = 80,
) -> list[str]:
    wanted = set(names)
    values: list[str] = []
    for child in item.iter():
        if child is item:
            continue
        if _local_name(child.tag) not in wanted:
            continue
        text = _clean_text("".join(child.itertext()), limit)
        if text:
            values.append(text)
    return _dedupe_texts(values)


def _first_paragraph(html_text: str) -> str:
    for match in PARAGRAPH_RE.finditer(str(html_text or "")):
        text = _clean_html(match.group(1))
        if text:
            return _truncate(text, 140)
    return ""


def _clean_html(value: Any) -> str:
    text = unescape(str(value or ""))
    text = SCRIPT_STYLE_RE.sub(" ", text)
    text = TAG_RE.sub(" ", text)
    return SPACE_RE.sub(" ", text).strip()


def _clean_text(value: Any, limit: int = 0) -> str:
    text = _clean_html(value)
    if limit > 0:
        return _truncate(text, limit)
    return text


def _truncate(value: str, limit: int) -> str:
    text = SPACE_RE.sub(" ", str(value or "")).strip()
    if limit <= 0 or len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _estimate_word_count(text: str) -> int:
    value = str(text or "")
    cjk_count = len(CJK_RE.findall(value))
    without_cjk = CJK_RE.sub(" ", value)
    word_count = len(WORD_RE.findall(without_cjk))
    return cjk_count + word_count


def _parse_datetime(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return parsedate_to_datetime(text)
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


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


def _onebot_image_file_value(image_ref: str, send_mode: str = "") -> str:
    value = str(image_ref or "").strip()
    if (
        str(send_mode or "").strip().lower() == "onebot_base64"
        and not value.startswith(("http://", "https://", "file://", "base64://"))
    ):
        return "base64://" + base64.b64encode(Path(value).read_bytes()).decode("ascii")
    if value.startswith(("http://", "https://", "file://", "base64://")):
        return value
    return Path(value).resolve().as_uri()


class _TaxonomyHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.categories: list[str] = []
        self.tags: list[str] = []
        self._capture: list[dict[str, Any]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        attrs_dict = {str(key).lower(): str(value or "") for key, value in attrs}
        classes = set(attrs_dict.get("class", "").split())
        href = attrs_dict.get("href", "")
        kind = ""
        if "post-meta-categories" in classes and "/categories/" in href:
            kind = "category"
        elif "post-meta__tags" in classes and "/tags/" in href:
            kind = "tag"
        if not kind:
            return
        if kind == "tag" and not attrs_dict.get("title", "").strip():
            return
        self._capture.append(
            {
                "kind": kind,
                "title": attrs_dict.get("title", ""),
                "text": [],
            }
        )

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._capture[-1]["text"].append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or not self._capture:
            return
        entry = self._capture.pop()
        value = _clean_text(entry["title"] or "".join(entry["text"]), 80)
        if not value:
            return
        if entry["kind"] == "category":
            self.categories.append(value)
        else:
            self.tags.append(value)


def _extract_taxonomy_from_html(html_text: str) -> tuple[list[str], list[str]]:
    parser = _TaxonomyHtmlParser()
    try:
        parser.feed(str(html_text or ""))
        parser.close()
    except Exception:
        return [], []
    return _dedupe_texts(parser.categories), _dedupe_texts(parser.tags)


def _rewrite_url_to_rss_origin(url: str, rss_url: str) -> str:
    target = urlparse(str(url or "").strip())
    rss = urlparse(str(rss_url or "").strip())
    if not target.scheme or not target.netloc:
        return urljoin(rss_url, url)
    if not rss.scheme or not rss.netloc:
        return url
    return urlunparse(
        (
            rss.scheme,
            rss.netloc,
            target.path,
            target.params,
            target.query,
            target.fragment,
        )
    )


def _rewrite_cover_to_rss_origin(
    cover_url: str,
    rss_url: str,
    article_link: str,
) -> str:
    cover = urlparse(str(cover_url or "").strip())
    rss = urlparse(str(rss_url or "").strip())
    article = urlparse(str(article_link or "").strip())
    if not cover.scheme or not cover.netloc:
        return cover_url
    if not rss.scheme or not rss.netloc:
        return cover_url
    if not article.netloc or cover.netloc != article.netloc:
        return cover_url
    if cover.netloc == rss.netloc:
        return cover_url
    return urlunparse(
        (
            rss.scheme,
            rss.netloc,
            cover.path,
            cover.params,
            cover.query,
            cover.fragment,
        )
    )


def _upgrade_halo_thumbnail_size(cover_url: str, size: str = "l") -> str:
    wanted_size = str(size or "l").strip().lower()
    if wanted_size not in {"s", "m", "l", "xl"}:
        wanted_size = "l"
    parsed = urlparse(str(cover_url or "").strip())
    if "/thumbnails/-/via-uri" not in parsed.path:
        return cover_url
    query = parse_qsl(parsed.query, keep_blank_values=True)
    replaced = False
    upgraded_query: list[tuple[str, str]] = []
    for key, value in query:
        if key == "size":
            upgraded_query.append((key, wanted_size))
            replaced = True
        else:
            upgraded_query.append((key, value))
    if not replaced:
        upgraded_query.append(("size", wanted_size))
    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            urlencode(upgraded_query),
            parsed.fragment,
        )
    )


def _image_mime_type(content_type: str, image_bytes: bytes) -> str:
    normalized = str(content_type or "").split(";", 1)[0].strip().lower()
    if normalized in {"image/jpeg", "image/png", "image/webp", "image/gif"}:
        return normalized
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image_bytes.startswith(b"GIF87a") or image_bytes.startswith(b"GIF89a"):
        return "image/gif"
    if len(image_bytes) >= 12 and image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    return ""


def _make_local_background(Image: Any, cover_image: bytes | None, width: int, height: int, data: dict[str, Any]) -> Any:
    from PIL import ImageDraw, ImageFilter

    if cover_image:
        try:
            with Image.open(BytesIO(cover_image)) as opened:
                background = opened.convert("RGB")
            background = _cover_resize(Image, background, width, height)
            blur_radius = max(4, int(width / CANVAS_SIZE[0] * 5))
            background = background.filter(ImageFilter.GaussianBlur(blur_radius))
            overlay = Image.new("RGBA", (width, height), (4, 8, 18, 0))
            draw = ImageDraw.Draw(overlay)
            draw.rectangle((0, 0, width, height), fill=(4, 8, 18, 116))
            draw.rectangle((0, 0, width, height), fill=(10, 16, 34, 64))
            return Image.alpha_composite(background.convert("RGBA"), overlay)
        except Exception:
            pass

    digest = hashlib.sha1(str(data.get("title", "")).encode("utf-8", "ignore")).digest()
    c1 = (8 + digest[0] % 34, 14 + digest[1] % 36, 32 + digest[2] % 38)
    c2 = (14 + digest[3] % 32, 40 + digest[4] % 48, 38 + digest[5] % 42)
    c3 = (36 + digest[6] % 44, 18 + digest[7] % 36, 44 + digest[8] % 44)
    background = Image.new("RGBA", (width, height), c1 + (255,))
    px = background.load()
    for y in range(height):
        yr = y / max(1, height - 1)
        for x in range(width):
            xr = x / max(1, width - 1)
            mix1 = (1 - xr) * (1 - yr)
            mix2 = xr * (1 - yr * 0.35)
            mix3 = yr * (0.55 + 0.45 * xr)
            total = mix1 + mix2 + mix3
            color = tuple(
                int((c1[i] * mix1 + c2[i] * mix2 + c3[i] * mix3) / total)
                for i in range(3)
            )
            px[x, y] = color + (255,)
    return background.filter(ImageFilter.GaussianBlur(1.5))


def _scale_render_data(data: dict[str, Any], scale: int) -> dict[str, Any]:
    del scale
    return dict(data)


def _compute_local_card_layout(
    draw: Any,
    fonts: Any,
    data: dict[str, Any],
    *,
    x: int,
    y: int,
    right: int,
    card_bottom: int,
    scale: int = 1,
) -> dict[str, Any]:
    content_width = right - x
    site_y = y
    title_y = y + 72 * scale

    title_size = 76 * scale
    title_lines: list[str] = []
    title_font = fonts.bold(title_size)
    while title_size >= 58 * scale:
        title_font = fonts.bold(title_size)
        title_lines = _wrap_text(
            draw,
            str(data.get("title", "")),
            title_font,
            max_width=content_width,
            max_lines=2,
        )
        if len(title_lines) <= 2:
            break
        title_size -= 4 * scale
    if not title_lines:
        title_lines = [""]

    title_line_step = int(title_size * 1.05)
    title_line_ys = [title_y + index * title_line_step for index in range(len(title_lines))]
    title_bottom = title_y + max(1, len(title_lines)) * title_line_step

    meta_y = title_bottom + 18 * scale
    meta_values = [
        str(data.get("author", "")),
        str(data.get("word_count_label", "")),
        str(data.get("published_label", "")),
    ]
    meta_height = _measure_pills_height(
        draw,
        meta_values,
        x,
        meta_y,
        right,
        fonts.regular(23 * scale),
        scale=scale,
    )
    meta_bottom = meta_y + meta_height

    chips = [
        str(chip.get("label", ""))
        for chip in data.get("taxonomy_chips", [])
        if isinstance(chip, dict)
    ]
    taxonomy_y = meta_bottom + 16 * scale
    taxonomy_height = _measure_pills_height(
        draw,
        chips,
        x,
        taxonomy_y,
        right,
        fonts.regular(21 * scale),
        max_height=98 * scale,
        scale=scale,
    ) if chips else 0
    taxonomy_bottom = taxonomy_y + taxonomy_height if chips else taxonomy_y

    summary_y = taxonomy_bottom + (20 * scale if chips else 18 * scale)
    available_summary = max(40 * scale, card_bottom - summary_y)
    summary_font_size = 32 * scale
    summary_font = fonts.regular(summary_font_size)
    max_summary_lines = max(1, min(3, available_summary // max(1, int(summary_font_size * 1.42))))
    summary_lines = _wrap_text(
        draw,
        str(data.get("summary", "")),
        summary_font,
        max_width=content_width,
        max_lines=max_summary_lines,
    )
    summary_step = int(summary_font_size * 1.42)
    summary_line_ys = [summary_y + index * summary_step for index in range(len(summary_lines))]
    summary_bottom = summary_y + len(summary_lines) * summary_step

    if summary_bottom > card_bottom and summary_lines:
        overflow = summary_bottom - card_bottom
        summary_y = max(taxonomy_bottom + 12 * scale, summary_y - overflow)
        summary_line_ys = [summary_y + index * summary_step for index in range(len(summary_lines))]
        summary_bottom = summary_y + len(summary_lines) * summary_step

    return {
        "site": (site_y, site_y + 34 * scale),
        "title": (title_y, title_bottom),
        "title_font": title_font,
        "title_lines": title_lines,
        "title_line_ys": title_line_ys,
        "meta": (meta_y, meta_bottom),
        "taxonomy": (taxonomy_y, taxonomy_bottom),
        "summary": (summary_y, summary_bottom),
        "summary_font": summary_font,
        "summary_lines": summary_lines,
        "summary_line_ys": summary_line_ys,
    }


def _cover_resize(Image: Any, image: Any, width: int, height: int) -> Any:
    src_w, src_h = image.size
    if src_w <= 0 or src_h <= 0:
        return Image.new("RGB", (width, height), (8, 14, 32))
    scale = max(width / src_w, height / src_h)
    resized = image.resize(
        (max(1, int(src_w * scale)), max(1, int(src_h * scale))),
        Image.Resampling.LANCZOS,
    )
    left = max(0, (resized.width - width) // 2)
    top = max(0, (resized.height - height) // 2)
    return resized.crop((left, top, left + width, top + height))


def _draw_soft_spots(draw: Any, width: int, height: int) -> None:
    scale = max(1, int(round(width / CANVAS_SIZE[0])))
    draw.ellipse(tuple(v * scale for v in (-140, -90, 560, 430)), fill=(56, 189, 248, 46))
    draw.ellipse(tuple(v * scale for v in (1040, -80, 1760, 460)), fill=(244, 114, 182, 34))
    draw.ellipse(tuple(v * scale for v in (760, 430, 1540, 1120)), fill=(16, 185, 129, 28))
    del height


def _draw_grid(draw: Any, width: int, height: int) -> None:
    scale = max(1, int(round(width / CANVAS_SIZE[0])))
    step = 42 * scale
    for x in range(0, width, step):
        draw.line((x, 0, x, height), fill=(255, 255, 255, 8), width=max(1, scale))
    for y in range(0, height, step):
        draw.line((0, y, width, y), fill=(255, 255, 255, 7), width=max(1, scale))


def _make_glass_card(Image: Any, width: int, height: int, radius: int) -> Any:
    from PIL import ImageDraw, ImageFilter

    card = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    shadow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    shadow_draw.rounded_rectangle(
        (12, 18, width - 12, height - 4),
        radius=radius,
        fill=(0, 0, 0, 118),
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(max(12, radius // 2)))
    card.alpha_composite(shadow)
    draw = ImageDraw.Draw(card)
    draw.rounded_rectangle(
        (0, 0, width - 1, height - 1),
        radius=radius,
        fill=(25, 33, 52, 168),
        outline=(255, 255, 255, 86),
        width=max(2, radius // 18),
    )
    gloss = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    gloss_draw = ImageDraw.Draw(gloss)
    for y in range(height):
        alpha = max(0, 58 - int(y * 0.10))
        if alpha:
            gloss_draw.line((0, y, width, y), fill=(255, 255, 255, alpha), width=1)
    mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, width - 1, height - 1), radius=radius, fill=255)
    card.alpha_composite(Image.composite(gloss, Image.new("RGBA", (width, height)), mask))
    return card


class _LocalFontSet:
    _cache: dict[tuple[str, int], Any] = {}

    def __init__(self, image_font: Any, configured_path: str = "") -> None:
        self.image_font = image_font
        self.configured_path = configured_path

    def regular(self, size: int) -> Any:
        return self._font(size, bold=False)

    def bold(self, size: int) -> Any:
        return self._font(size, bold=True)

    def _font(self, size: int, *, bold: bool) -> Any:
        for path in _font_candidates(self.configured_path, bold=bold):
            key = (path, size)
            if key in self._cache:
                return self._cache[key]
            try:
                font = self.image_font.truetype(path, size)
                self._cache[key] = font
                return font
            except Exception:
                continue
        return self.image_font.load_default()


def _font_candidates(configured_path: str, *, bold: bool) -> list[str]:
    configured = str(configured_path or "").strip()
    candidates: list[str] = []
    if configured:
        candidates.append(configured)
    base = [
        "/AstrBot/data/font.ttf",
        "/AstrBot/data/fonts/NotoSansCJK-Regular.ttc",
        "/AstrBot/data/fonts/NotoSansSC-Regular.otf",
        "/data/font.ttf",
        "data/font.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansSC-Regular.ttf",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/arphic/ukai.ttc",
        "/usr/share/fonts/truetype/arphic/uming.ttc",
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/msyhbd.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/simsun.ttc",
        "NotoSansCJK-Regular.ttc",
        "NotoSansSC-Regular.otf",
        "Microsoft YaHei.ttf",
        "msyh.ttc",
        "PingFang.ttc",
        "DejaVuSans.ttf",
    ]
    if bold:
        base = [
            "/AstrBot/data/font.ttf",
            "/AstrBot/data/fonts/NotoSansCJK-Bold.ttc",
            "/AstrBot/data/fonts/NotoSansSC-Bold.otf",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
            "/usr/share/fonts/truetype/noto/NotoSansSC-Bold.ttf",
            "NotoSansCJK-Bold.ttc",
            "NotoSansSC-Bold.otf",
            "msyhbd.ttc",
            *base,
        ]
    for item in base:
        if item not in candidates:
            candidates.append(item)
    return candidates


def _text_width(draw: Any, text: str, font: Any) -> int:
    if not text:
        return 0
    box = draw.textbbox((0, 0), text, font=font)
    return int(box[2] - box[0])


def _draw_text_fit(
    draw: Any,
    text: str,
    xy: tuple[int, int],
    font: Any,
    *,
    fill: tuple[int, int, int, int],
    max_width: int,
) -> None:
    value = _ellipsize_text(draw, text, font, max_width)
    draw.text(xy, value, font=font, fill=fill)


def _ellipsize_text(draw: Any, text: str, font: Any, max_width: int) -> str:
    value = str(text or "")
    if _text_width(draw, value, font) <= max_width:
        return value
    suffix = "..."
    while value and _text_width(draw, value + suffix, font) > max_width:
        value = value[:-1]
    return (value.rstrip() + suffix) if value else suffix


def _wrap_text(
    draw: Any,
    text: str,
    font: Any,
    *,
    max_width: int,
    max_lines: int,
) -> list[str]:
    value = SPACE_RE.sub(" ", str(text or "")).strip()
    if not value:
        return []
    lines: list[str] = []
    current = ""
    for char in value:
        candidate = current + char
        if current and _text_width(draw, candidate, font) > max_width:
            lines.append(current.rstrip())
            current = char
            if len(lines) >= max_lines:
                break
        else:
            current = candidate
    if len(lines) < max_lines and current.strip():
        lines.append(current.rstrip())
    if len(lines) > max_lines:
        lines = lines[:max_lines]
    if lines and _text_width(draw, lines[-1], font) > max_width:
        lines[-1] = _ellipsize_text(draw, lines[-1], font, max_width)
    if len(lines) == max_lines and len("".join(lines)) < len(value):
        lines[-1] = _ellipsize_text(draw, lines[-1], font, max_width)
    return lines


def _draw_pills(
    draw: Any,
    values: list[str],
    x: int,
    y: int,
    right: int,
    font: Any,
    *,
    category_first: bool = False,
    max_height: int = 140,
    scale: int = 1,
) -> int:
    return y + _measure_and_draw_pills(
        draw,
        values,
        x,
        y,
        right,
        font,
        category_first=category_first,
        max_height=max_height,
        scale=scale,
        draw_pills=True,
    )


def _measure_pills_height(
    draw: Any,
    values: list[str],
    x: int,
    y: int,
    right: int,
    font: Any,
    *,
    max_height: int = 140,
    scale: int = 1,
) -> int:
    return _measure_and_draw_pills(
        draw,
        values,
        x,
        y,
        right,
        font,
        max_height=max_height,
        scale=scale,
        draw_pills=False,
    )


def _measure_and_draw_pills(
    draw: Any,
    values: list[str],
    x: int,
    y: int,
    right: int,
    font: Any,
    *,
    category_first: bool = False,
    max_height: int = 140,
    scale: int = 1,
    draw_pills: bool = True,
) -> int:
    cursor_x = x
    cursor_y = y
    row_h = 44 * scale
    start_y = y
    drew_any = False
    for index, raw in enumerate(values):
        value = str(raw or "").strip()
        if not value:
            continue
        max_label_width = min(300 * scale, right - x - 40 * scale)
        label = _ellipsize_text(draw, value, font, max_label_width)
        w = _text_width(draw, label, font) + 32 * scale
        if cursor_x > x and cursor_x + w > right:
            cursor_x = x
            cursor_y += row_h + 12 * scale
        if cursor_y + row_h - start_y > max_height:
            break
        drew_any = True
        category = category_first and index == 0
        fill = (14, 165, 233, 58) if category else (15, 23, 42, 86)
        outline = (125, 211, 252, 96) if category else (255, 255, 255, 52)
        text_fill = (240, 249, 255, 240) if category else (226, 232, 240, 220)
        if draw_pills:
            draw.rounded_rectangle(
                (cursor_x, cursor_y, cursor_x + w, cursor_y + row_h),
                radius=22 * scale,
                fill=fill,
                outline=outline,
                width=max(1, scale),
            )
            draw.text((cursor_x + 16 * scale, cursor_y + 9 * scale), label, font=font, fill=text_fill)
        cursor_x += w + 12 * scale
    return (cursor_y + row_h - start_y) if drew_any else 0


class _ArticleContentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text_parts: list[str] = []
        self.word_count = 0
        self._capture_article = False
        self._article_depth = 0
        self._skip_depth = 0
        self._capture_word_count = False
        self._word_count_depth = 0
        self._word_count_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {str(key).lower(): str(value or "") for key, value in attrs}
        classes = set(attrs_dict.get("class", "").split())
        element_id = attrs_dict.get("id", "")
        if self._capture_article:
            self._article_depth += 1
            if tag.lower() in {"script", "style", "nav", "aside", "button"}:
                self._skip_depth += 1
        elif tag.lower() == "article" and (
            element_id == "article-container" or "post-content" in classes
        ):
            self._capture_article = True
            self._article_depth = 1
        if self._capture_word_count:
            self._word_count_depth += 1
        elif "word-count" in classes or "post-meta-wordcount" in classes:
            self._capture_word_count = True
            self._word_count_depth = 1
        if self._capture_article and tag.lower() in {"p", "br", "h1", "h2", "h3", "li"}:
            self.text_parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if self._skip_depth:
            self._skip_depth -= 1
        if self._capture_article:
            if tag.lower() in {"p", "h1", "h2", "h3", "li", "div", "section"}:
                self.text_parts.append(" ")
            self._article_depth -= 1
            if self._article_depth <= 0:
                self._capture_article = False
        if self._capture_word_count:
            self._word_count_depth -= 1
            if self._word_count_depth <= 0:
                self._capture_word_count = False
                text = "".join(self._word_count_text)
                matches = [int(match.replace(",", "")) for match in INT_RE.findall(text)]
                if matches:
                    self.word_count = max(matches)

    def handle_data(self, data: str) -> None:
        if self._capture_word_count:
            self._word_count_text.append(data)
        if self._capture_article and not self._skip_depth:
            self.text_parts.append(data)


def _extract_article_content_from_html(html_text: str) -> tuple[str, int]:
    parser = _ArticleContentParser()
    try:
        parser.feed(str(html_text or ""))
        parser.close()
    except Exception:
        return "", 0
    text = SPACE_RE.sub(" ", "".join(parser.text_parts)).strip()
    text = re.sub(r"([。！？；：，、])\s+(?=[\u3400-\u9fff])", r"\1", text)
    return text, parser.word_count


def _dedupe_texts(values: list[str]) -> list[str]:
    result: list[str] = []
    used: set[str] = set()
    for value in values:
        text = _clean_text(value, 80)
        key = _taxonomy_key(text)
        if not text or key in used:
            continue
        used.add(key)
        result.append(text)
    return result


def _taxonomy_key(value: str) -> str:
    return SPACE_RE.sub(" ", str(value or "")).strip().casefold()


def _local_name(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag
