"""Halo RSS 新文章预览推送插件。

轮询 Halo 博客 RSS，发现新文章后在本地用 Pillow 渲染一张玻璃拟态
预览图，并推送到绑定的 OneBot v11(aiocqhttp) 群聊或私聊。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import functools
import hashlib
import json
import random
import re
import tempfile
import time
from dataclasses import dataclass, replace
from datetime import datetime
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import aiohttp

try:
    import astrbot.api.message_components as Comp
    from astrbot.api import logger
    from astrbot.api.event import AstrMessageEvent, MessageChain, filter
    from astrbot.api.star import Context, Star
except ImportError:  # 允许在 AstrBot 环境外独立运行渲染器（如 render_sample.py）
    import logging

    logger = logging.getLogger(__name__)
    Comp = None
    AstrMessageEvent = Any
    MessageChain = None
    Context = Any

    class Star:  # type: ignore[no-redef]
        def __init__(self, context: Any = None, config: dict | None = None) -> None:
            self.context = context

    def _identity_decorator(*_args: Any, **_kwargs: Any) -> Any:
        def wrap(func: Any) -> Any:
            return func

        return wrap

    class _CommandGroupStub:
        def __init__(self, func: Any) -> None:
            self.func = func
            functools.update_wrapper(self, func)

        def command(self, *args: Any, **kwargs: Any) -> Any:
            return _identity_decorator(*args, **kwargs)

    class _FilterStub:
        @staticmethod
        def command_group(*_args: Any, **_kwargs: Any) -> Any:
            def wrap(func: Any) -> Any:
                return _CommandGroupStub(func)

            return wrap

    filter = _FilterStub()

try:
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont
except ImportError:  # pragma: no cover - 由渲染入口统一报错
    Image = None
    ImageDraw = None
    ImageEnhance = None
    ImageFilter = None
    ImageFont = None


PLUGIN_NAME = "astrbot_plugin_halo_rss_poster"
DEFAULT_RSS_URL = "http://any.onprs.top:8090/rss.xml"
DEFAULT_SITE_NAME = "ONPRS"
SUPPORTED_PLATFORMS = {"aiocqhttp"}

CANVAS_W = 1600
CANVAS_H = 900
# 卡片相对画布的内边距
CARD_MARGIN_X = 150
CARD_MARGIN_Y = 96
CARD_PAD_X = 72
CARD_PAD_TOP = 56
CARD_PAD_BOTTOM = 52
CARD_RADIUS = 36

TAG_RE = re.compile(r"<[^>]+>")
SCRIPT_STYLE_RE = re.compile(
    r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL
)
PARAGRAPH_RE = re.compile(r"<p\b[^>]*>(.*?)</p>", re.IGNORECASE | re.DOTALL)
IMG_SRC_RE = re.compile(
    r"<img\b[^>]*\bsrc=[\"']([^\"']+)[\"']", re.IGNORECASE | re.DOTALL
)
SPACE_RE = re.compile(r"\s+")
CJK_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
WORD_RE = re.compile(r"[A-Za-z0-9]+(?:[-_'][A-Za-z0-9]+)*")


class FeedParseError(ValueError):
    """拉取到的内容不是可用的 RSS/Atom 时抛出。"""


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


# ---------------------------------------------------------------------------
# 本地渲染器
# ---------------------------------------------------------------------------


class _FontBook:
    """按字号缓存的字体集合，自动寻找可用的中文字体。

    查找顺序与 AstrBot 官方 FontManager 一致：
    1. 插件配置里指定的绝对路径；
    2. <AstrBot根>/data/font.ttf（粗体用 data/font-bold.ttf）；
    3. 常见绝对路径；
    4. 按字体名让 Pillow 在系统字体目录搜索（Windows/macOS/常见 Linux 包）。
    """

    def __init__(self, configured_path: str = "") -> None:
        self._regular, self._bold = self._resolve(configured_path)
        self._cache: dict[tuple[str, int], Any] = {}

    @staticmethod
    def _astrbot_data_dir() -> str | None:
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            return get_astrbot_data_path()
        except Exception:
            return None

    @classmethod
    def _resolve(cls, configured_path: str) -> tuple[str | None, str | None]:
        data_dir = cls._astrbot_data_dir()
        data_font = str(Path(data_dir) / "font.ttf") if data_dir else None
        data_bold = str(Path(data_dir) / "font-bold.ttf") if data_dir else None

        regular_paths = [
            configured_path,
            data_font,
            "/AstrBot/data/font.ttf",
            "/AstrBot/data/fonts/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
            "C:/Windows/Fonts/msyh.ttc",
            "C:/Windows/Fonts/simhei.ttf",
            "/System/Library/Fonts/PingFang.ttc",
        ]
        bold_paths = [
            data_bold,
            "/AstrBot/data/font-bold.ttf",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
            "C:/Windows/Fonts/msyhbd.ttc",
            "C:/Windows/Fonts/simhei.ttf",
            "/System/Library/Fonts/PingFang.ttc",
        ]
        # 字体名兜底：交给 Pillow 在系统字体目录里找，适合服务器只装了字体包的情况
        regular_names = [
            "NotoSansCJK-Regular.ttc",
            "wqy-microhei.ttc",
            "msyh.ttc",
            "simhei.ttf",
            "PingFang.ttc",
        ]
        bold_names = [
            "NotoSansCJK-Bold.ttc",
            "msyhbd.ttc",
            "simhei.ttf",
            "PingFang.ttc",
        ]

        regular = next(
            (p for p in regular_paths if p and Path(p).is_file()), None
        ) or next((n for n in regular_names if cls._name_loadable(n)), None)
        bold = next((p for p in bold_paths if p and Path(p).is_file()), None) or next(
            (n for n in bold_names if cls._name_loadable(n)), None
        )
        return regular, bold

    @staticmethod
    def _name_loadable(name: str) -> bool:
        try:
            ImageFont.truetype(name, 16)
            return True
        except Exception:
            return False

    def get(self, size: int, *, bold: bool = False) -> Any:
        size = max(8, int(size))
        key = ("b" if bold else "r", size)
        if key in self._cache:
            return self._cache[key]
        candidates = []
        if bold:
            candidates = [self._bold, self._regular]
        else:
            candidates = [self._regular]
        font = None
        for path in candidates:
            if not path:
                continue
            try:
                font = ImageFont.truetype(path, size)
                break
            except Exception:
                continue
        if font is None:
            font = ImageFont.load_default()
        self._cache[key] = font
        return font


class PreviewRenderer:
    """把文章预览数据渲染为一张 1600x900 的 JPEG。

    以 scale 倍超采样绘制，最后 LANCZOS 缩回目标尺寸，减轻锯齿。
    """

    # 封面取色失败时的兜底调色板
    FALLBACK_PALETTE = [(56, 189, 248), (167, 139, 250), (244, 114, 182)]
    # 无封面时使用的明显调色板，保证背景有色彩
    NO_COVER_PALETTE = [(56, 189, 248), (99, 102, 241), (244, 114, 182)]

    def __init__(
        self,
        fonts: _FontBook,
        *,
        scale: int = 3,
        quality: int = 95,
        format: str = "jpeg",
        output_dir: Path | None = None,
    ) -> None:
        if Image is None:
            raise RuntimeError(
                "本地渲染依赖 Pillow，请先安装 pillow（见 requirements.txt）。"
            )
        self.fonts = fonts
        self.s = max(1, min(int(scale), 4))
        self.quality = quality
        self.format = "png" if str(format).lower() == "png" else "jpeg"
        self.output_dir = output_dir or (Path(tempfile.gettempdir()) / PLUGIN_NAME)

    # -- 对外入口 ---------------------------------------------------------

    def render(self, data: dict[str, Any], cover_bytes: bytes | None) -> str:
        s = self.s
        W, H = CANVAS_W * s, CANVAS_H * s
        rng = random.Random(str(data.get("title", "")))

        cover_img = self._load_cover(cover_bytes)
        palette = (
            self._palette_from_cover(cover_img)
            if cover_img is not None
            else list(self.NO_COVER_PALETTE)
        )

        canvas = self._make_background(W, H, cover_img, palette, rng)
        # 深色基底上用“滤色”叠加光斑，颜色更通透
        glow_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        self._add_glows(glow_layer, W, H, palette, rng)
        canvas = self._screen(canvas, glow_layer)
        self._add_grid(canvas, W, H)

        card, off = self._make_card(W, H, palette)
        card_x = (W - card.width) // 2
        card_y = (H - card.height) // 2
        canvas.alpha_composite(card, (card_x, card_y))

        # 真正的玻璃拟态：把卡片底下的背景重度模糊后叠回卡片区域
        body = (
            card_x + off,
            card_y + off,
            card_x + off + card.width - 2 * off,
            card_y + off + card.height - 2 * off,
        )
        behind = canvas.crop(body).filter(ImageFilter.GaussianBlur(14 * s))
        behind = ImageEnhance.Brightness(behind).enhance(1.06)
        mask = Image.new("L", behind.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            [0, 0, behind.width, behind.height], radius=CARD_RADIUS * s, fill=110
        )
        canvas.paste(behind, body[:2], mask)
        # 叠回一圈边缘高光，恢复被模糊层覆盖的玻璃轮廓
        edge_only = Image.new("RGBA", card.size, (0, 0, 0, 0))
        ed2 = ImageDraw.Draw(edge_only)
        ed2.rounded_rectangle(
            [off, off, off + card.width - 2 * off, off + card.height - 2 * off],
            radius=CARD_RADIUS * s,
            outline=(255, 255, 255, 64),
            width=max(2, 2 * s),
        )
        canvas.alpha_composite(edge_only, (card_x, card_y))

        self._draw_content(
            canvas,
            data,
            x=card_x + off + CARD_PAD_X * s,
            y=card_y + off + CARD_PAD_TOP * s,
            right=card_x + off + (card.width - 2 * off) - CARD_PAD_X * s,
            bottom=card_y + off + (card.height - 2 * off) - CARD_PAD_BOTTOM * s,
        )

        # 缩回目标尺寸后再加细噪点，避免超采样把噪点放大成糊块
        if s != 1:
            canvas = canvas.resize((CANVAS_W, CANVAS_H), Image.Resampling.LANCZOS)
        self._add_noise(canvas, CANVAS_W, CANVAS_H, rng)
        return self._save(canvas.convert("RGB"), data)

    # -- 背景 -------------------------------------------------------------

    @staticmethod
    def _load_cover(cover_bytes: bytes | None) -> Any | None:
        if not cover_bytes:
            return None
        try:
            img = Image.open(BytesIO(cover_bytes))
            img.load()
            return img.convert("RGB")
        except Exception:
            return None

    def _palette_from_cover(self, cover_img: Any | None) -> list[tuple[int, int, int]]:
        if cover_img is None:
            return list(self.FALLBACK_PALETTE)
        try:
            small = cover_img.resize((3, 1), Image.Resampling.BILINEAR)
            pixels = list(small.getdata())
            colors = []
            for px in pixels:
                r, g, b = (int(px[0]), int(px[1]), int(px[2]))
                # 过暗/过灰的像素不适合做光斑，提亮并增加饱和
                mx = max(r, g, b, 1)
                if mx < 90:
                    boost = 90 / mx
                    r, g, b = (
                        min(255, int(r * boost)),
                        min(255, int(g * boost)),
                        min(255, int(b * boost)),
                    )
                colors.append((r, g, b))
            while len(colors) < 3:
                colors.append(self.FALLBACK_PALETTE[len(colors)])
            return colors[:3]
        except Exception:
            return list(self.FALLBACK_PALETTE)

    def _make_background(
        self,
        W: int,
        H: int,
        cover_img: Any | None,
        palette: list[tuple[int, int, int]],
        rng: random.Random,
    ) -> Any:
        if cover_img is not None:
            base = self._cover_fill(cover_img, W, H)
            base = base.filter(ImageFilter.GaussianBlur(6 * self.s))
            base = ImageEnhance.Color(base).enhance(1.12)
            base = ImageEnhance.Brightness(base).enhance(0.92)
            base = base.convert("RGBA")
        else:
            # 无封面时用偏蓝的深空渐变，避免纯黑死背景
            base = self._vertical_gradient(W, H, (52, 76, 130), (30, 42, 88)).convert(
                "RGBA"
            )

        # 不再额外压暗背景，保持渐变本身的层次；卡片上的文字由玻璃底保证可读性
        return base

    def _cover_fill(self, img: Any, W: int, H: int) -> Any:
        sw, sh = img.size
        scale = max(W / sw, H / sh)
        nw, nh = int(sw * scale + 0.5), int(sh * scale + 0.5)
        resized = img.resize((nw, nh), Image.Resampling.LANCZOS)
        left = (nw - W) // 2
        top = (nh - H) // 2
        return resized.crop((left, top, left + W, top + H))

    @staticmethod
    def _vertical_gradient(
        W: int, H: int, top: tuple[int, int, int], bottom: tuple[int, int, int]
    ) -> Any:
        grad = Image.new("RGB", (1, H))
        for y in range(H):
            t = y / max(H - 1, 1)
            grad.putpixel(
                (0, y),
                (
                    int(top[0] + (bottom[0] - top[0]) * t),
                    int(top[1] + (bottom[1] - top[1]) * t),
                    int(top[2] + (bottom[2] - top[2]) * t),
                ),
            )
        return grad.resize((W, H))

    def _diagonal_shade(self, W: int, H: int) -> Any:
        """左下到右上的柔和明暗变化。"""
        small = 64
        mask = Image.new("L", (small, small), 0)
        for y in range(small):
            for x in range(small):
                t = (x + y) / (2 * (small - 1))
                mask.putpixel((x, y), int(70 * (1 - t)))
        mask = mask.resize((W, H), Image.Resampling.BICUBIC)
        shade = Image.new("RGBA", (W, H), (8, 12, 26, 255))
        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        layer.paste(shade, (0, 0), mask)
        return layer

    @staticmethod
    def _screen(base: Any, layer: Any) -> Any:
        """按 alpha 加权的滤色混合：base ◇ layer，让光斑在暗背景上发光。"""
        from PIL import ImageChops

        base_rgb = base.convert("RGB")
        layer_rgb = Image.new("RGB", base.size, (0, 0, 0))
        layer_rgb.paste(layer.convert("RGB"), (0, 0), layer.split()[3])
        screened = ImageChops.screen(base_rgb, layer_rgb)
        return screened.convert("RGBA")

    def _add_glows(
        self,
        canvas: Any,
        W: int,
        H: int,
        palette: list[tuple[int, int, int]],
        rng: random.Random,
    ) -> None:
        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        spots = [
            (0.14, 0.10, 0.40, palette[0], 255),
            (0.88, 0.14, 0.38, palette[1], 255),
            (0.78, 0.92, 0.42, palette[2], 235),
        ]
        for cx, cy, r, color, alpha in spots:
            jx = cx + rng.uniform(-0.03, 0.03)
            jy = cy + rng.uniform(-0.03, 0.03)
            radius = int(min(W, H) * r)
            d.ellipse(
                [
                    int(jx * W) - radius,
                    int(jy * H) - radius,
                    int(jx * W) + radius,
                    int(jy * H) + radius,
                ],
                fill=(*color, alpha),
            )
        layer = layer.filter(ImageFilter.GaussianBlur(int(min(W, H) * 0.09)))
        canvas.alpha_composite(layer)

    def _add_grid(self, canvas: Any, W: int, H: int) -> None:
        s = self.s
        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        # 线宽取 2*s，缩回后约 2px，比 1px 更平滑不易显锯齿
        step = 44 * s
        lw = max(2, 2 * s)
        for x in range(0, W + step, step):
            d.line([(x, 0), (x, H)], fill=(255, 255, 255, 12), width=lw)
        for y in range(0, H + step, step):
            d.line([(0, y), (W, y)], fill=(255, 255, 255, 10), width=lw)
        # 只让网格在四角隐约可见，避免干扰卡片
        mask = Image.new("L", (W, H), 0)
        md = ImageDraw.Draw(mask)
        for cx, cy in [(0, 0), (W, 0), (0, H), (W, H)]:
            r = int(min(W, H) * 0.62)
            md.ellipse([cx - r, cy - r, cx + r, cy + r], fill=110)
        mask = mask.filter(ImageFilter.GaussianBlur(60 * s))
        alpha = Image.composite(
            layer.split()[3].point(lambda a: min(a, 34)),
            Image.new("L", (W, H), 0),
            mask,
        )
        canvas.paste(layer, (0, 0), alpha)

    def _add_noise(self, canvas: Any, W: int, H: int, rng: random.Random) -> None:
        """细颗粒噪点，提升质感、掩盖色带。在最终分辨率上生成，避免被缩放放大成糊块。"""
        noise = Image.effect_noise((W, H), 10).convert("L")
        alpha = noise.point(lambda v: max(0, min(12, (v - 112) // 10)))
        grain = Image.new("RGBA", (W, H), (255, 255, 255, 0))
        grain.putalpha(alpha)
        canvas.alpha_composite(grain)

    # -- 卡片 -------------------------------------------------------------

    def _make_card(
        self, W: int, H: int, palette: list[tuple[int, int, int]]
    ) -> tuple[Any, int]:
        """返回 (卡片图层, 阴影偏移)，图层四周比卡片大一圈以容纳投影。"""
        s = self.s
        cw = W - CARD_MARGIN_X * 2 * s
        ch = H - CARD_MARGIN_Y * 2 * s
        radius = CARD_RADIUS * s

        card = Image.new("RGBA", (cw + 80 * s, ch + 80 * s), (0, 0, 0, 0))
        d = ImageDraw.Draw(card)
        off = 40 * s

        # 底部投影
        shadow = Image.new("RGBA", card.size, (0, 0, 0, 0))
        sd = ImageDraw.Draw(shadow)
        sd.rounded_rectangle(
            [off, off + 10 * s, off + cw, off + ch + 10 * s],
            radius=radius,
            fill=(2, 6, 16, 160),
        )
        shadow = shadow.filter(ImageFilter.GaussianBlur(24 * s))
        card.alpha_composite(shadow)

        # 玻璃主体：更淡的半透明底 + 顶部高亮（模糊感在合成阶段叠加）
        d.rounded_rectangle(
            [off, off, off + cw, off + ch],
            radius=radius,
            fill=(14, 20, 38, 128),
        )
        highlight = Image.new("RGBA", card.size, (0, 0, 0, 0))
        hd = ImageDraw.Draw(highlight)
        hd.rounded_rectangle(
            [off, off, off + cw, off + ch],
            radius=radius,
            fill=(255, 255, 255, 26),
        )
        fade = Image.new("L", (1, ch), 0)
        for y in range(ch):
            t = y / max(ch - 1, 1)
            fade.putpixel((0, y), int(255 * max(0.0, 1 - t * 2.4)))
        fade = fade.resize(card.size)
        highlight.putalpha(
            Image.composite(highlight.split()[3], Image.new("L", card.size, 0), fade)
        )
        card.alpha_composite(highlight)

        # 顶部边缘一道主色微光
        edge = Image.new("RGBA", card.size, (0, 0, 0, 0))
        ed = ImageDraw.Draw(edge)
        ed.rounded_rectangle(
            [off, off, off + cw, off + ch],
            radius=radius,
            outline=(*palette[0], 90),
            width=max(2, 2 * s),
        )
        ed.rounded_rectangle(
            [off + s, off + s, off + cw - s, off + ch - s],
            radius=radius - s,
            outline=(255, 255, 255, 46),
            width=max(2, 2 * s),
        )
        card.alpha_composite(edge)

        return card, off

    # -- 文案绘制 -----------------------------------------------------------

    def _draw_content(
        self,
        canvas: Any,
        data: dict[str, Any],
        *,
        x: int,
        y: int,
        right: int,
        bottom: int,
    ) -> None:
        s = self.s
        d = ImageDraw.Draw(canvas)
        content_w = right - x

        # 站点行：左上角标，右侧域名
        site_font = self.fonts.get(25 * s, bold=True)
        domain_font = self.fonts.get(20 * s)
        site = str(data.get("site_name", ""))
        domain = str(data.get("domain", ""))
        accent = (125, 211, 252, 255)
        bar_h = 30 * s
        d.rounded_rectangle(
            [x, y + 4 * s, x + 7 * s, y + 4 * s + bar_h],
            radius=4 * s,
            fill=accent,
        )
        d.text((x + 20 * s, y), site, font=site_font, fill=(241, 245, 249, 245))
        site_h = self._line_height(site_font)
        if domain:
            dw = self._text_width(d, domain, domain_font)
            d.text(
                (right - min(dw, 380 * s), y + 5 * s),
                domain
                if dw <= 380 * s
                else self._ellipsize(d, domain, domain_font, 380 * s),
                font=domain_font,
                fill=(203, 213, 225, 170),
            )
        cursor = y + site_h + 30 * s

        # 标题：最多两行，逐档缩小字号直到放得下
        title = str(data.get("title", "")) or "Untitled"
        title_font, title_lines = self._fit_lines(
            d, title, [(64, True), (56, True), (48, True)], content_w, 2, s
        )
        title_lh = int(self._line_height(title_font) * 1.12)
        for line in title_lines:
            d.text((x, cursor), line, font=title_font, fill=(255, 255, 255, 255))
            cursor += title_lh
        cursor += 22 * s

        # 元信息胶囊：作者 / 字数 / 发布时间
        pills = [
            str(data.get("author", "")),
            str(data.get("word_count_label", "")),
            str(data.get("published_label", "")),
        ]
        used = self._draw_pills(
            d,
            canvas,
            [p for p in pills if p],
            x,
            cursor,
            right,
            self.fonts.get(21 * s),
            fg=(226, 232, 240, 235),
        )
        cursor += used + 16 * s

        # 分类与标签
        chips = data.get("taxonomy_chips") or []
        chip_labels = [str(c.get("label", "")) for c in chips if isinstance(c, dict)]
        chip_labels = [c for c in chip_labels if c]
        if chip_labels:
            used = self._draw_pills(
                d,
                canvas,
                chip_labels,
                x,
                cursor,
                right,
                self.fonts.get(19 * s),
                fg=(224, 242, 254, 225),
                accent=True,
                max_rows=1,
            )
            cursor += used + 16 * s

        # 摘要：占满剩余空间，最多三行
        summary = str(data.get("summary", ""))
        if summary and cursor < bottom - 30 * s:
            avail_h = bottom - cursor
            summary_font, summary_lines = self._fit_lines(
                d, summary, [(30, False), (28, False), (26, False)], content_w, 3, s
            )
            lh = int(self._line_height(summary_font) * 1.5)
            max_rows = max(1, min(len(summary_lines), avail_h // max(lh, 1)))
            for line in summary_lines[:max_rows]:
                d.text((x, cursor), line, font=summary_font, fill=(214, 226, 240, 225))
                cursor += lh

    # -- 文本工具 -----------------------------------------------------------

    @staticmethod
    def _text_width(draw: Any, text: str, font: Any) -> int:
        if not text:
            return 0
        box = draw.textbbox((0, 0), text, font=font)
        return box[2] - box[0]

    @staticmethod
    def _line_height(font: Any) -> int:
        try:
            asc, desc = font.getmetrics()
            return asc + desc
        except Exception:
            box = font.getbbox("国Ag")
            return box[3] - box[1]

    def _fit_lines(
        self,
        draw: Any,
        text: str,
        candidates: list[tuple[int, bool]],
        max_width: int,
        max_lines: int,
        s: int,
    ) -> tuple[Any, list[str]]:
        """从候选字号中选最大且能放进 max_lines 行的字号。"""
        for size, bold in candidates:
            font = self.fonts.get(size * s, bold=bold)
            lines = self._wrap(draw, text, font, max_width)
            if len(lines) <= max_lines:
                return font, lines
        font = self.fonts.get(candidates[-1][0] * s, bold=candidates[-1][1])
        lines = self._wrap(draw, text, font, max_width)[:max_lines]
        if lines and len(self._wrap(draw, text, font, max_width)) > max_lines:
            lines[-1] = self._ellipsize(draw, lines[-1] + "…", font, max_width)
        return font, lines

    def _wrap(self, draw: Any, text: str, font: Any, max_width: int) -> list[str]:
        """中英文混排换行：按词切分，CJK 字符可独立断行。"""
        tokens = self._tokenize(text)
        lines: list[str] = []
        current = ""
        for token in tokens:
            trial = current + token
            if current and self._text_width(draw, trial, font) > max_width:
                lines.append(current.rstrip())
                current = token.lstrip()
            else:
                current = trial
        if current.strip():
            lines.append(current.rstrip())
        return lines or [""]

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        tokens: list[str] = []
        buf = ""
        for ch in text:
            if CJK_RE.match(ch):
                if buf:
                    tokens.append(buf)
                    buf = ""
                tokens.append(ch)
            elif ch.isspace():
                if buf:
                    tokens.append(buf)
                    buf = ""
                tokens.append(" ")
            else:
                buf += ch
        if buf:
            tokens.append(buf)
        return tokens

    def _ellipsize(self, draw: Any, text: str, font: Any, max_width: int) -> str:
        if self._text_width(draw, text, font) <= max_width:
            return text
        trimmed = text
        while trimmed and self._text_width(draw, trimmed + "…", font) > max_width:
            trimmed = trimmed[:-1]
        return trimmed + "…" if trimmed else ""

    def _draw_pills(
        self,
        draw: Any,
        canvas: Any,
        labels: list[str],
        x: int,
        y: int,
        right: int,
        font: Any,
        *,
        fg: tuple[int, int, int, int],
        accent: bool = False,
        max_rows: int = 1,
    ) -> int:
        """绘制一排胶囊标签，返回占用高度。"""
        s = self.s
        if not labels:
            return 0
        pad_x = 16 * s
        gap = 12 * s
        row_h = self._line_height(font) + 16 * s
        cx, cy = x, y
        rows = 1
        for label in labels:
            text = self._ellipsize(draw, label, font, 320 * s)
            w = self._text_width(draw, text, font) + pad_x * 2
            if cx + w > right:
                if rows >= max_rows:
                    break
                rows += 1
                cx = x
                cy += row_h + 10 * s
            fill = (56, 130, 246, 60) if accent else (148, 163, 184, 34)
            outline = (125, 211, 252, 90) if accent else (255, 255, 255, 40)
            draw.rounded_rectangle(
                [cx, cy, cx + w, cy + row_h],
                radius=row_h // 2,
                fill=fill,
                outline=outline,
                width=max(2, 2 * s),
            )
            draw.text(
                (cx + pad_x, cy + (row_h - self._line_height(font)) // 2 - s),
                text,
                font=font,
                fill=fg,
            )
            cx += w + gap
        return rows * row_h + (rows - 1) * 10 * s

    # -- 输出 ----------------------------------------------------------------

    def _save(self, img: Any, data: dict[str, Any]) -> str:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha1(
            f"{data.get('title', '')}|{time.time_ns()}".encode("utf-8", "ignore")
        ).hexdigest()[:12]
        if self.format == "png":
            path = self.output_dir / f"halo-rss-preview-{digest}.png"
            img.save(path, format="PNG", optimize=True)
        else:
            path = self.output_dir / f"halo-rss-preview-{digest}.jpg"
            # subsampling=0 关闭 4:2:0 色度抽样，文字/描边边缘不再被糊掉
            img.save(
                path,
                format="JPEG",
                quality=self.quality,
                subsampling=0,
                optimize=True,
                progressive=True,
            )
        return str(path)


# ---------------------------------------------------------------------------
# RSS 解析
# ---------------------------------------------------------------------------


def parse_feed_articles(
    xml_text: str, feed_url: str, default_author: str = ""
) -> list[ArticlePreview]:
    text = str(xml_text or "").strip()
    if not text:
        raise FeedParseError("RSS 响应为空。")
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise FeedParseError(f"RSS 响应不是合法 XML: {exc}") from exc

    root_name = _local_name(root.tag)
    if root_name == "rss":
        items = list(root.findall(".//item"))
    elif root_name == "feed":
        items = [c for c in root if _local_name(c.tag) == "entry"]
    else:
        raise FeedParseError("RSS 响应不是有效的 RSS 或 Atom。")

    articles: list[ArticlePreview] = []
    for item in items:
        article = _parse_item(item, feed_url, default_author)
        if article is not None:
            articles.append(article)
    return articles


def _parse_item(
    item: ElementTree.Element, feed_url: str, default_author: str
) -> ArticlePreview | None:
    title = _clean_text(_child_text(item, ("title",)), 180)
    link = _extract_link(item, feed_url)
    published_raw = _clean_text(
        _child_text(item, ("pubDate", "published", "updated")), 80
    )
    article_id = _clean_text(_child_text(item, ("guid", "id")), 300) or link
    if not article_id:
        article_id = f"{title}|{published_raw}"
    if not article_id.strip("|"):
        return None

    author = _clean_text(_child_text(item, ("creator", "author", "name")), 80)
    if not author:
        author = default_author or "Unknown"

    content_html = _child_text(item, ("encoded", "content"))
    description_html = _child_text(item, ("description", "summary"))
    source_html = content_html or description_html
    content_text = _clean_html(source_html)
    summary = _first_paragraph(source_html) or _truncate(content_text, 140)
    word_count = _estimate_word_count(content_text or _clean_html(description_html))

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


def _extract_cover_url(item: ElementTree.Element, html_text: str, base_url: str) -> str:
    for child in item:
        name = _local_name(child.tag)
        url = str(child.attrib.get("url") or "").strip()
        if not url or _is_tracking_pixel(url):
            continue
        media_hint = str(child.attrib.get("medium") or "").lower() == "image" or str(
            child.attrib.get("type") or ""
        ).lower().startswith("image/")
        if name in {"thumbnail", "content", "enclosure"} and media_hint:
            return urljoin(base_url, url)
    match = IMG_SRC_RE.search(str(html_text or ""))
    if match:
        url = unescape(match.group(1)).strip()
        if url and not _is_tracking_pixel(url):
            return urljoin(base_url, url)
    return ""


def _is_tracking_pixel(url: str) -> bool:
    """判断是否为 Halo feed 插件的统计像素等假封面。"""
    path = urlparse(str(url or "")).path.lower()
    return "telemetry" in path or "tracker" in path or "pixel" in path


def _local_name(tag: str) -> str:
    return str(tag or "").rsplit("}", 1)[-1]


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
    item: ElementTree.Element, names: tuple[str, ...], limit: int = 80
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
            return _truncate(text, 160)
    return ""


def _clean_html(value: Any) -> str:
    text = unescape(str(value or ""))
    text = SCRIPT_STYLE_RE.sub(" ", text)
    text = TAG_RE.sub(" ", text)
    return SPACE_RE.sub(" ", text).strip()


def _clean_text(value: Any, limit: int = 0) -> str:
    text = _clean_html(value)
    return _truncate(text, limit) if limit > 0 else text


def _truncate(value: str, limit: int) -> str:
    text = SPACE_RE.sub(" ", str(value or "")).strip()
    if limit <= 0 or len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _estimate_word_count(text: str) -> int:
    value = str(text or "")
    cjk = len(CJK_RE.findall(value))
    words = len(WORD_RE.findall(CJK_RE.sub(" ", value)))
    return cjk + words


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


def _dedupe_texts(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        key = text.lower()
        if not text or key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


# ---------------------------------------------------------------------------
# 文章页分类/标签/字数补全
# ---------------------------------------------------------------------------


class _TaxonomyHtmlParser(HTMLParser):
    """从 Halo 主题文章页提取分类、标签和官方字数。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.categories: list[str] = []
        self.tags: list[str] = []
        self.word_count = 0
        self._capture: dict[str, Any] | None = None
        self._in_word_count = False
        self._word_buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {str(k).lower(): str(v or "") for k, v in attrs}
        classes = set(attrs_dict.get("class", "").split())
        id_value = attrs_dict.get("id", "")
        if "word-count" in classes or id_value == "word-count":
            self._in_word_count = True
            self._word_buf = []
            return
        if tag.lower() != "a":
            return
        href = attrs_dict.get("href", "")
        kind = ""
        if (
            "post-meta-categories" in classes or "/categories/" in href
        ) and "/tags/" not in href:
            kind = "category"
        elif "post-meta__tags" in classes or "/tags/" in href:
            kind = "tag"
        if not kind:
            return
        if kind == "tag" and not attrs_dict.get("title", "").strip():
            return
        self._capture = {
            "kind": kind,
            "title": attrs_dict.get("title", ""),
            "text": [],
        }

    def handle_data(self, data: str) -> None:
        if self._in_word_count:
            self._word_buf.append(data)
        if self._capture is not None:
            self._capture["text"].append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._in_word_count:
            self._in_word_count = False
            match = re.search(r"(\d[\d,]*)", "".join(self._word_buf))
            if match:
                try:
                    self.word_count = int(match.group(1).replace(",", ""))
                except ValueError:
                    self.word_count = 0
        if tag.lower() != "a" or self._capture is None:
            return
        entry, self._capture = self._capture, None
        value = _clean_text(entry["title"] or "".join(entry["text"]), 80)
        if not value:
            return
        if entry["kind"] == "category":
            self.categories.append(value)
        else:
            self.tags.append(value)


