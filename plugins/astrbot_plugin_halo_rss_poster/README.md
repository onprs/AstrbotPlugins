# Halo RSS 新文章预览推送

这个 AstrBot 插件用于 OneBot v11(`aiocqhttp`) 平台。它轮询 Halo 博客 RSS，发现新文章后**在本地使用 Pillow 渲染**一张玻璃拟态文章预览图，并主动推送到绑定的 QQ 群或私聊。推送消息只包含图片。

2.0 版本起插件完全移除了 AstrBot 远端 HTML 渲染路径，不再依赖远端渲染服务，因此不会遇到远端渲染 503、超时或外链背景加载失败的问题。

## 预览图设计

- 1600x900 JPEG，默认 2 倍超采样绘制后缩回，文字与圆角更平滑；
- 文章封面铺满背景，自动从封面取色生成三色光斑（滤色混合），配合景深暗角与细颗粒噪点；
- 玻璃拟态卡片：背景区域真实高斯模糊 + 半透明深色底 + 边缘高光 + 柔和投影；
- 标题按实际文字宽度自动换行（最多两行，逐档缩小字号），作者/字数/时间以胶囊展示，分类标签一行，摘要最多三行；
- 封面下载失败时自动生成渐变兜底背景，不会改发文字。

## 使用方式

1. 将 `astrbot_plugin_halo_rss_poster` 放入 AstrBot 的 `data/plugins/` 目录。
2. 在 AstrBot WebUI 中启用插件（依赖见 `requirements.txt`：aiohttp、Pillow），确认配置里的 `rss_url`。
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

公开访问地址 `https://blog.onprs.top/rss.xml` 经过 EdgeOne 加速并启用了人机认证校验，非浏览器环境可能拿到校验脚本而不是 RSS XML，不建议作为机器人轮询地址。

若源站需要指定 Host，可在 `request_headers` 文本框中填写 JSON 对象：

```json
{
  "Host": "blog.onprs.top",
  "User-Agent": "AstrBot-Halo-RSS/2.0"
}
```

RSS 中的封面图地址默认是 `https://blog.onprs.top/apis/...`，同样会被人机校验拦截。插件默认开启 `rewrite_cover_to_rss_origin=true`，下载封面时会改写到 `rss_url` 的源站 origin，并用 Pillow 校验下载到的确实是图片；失败或超过 `max_cover_bytes` 时使用兜底背景。

## 图片发送说明

插件只发送图片消息。默认 `image_send_mode=onebot_base64`，直接调用 aiocqhttp bot 的 OneBot 原生 `send_group_msg` / `send_private_msg` 发送 `base64://...` 图片段，适合 AstrBot Docker 部署，不要求协议端共享文件目录。

如果 AstrBot 容器和 OneBot 协议端共享目录，可把 `image_send_mode` 改成 `onebot_file` 并把 `local_render_output_dir` 指向共享目录。`astrbot` 模式走 AstrBot 的图片发送适配器，一般不推荐。

图片发送失败时插件不会改发文字，也不会把该文章标记为已推送，下次检查会继续重试。

## Docker 字体说明

本地 Pillow 渲染需要容器里有中文字体，插件会按顺序尝试：

```text
/AstrBot/data/font.ttf            （粗体可用 /AstrBot/data/font-bold.ttf）
/AstrBot/data/fonts/NotoSansCJK-Regular.ttc
/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc
/usr/share/fonts/truetype/wqy/wqy-microhei.ttc
```

推荐把一个中文字体（思源黑体/Noto Sans CJK、微软雅黑、文泉驿微米黑等）挂载到 `/AstrBot/data/font.ttf`；挂到其他位置时在 `local_render_font_path` 填写容器内绝对路径。

## 首次运行

默认 `push_existing_on_first_run=false`，首次检查只记录 RSS 中已有文章，不会把历史文章全部推送。之后出现的新文章才会推送。

## 文章页信息补全

标签通常不在 RSS 里。插件默认开启 `fetch_article_taxonomy=true`，推送前会访问源站文章页，读取主题页面里的分类、标签和 Halo 官方 `word-count`。文章页不可访问时自动退回 RSS 中已有的信息，不影响推送。

## 本地渲染自检

插件目录附带 `render_sample.py`，不依赖 AstrBot 运行环境：

```bash
python render_sample.py            # 拉取默认 RSS 最新文章渲染到桌面
python render_sample.py --offline  # 使用内置样例数据
python render_sample.py --out D:\sample.jpg
```
