# Halo RSS 新文章预览推送

这个 AstrBot 插件用于 OneBot v11(`aiocqhttp`) 平台。它会轮询 Halo 博客 RSS，发现新文章后渲染一张高级玻璃质感的文章预览图，并主动推送到你绑定的 QQ 群或私聊。推送消息默认只包含图片，不额外发送文字。

## 使用方式

1. 将 `astrbot_plugin_halo_rss_poster` 放入 AstrBot 的 `data/plugins/` 目录。
2. 在 AstrBot WebUI 中启用插件，并确认配置里的 `rss_url`。
3. 在目标 QQ 群或私聊中发送 `/halorss bind`。
4. 可发送 `/halorss check` 手动检查一次。
5. 可发送 `/halorss latest` 手动推送 RSS 中最新一篇文章的预览图。
6. 可发送 `/halorss status` 查看绑定和最近错误。
7. 可发送 `/halorss reset` 清空绑定和已见文章状态。

## RSS 地址说明

默认 RSS 地址是源站直连地址：

```text
http://any.onprs.top:8090/rss.xml
```

公开访问地址 `https://blog.onprs.top/rss.xml` 经过 EdgeOne 加速，并启用了人机认证校验。非浏览器环境访问时可能拿到的是校验脚本而不是 RSS XML，不建议作为机器人轮询地址。

如果后续源站地址变化，请把 `rss_url` 改成 AstrBot 所在机器能直接访问的 RSS 地址。若源站需要指定 Host，可在 `request_headers` 文本框中填写 JSON 对象：

```json
{
  "Host": "blog.onprs.top",
  "User-Agent": "AstrBot-Halo-RSS/1.0"
}
```

当前源站 RSS 中可以读到封面图字段，但封面图地址默认仍是 `https://blog.onprs.top/apis/...`。由于该域名经过 EdgeOne 人机校验，插件默认开启 `rewrite_cover_to_rss_origin=true`，渲染预览图时会把这类封面地址改成 `rss_url` 的源站 origin，例如 `http://any.onprs.top:8090/apis/...`，从而拿到真实图片。

插件默认 `render_mode=local_pillow`，会在 AstrBot 容器内使用 Pillow 本地渲染 1600x900 JPEG，不再依赖 AstrBot 远端 HTML 渲染服务，因此可以避开远端渲染 503、超时和外链背景加载失败。`remote_html` 仍保留为兼容模式。

封面图会由插件自己下载到内存再合成进背景，默认 `cover_download_retries=2`，源站偶发 503/超时时会再试一次。若封面下载失败或超过 `max_cover_bytes`，本地渲染会生成一张带渐变和纹理的兜底背景，不会改发文字。

当前 RSS 中可以直接读到文章分类。标签通常不在 RSS 里，插件默认开启 `fetch_article_taxonomy=true`，推送新文章前会访问源站文章页，从主题页面里读取文章分类、标签、正文内容和 Halo 页面里的 `word-count`，并在预览图中展示更准确的字数。若你的源站文章页不可访问，插件会自动退回 RSS 中已有的信息，不影响文章推送。

## 图片发送说明

插件会把预览图渲染为 JPEG，并且只发送图片消息。默认 `image_send_mode=onebot_base64`，插件会直接调用 aiocqhttp bot 的 OneBot 原生 `send_group_msg` / `send_private_msg`，发送 OneBot 图片段 `base64://...`。这适合 AstrBot Docker 部署：图片文件只需要 AstrBot 容器可读，不要求 OneBot 协议端共享同一个文件目录。

如果你的 AstrBot 容器和 OneBot 协议端共享了同一个目录，也可以把 `image_send_mode` 改成 `onebot_file`，并把 `local_render_output_dir` 指向共享目录。`onebot_url` 主要用于 `render_mode=remote_html` 时发送远端渲染器返回的 URL。`astrbot` 模式会重新走 AstrBot 的图片发送适配器，一般不推荐。

如果图片发送失败，插件不会改发文字提醒，也不会把该文章标记为已推送，下一次检查会继续重试。

预览图默认输出为 1600x900，卡片居中显示，`image_quality=88`。本地渲染默认 `local_render_scale=2`，会先以 2 倍尺寸绘制文字、圆角、边框和阴影，再缩回 1600x900，减轻 Pillow 直绘带来的锯齿感。RSS 中 Halo 缩略图默认会从 `size=m` 提升到 `size=l`，让背景封面更清晰。若仍觉得糊，可以把 `cover_thumbnail_size` 调成 `xl`。

长标题会按实际文本宽度换行，作者/字数/时间、分类标签和摘要会按测量后的高度依次排布，避免标题和下面的标签重叠。

## Docker 字体说明

本地 Pillow 渲染需要容器里有能显示中文的字体。插件会优先尝试：

```text
/AstrBot/data/font.ttf
/AstrBot/data/fonts/NotoSansCJK-Regular.ttc
/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc
/usr/share/fonts/truetype/wqy/wqy-microhei.ttc
```

推荐做法是把一个中文字体挂载到 AstrBot 容器内的 `/AstrBot/data/font.ttf`。例如可以使用思源黑体/Noto Sans CJK、微软雅黑、文泉驿微米黑等。若你挂载到别的位置，请在 `local_render_font_path` 填写容器内绝对路径。

## 首次运行

默认 `push_existing_on_first_run=false`，首次检查只记录 RSS 中已有文章，不会把历史文章全部推送出来。之后出现的新文章才会推送。

## 预览图内容

预览图包含文章封面、标题、作者、字数、发布日期时间、分类、标签、第一段摘要、站点名和域名。插件会优先读取 Halo 文章页里的官方 `word-count`；读取不到时再按正文文本估算，并显示为“约 N 字”。