def _parse_article_page(html_text: str) -> tuple[list[str], list[str], int]:
    parser = _TaxonomyHtmlParser()
    try:
        parser.feed(str(html_text or ""))
        parser.close()
    except Exception:
        return [], [], 0
    return (
        _dedupe_texts(parser.categories),
        _dedupe_texts(parser.tags),
        parser.word_count,
    )


# ---------------------------------------------------------------------------
# URL 工具
# ---------------------------------------------------------------------------


def _rewrite_to_feed_origin(url: str, feed_url: str) -> str:
    """把与文章同域的资源地址改写到 RSS 源站，绕过 CDN 人机校验。"""
    target = urlparse(str(url or "").strip())
    feed = urlparse(str(feed_url or "").strip())
    if not target.scheme or not target.netloc:
        return urljoin(feed_url, url)
    if not feed.scheme or not feed.netloc:
        return url
    return urlunparse(
        (
            feed.scheme,
            feed.netloc,
            target.path,
            target.params,
            target.query,
            target.fragment,
        )
    )


def _upgrade_halo_thumbnail(url: str, size: str) -> str:
    text = str(url or "").strip()
    if not text or "/apis/" not in text or "thumbnail" not in text:
        return text
    size = size if size in {"s", "m", "l", "xl"} else "l"
    return (
        re.sub(r"([?&]size=)[a-zA-Z]+", rf"\g<1>{size}", text)
        if "size=" in text
        else text
    )


