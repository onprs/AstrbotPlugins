"""本地渲染成果检验脚本（不属于插件运行时代码）。

用法：
    python render_sample.py                 # 尝试拉取默认 RSS 最新文章渲染
    python render_sample.py --offline       # 使用内置样例数据 + 程序生成封面
    python render_sample.py --out <path>    # 指定输出图片路径
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from main import (  # noqa: E402
    DEFAULT_RSS_URL,
    DEFAULT_SITE_NAME,
    PreviewRenderer,
    _FontBook,
    _rewrite_to_feed_origin,
    _truncate,
    _upgrade_halo_thumbnail,
    parse_feed_articles,
)

SAMPLE = {
    "site_name": DEFAULT_SITE_NAME,
    "domain": "blog.onprs.top",
    "title": "用 Pillow 在本地渲染出一张足够好看的文章预览图",
    "author": "onprs",
    "summary": _truncate(
        "彻底告别远端 HTML 渲染服务的 503 与超时：插件现在完全在本地使用 Pillow "
        "绘制预览图，封面取色光斑、细颗粒噪点、玻璃拟态卡片与超采样抗锯齿一个不少，"
        "排版也会根据实际文字宽度自动换行。",
        140,
    ),
    "cover_url": "",
    "taxonomy_chips": [
        {"label": "分类 · 开发手记"},
        {"label": "#AstrBot"},
        {"label": "#Pillow"},
    ],
    "word_count_label": "约 1280 字",
    "published_label": "2026-07-23 20:45",
}


def make_sample_cover() -> bytes:
    """生成一张 1600x900 的演示封面（日落渐变 + 山峦剪影）。"""
    from io import BytesIO

    from PIL import Image, ImageDraw, ImageFilter

    W, H = 1600, 900
    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)
    top, bottom = (38, 50, 96), (235, 126, 90)
    for y in range(H):
        t = y / (H - 1)
        d.line(
            [(0, y), (W, y)],
            fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)),
        )
    # 太阳
    d.ellipse([W * 0.62, H * 0.30, W * 0.78, H * 0.48], fill=(255, 214, 170))
    sun = img.crop((int(W * 0.55), int(H * 0.22), int(W * 0.85), int(H * 0.56)))
    sun = sun.filter(ImageFilter.GaussianBlur(18))
    img.paste(sun, (int(W * 0.55), int(H * 0.22)))
    # 山峦
    d = ImageDraw.Draw(img)
    d.polygon(
        [
            (0, H * 0.72),
            (W * 0.28, H * 0.42),
            (W * 0.52, H * 0.68),
            (W * 0.7, H * 0.5),
            (W, H * 0.74),
            (W, H),
            (0, H),
        ],
        fill=(24, 30, 58),
    )
    d.polygon(
        [
            (0, H * 0.86),
            (W * 0.36, H * 0.58),
            (W * 0.66, H * 0.84),
            (W, H * 0.66),
            (W, H),
            (0, H),
        ],
        fill=(15, 19, 40),
    )
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


async def fetch_latest() -> tuple[dict, bytes | None]:
    import aiohttp

    async with aiohttp.ClientSession(trust_env=True) as session:
        async with session.get(
            DEFAULT_RSS_URL, timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            text = await resp.text()
            if resp.status != 200:
                raise RuntimeError(f"RSS HTTP {resp.status}")
        articles = parse_feed_articles(
            text, DEFAULT_RSS_URL, default_author=DEFAULT_SITE_NAME
        )
        if not articles:
            raise RuntimeError("RSS 中没有文章")
        article = articles[0]
        cover = _upgrade_halo_thumbnail(
            _rewrite_to_feed_origin(article.cover_url, DEFAULT_RSS_URL), "l"
        )
        cover_bytes = None
        if cover:
            try:
                async with session.get(
                    cover, timeout=aiohttp.ClientTimeout(total=8)
                ) as resp:
                    if resp.status == 200:
                        cover_bytes = await resp.read()
            except Exception:
                cover_bytes = None
    chips = [{"label": f"分类 · {c}"} for c in article.categories[:1]]
    chips += [{"label": f"#{t}"} for t in article.tags[:4]]
    data = {
        "site_name": DEFAULT_SITE_NAME,
        "domain": "blog.onprs.top",
        "title": article.title,
        "author": article.author,
        "summary": _truncate(article.summary, 140),
        "cover_url": cover,
        "taxonomy_chips": chips,
        "word_count_label": f"约 {article.word_count} 字",
        "published_label": article.published_dt.strftime("%Y-%m-%d %H:%M")
        if article.published_dt
        else article.published_raw,
    }
    return data, cover_bytes


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--offline", action="store_true", help="不访问网络，使用内置样例"
    )
    parser.add_argument("--out", default="", help="输出图片路径")
    args = parser.parse_args()

    desktop = Path.home() / "Desktop"
    out_path = Path(args.out) if args.out else desktop / "halo-rss-preview-sample.jpg"

    cover_bytes: bytes | None = None
    if args.offline:
        data = SAMPLE
        cover_bytes = make_sample_cover()
    else:
        try:
            data, cover_bytes = asyncio.run(fetch_latest())
            print(f"已从 RSS 读取最新文章：{data['title']}")
        except Exception as exc:
            print(f"RSS 拉取失败（{exc}），改用内置样例数据。")
            data = SAMPLE
            cover_bytes = make_sample_cover()
        if cover_bytes is None:
            cover_bytes = make_sample_cover()

    renderer = PreviewRenderer(
        _FontBook(""), scale=3, quality=95, output_dir=out_path.parent
    )
    path = renderer.render(data, cover_bytes)
    if Path(path) != out_path:
        Path(path).replace(out_path)
    print(f"渲染完成：{out_path}")


if __name__ == "__main__":
    main()