def _parse_target_session(target: str) -> tuple[str, str, str]:
    try:
        platform_id, message_type, session_id = str(target or "").split(":", 2)
    except ValueError as exc:
        raise RuntimeError(f"invalid target session: {target}") from exc
    if not platform_id or not message_type or not session_id:
        raise RuntimeError(f"invalid target session: {target}")
    return platform_id, message_type, session_id


def _session_numeric_id(session_id: str) -> int:
    value = str(session_id or "").rsplit("_", 1)[-1].strip()
    if not value.isdigit():
        raise RuntimeError(f"session id is not numeric: {session_id}")
    return int(value)


# ---------------------------------------------------------------------------
# 插件主体
# ---------------------------------------------------------------------------


class HaloRssPosterPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        self.config = config or {}
        self._poll_task: asyncio.Task | None = None
        self._checking = False
        self._fonts = _FontBook(self._cfg_str("local_render_font_path", ""))

    # -- 命令 -----------------------------------------------------------

    @filter.command_group("halorss")
    def halorss(self):
        """Halo RSS 新文章预览推送。"""

    @halorss.command("bind")
    async def bind(self, event: AstrMessageEvent):
        """把当前 OneBot v11 会话绑定为推送目标。"""
        if not self._cfg_bool("enable", True):
            yield event.plain_result("Halo RSS 推送插件未启用。")
            return
        if not self._is_allowed(event):
            yield event.plain_result("此命令仅 AstrBot 管理员可用。")
            return
        if self._event_platform_name(event) not in SUPPORTED_PLATFORMS:
            yield event.plain_result(
                "请在 OneBot v11(aiocqhttp) 群聊或私聊中使用 /halorss bind。"
            )
            return
        target = str(getattr(event, "unified_msg_origin", "") or "")
        await self.put_kv_data("target_session", target)
        yield event.plain_result(f"已绑定当前会话为推送目标。\n- target: {target}")

    @halorss.command("reset")
    async def reset(self, event: AstrMessageEvent):
        """清空绑定与已见文章状态。"""
        if not self._is_allowed(event):
            yield event.plain_result("此命令仅 AstrBot 管理员可用。")
            return
        for key in ("target_session", "seen_ids", "stats"):
            await self.delete_kv_data(key)
        yield event.plain_result("已清空 Halo RSS 推送绑定和状态。")

    @halorss.command("status")
    async def status(self, event: AstrMessageEvent):
        """查看绑定、轮询和最近错误。"""
        if not self._is_allowed(event):
            yield event.plain_result("此命令仅 AstrBot 管理员可用。")
            return
        target = str(await self.get_kv_data("target_session", "") or "")
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
        if stats.get("last_check_at"):
            lines.append(f"- last_check_at: {stats['last_check_at']}")
        if stats.get("last_error"):
            lines.append(f"- last_error: {_truncate(str(stats['last_error']), 240)}")
        yield event.plain_result("\n".join(lines))

    @halorss.command("check")
    async def check(self, event: AstrMessageEvent):
        """立刻执行一次 RSS 检查。"""
        if not self._is_allowed(event):
            yield event.plain_result("此命令仅 AstrBot 管理员可用。")
            return
        outcome = await self.check_once()
        lines = [
            "Halo RSS 检查完成。",
            f"- found: {outcome.found_count}",
            f"- new: {outcome.new_count}",
            f"- pushed: {outcome.pushed_count}",
            f"- recorded: {outcome.recorded_count}",
        ]
        if outcome.error:
            lines.append(f"- error: {_truncate(outcome.error, 300)}")
        yield event.plain_result("\n".join(lines))

    @halorss.command("latest")
    async def latest(self, event: AstrMessageEvent):
        """立刻推送 RSS 中最新一篇文章的预览图（只发图片，不发额外文字）。"""
        if not self._is_allowed(event):
            yield event.plain_result("此命令仅 AstrBot 管理员可用。")
            return
        target = str(await self.get_kv_data("target_session", "") or "")
        if not target:
            yield event.plain_result(
                "尚未绑定推送目标，请先在目标会话发送 /halorss bind。"
            )
            return
        try:
            articles = await self._fetch_articles()
            if not articles:
                yield event.plain_result("RSS 中没有可推送的文章。")
                return
            article = articles[0]
            await self._send_article(target, article)
            await self._save_seen(
                self._merge_seen(await self._load_seen(), [article.article_id])
            )
        except Exception as exc:
            logger.warning(
                "[%s] latest push failed: %s", PLUGIN_NAME, exc, exc_info=True
            )
            yield event.plain_result(f"最新文章推送失败：{_truncate(str(exc), 240)}")
            return
        # 推送消息只包含图片，不再额外发送文字
        if False:
            yield event.plain_result("")

    # -- 生命周期 ---------------------------------------------------------

    async def initialize(self) -> None:
        if not self._cfg_bool("enable", True):
            return
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = asyncio.create_task(self._poll_loop())

    async def terminate(self) -> None:
        task, self._poll_task = self._poll_task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    # -- 轮询 -------------------------------------------------------------

    async def _poll_loop(self) -> None:
        interval = self._cfg_int("poll_interval_seconds", 300, 30, 86400)
        while True:
            try:
                if await self.get_kv_data("target_session", ""):
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

    async def check_once(self) -> CheckOutcome:
        if not self._cfg_bool("enable", True):
            outcome = CheckOutcome(error="plugin disabled")
            await self._save_stats(outcome)
            return outcome
        if self._checking:
            return CheckOutcome(error="check already running")

        self._checking = True
        try:
            target = str(await self.get_kv_data("target_session", "") or "")
            if not target:
                outcome = CheckOutcome(error="no target session bound")
                await self._save_stats(outcome)
                return outcome

            articles = await self._fetch_articles()
            seen = await self._load_seen()
            seen_set = set(seen)
            new_articles = [a for a in articles if a.article_id not in seen_set]
            outcome = CheckOutcome(
                found_count=len(articles), new_count=len(new_articles)
            )

            if not new_articles:
                await self._save_stats(outcome)
                return outcome

            if not seen and not self._cfg_bool("push_existing_on_first_run", False):
                ids = [a.article_id for a in articles]
                await self._save_seen(self._merge_seen(seen, ids))
                outcome.recorded_count = len(ids)
                await self._save_stats(outcome)
                return outcome

            limit = self._cfg_int("max_articles_per_check", 3, 1, 20)
            pushed: list[str] = []
            for article in reversed(new_articles[:limit]):
                await self._send_article(target, article)
                pushed.append(article.article_id)
            if pushed:
                await self._save_seen(self._merge_seen(seen, pushed))
            outcome.pushed_count = len(pushed)
            outcome.recorded_count = len(pushed)
            await self._save_stats(outcome)
            return outcome
        except Exception as exc:
            logger.warning("[%s] RSS check failed: %s", PLUGIN_NAME, exc, exc_info=True)
            outcome = CheckOutcome(error=f"{type(exc).__name__}: {exc}")
            await self._save_stats(outcome)
            return outcome
        finally:
            self._checking = False

    # -- 抓取 ---------------------------------------------------------------

    async def _fetch_text(
        self, url: str, timeout_key: str = "request_timeout_seconds"
    ) -> str:
        timeout = aiohttp.ClientTimeout(total=self._cfg_int(timeout_key, 10, 1, 120))
        headers = self._cfg_headers() or None
        async with aiohttp.ClientSession(trust_env=True, timeout=timeout) as session:
            async with session.get(url, headers=headers) as response:
                text = await response.text()
                if response.status != 200:
                    detail = _truncate(_clean_html(text), 240)
                    raise FeedParseError(f"HTTP {response.status}: {detail}")
                return text

    async def _fetch_articles(self) -> list[ArticlePreview]:
        feed_text = await self._fetch_text(self._rss_url())
        return parse_feed_articles(
            feed_text, self._rss_url(), default_author=self._site_name()
        )

    async def _fetch_cover(self, cover_url: str) -> bytes | None:
        cover_url = str(cover_url or "").strip()
        if not cover_url:
            return None
        retries = self._cfg_int("cover_download_retries", 2, 1, 5)
        max_bytes = self._cfg_int(
            "max_cover_bytes", 5 * 1024 * 1024, 64 * 1024, 12 * 1024 * 1024
        )
        last_error: Exception | None = None
        for attempt in range(retries):
            try:
                timeout = aiohttp.ClientTimeout(
                    total=self._cfg_int("cover_download_timeout_seconds", 8, 1, 60)
                )
                async with aiohttp.ClientSession(
                    trust_env=True, timeout=timeout
                ) as session:
                    async with session.get(
                        cover_url, headers=self._cfg_headers() or None
                    ) as response:
                        if response.status != 200:
                            raise FeedParseError(f"cover HTTP {response.status}")
                        data = await response.read()
                if len(data) > max_bytes:
                    raise FeedParseError(f"cover too large: {len(data)} bytes")
                if Image is not None:
                    try:
                        probe = Image.open(BytesIO(data))
                        probe.verify()
                    except Exception as exc:
                        raise FeedParseError("cover is not a supported image") from exc
                return data
            except Exception as exc:
                last_error = exc
                if attempt + 1 < retries:
                    await asyncio.sleep(min(0.3 * (attempt + 1), 1.0))
        if last_error and self._cfg_bool("debug_log", False):
            logger.warning(
                "[%s] cover fetch failed: %s", PLUGIN_NAME, last_error, exc_info=True
            )
        return None

    async def _enrich_article(self, article: ArticlePreview) -> ArticlePreview:
        """访问源站文章页，补全标签和 Halo 官方字数。"""
        if not self._cfg_bool("fetch_article_taxonomy", True) or not article.link:
            return article
        url = _rewrite_to_feed_origin(article.link, self._rss_url())
        try:
            html_text = await self._fetch_text(url)
        except Exception as exc:
            if self._cfg_bool("debug_log", False):
                logger.warning(
                    "[%s] article page fetch failed: %s",
                    PLUGIN_NAME,
                    exc,
                    exc_info=True,
                )
            return article
        categories, tags, word_count = _parse_article_page(html_text)
        if not categories and not tags and word_count <= 0:
            return article
        return replace(
            article,
            categories=categories or article.categories,
            tags=tags or article.tags,
            word_count=word_count or article.word_count,
            word_count_estimated=False
            if word_count > 0
            else article.word_count_estimated,
        )

    # -- 发送 ---------------------------------------------------------------

    async def _send_article(self, target_session: str, article: ArticlePreview) -> None:
        article = await self._enrich_article(article)
        data = self._build_render_data(article)
        cover_bytes = await self._fetch_cover(data["cover_url"])
        image_path = await asyncio.to_thread(self._render_sync, data, cover_bytes)

        mode = self._cfg_str("image_send_mode", "onebot_base64").lower()
        if mode not in {"onebot_base64", "onebot_file", "astrbot"}:
            mode = "onebot_base64"
        if mode == "astrbot":
            chain = MessageChain([Comp.Image.fromFileSystem(image_path)])
            sent = await self.context.send_message(target_session, chain)
            if sent is False:
                raise RuntimeError(f"target session not found: {target_session}")
            return
        await self._send_onebot_image(
            target_session, image_path, base64_mode=(mode == "onebot_base64")
        )

    def _render_sync(self, data: dict[str, Any], cover_bytes: bytes | None) -> str:
        output_dir_raw = self._cfg_str("local_render_output_dir", "")
        renderer = PreviewRenderer(
            self._fonts,
            scale=self._cfg_int("local_render_scale", 3, 1, 4),
            quality=self._cfg_int("image_quality", 95, 30, 100),
            format=self._cfg_str("image_format", "jpeg"),
            output_dir=Path(output_dir_raw) if output_dir_raw else None,
        )
        return renderer.render(data, cover_bytes)

    async def _send_onebot_image(
        self, target: str, image_path: str, *, base64_mode: bool
    ) -> None:
        platform_id, message_type, session_id = _parse_target_session(target)
        bot = self._find_onebot_bot(platform_id)
        if base64_mode:
            file_value = "base64://" + base64.b64encode(
                Path(image_path).read_bytes()
            ).decode("ascii")
        else:
            file_value = Path(image_path).resolve().as_uri()
        payload = [{"type": "image", "data": {"file": file_value, "cache": 0}}]
        if message_type == "GroupMessage":
            await bot.send_group_msg(
                group_id=_session_numeric_id(session_id), message=payload
            )
        elif message_type == "FriendMessage":
            await bot.send_private_msg(
                user_id=_session_numeric_id(session_id), message=payload
            )
        else:
            raise RuntimeError(f"unsupported OneBot message type: {message_type}")

    def _find_onebot_bot(self, platform_id: str) -> Any:
        manager = getattr(self.context, "platform_manager", None)
        for platform in getattr(manager, "platform_insts", []) or []:
            meta_func = getattr(platform, "meta", None)
            if not callable(meta_func):
                continue
            if getattr(meta_func(), "id", "") != platform_id:
                continue
            bot = getattr(platform, "bot", None)
            if bot is not None:
                return bot
        raise RuntimeError(f"cannot find aiocqhttp bot for platform: {platform_id}")

    # -- 渲染数据 -----------------------------------------------------------

    def _build_render_data(self, article: ArticlePreview) -> dict[str, Any]:
        cover = str(article.cover_url or "").strip()
        if cover and self._cfg_bool("rewrite_cover_to_rss_origin", True):
            cover = _rewrite_to_feed_origin(cover, self._rss_url())
        cover = _upgrade_halo_thumbnail(
            cover, self._cfg_str("cover_thumbnail_size", "xl")
        )

        chips: list[dict[str, str]] = []
        categories = _dedupe_texts(article.categories)
        category_keys = {c.lower() for c in categories}
        for category in categories[:1]:
            chips.append({"label": f"分类 · {category}"})
        shown = 0
        for tag in _dedupe_texts(article.tags):
            if tag.lower() in category_keys:
                continue
            chips.append({"label": f"#{tag}"})
            shown += 1
            if shown >= self._cfg_int("max_tag_chips", 4, 0, 12):
                break

        prefix = "约 " if article.word_count_estimated else ""
        return {
            "site_name": self._site_name(),
            "domain": self._article_domain(article),
            "title": article.title,
            "author": article.author,
            "summary": _truncate(
                article.summary, self._cfg_int("summary_chars", 140, 40, 300)
            ),
            "cover_url": cover,
            "taxonomy_chips": chips,
            "word_count_label": f"{prefix}{max(article.word_count, 0)} 字",
            "published_label": self._format_datetime(article),
        }

    def _format_datetime(self, article: ArticlePreview) -> str:
        if article.published_dt is None:
            return article.published_raw or "未知时间"
        dt = article.published_dt
        try:
            zone = ZoneInfo(
                self._cfg_str("timezone", "Asia/Shanghai") or "Asia/Shanghai"
            )
            dt = dt.replace(tzinfo=zone) if dt.tzinfo is None else dt.astimezone(zone)
        except Exception:
            pass
        return dt.strftime("%Y-%m-%d %H:%M")

    def _article_domain(self, article: ArticlePreview) -> str:
        for value in (article.link, self._rss_url()):
            parsed = urlparse(value)
            if parsed.netloc:
                return parsed.netloc
        return self._site_name()

    # -- 状态存储 -----------------------------------------------------------

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
        stats["last_check_at"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
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

    # -- 配置与权限 ---------------------------------------------------------

    def _rss_url(self) -> str:
        return self._cfg_str("rss_url", DEFAULT_RSS_URL) or DEFAULT_RSS_URL

    def _site_name(self) -> str:
        return self._cfg_str("site_name", DEFAULT_SITE_NAME) or DEFAULT_SITE_NAME

    def _is_allowed(self, event: AstrMessageEvent) -> bool:
        if not self._cfg_bool("admin_only", True):
            return True
        is_admin = getattr(event, "is_admin", None)
        if not callable(is_admin):
            return False
        try:
            return bool(is_admin())
        except Exception:
            return False

    def _event_platform_name(self, event: AstrMessageEvent) -> str:
        getter = getattr(event, "get_platform_name", None)
        if not callable(getter):
            return ""
        try:
            return str(getter() or "")
        except Exception:
            return ""

    def _cfg(self, key: str, default: Any) -> Any:
        if hasattr(self.config, "get"):
            return self.config.get(key, default)
        return default

    def _cfg_str(self, key: str, default: str) -> str:
        return str(self._cfg(key, default) or "").strip()

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

    def _cfg_headers(self) -> dict[str, str]:
        value = self._cfg("request_headers", {})
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return {}
            try:
                value = json.loads(text)
            except json.JSONDecodeError as exc:
                logger.warning(
                    "[%s] request_headers 不是合法 JSON: %s", PLUGIN_NAME, exc
                )
                return {}
        if not isinstance(value, dict):
            return {}
        return {
            str(k): str(v)
            for k, v in value.items()
            if str(k).strip() and str(v).strip()
        }
