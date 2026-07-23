# SearXNG 网页搜索

这个插件为 AstrBot Agent 注册一个 LLM 工具 `web_search_searxng`，让机器人在需要实时信息时通过本机 SearXNG 搜索网页。

## 安装

1. 将 `astrbot_plugin_searxng_search` 目录放入 AstrBot 的 `data/plugins/` 目录。
2. 在 AstrBot WebUI 的插件管理中重载或启用插件。
3. 在插件配置里确认 `base_url` 指向你的 SearXNG 服务。

默认地址是：

```text
http://127.0.0.1:8080
```

如果你的 Docker 映射端口或路径不同，请在 WebUI 中修改。例如：

```text
http://127.0.0.1:8888
http://192.168.1.10:8080/searxng
http://127.0.0.1:8080/search
```

## SearXNG 要求

插件调用 SearXNG 的 `/search` 接口，并带上：

```text
format=json
```

如果调用时返回 HTTP 403，请检查 SearXNG 的 `settings.yml`，确保 `search.formats` 包含 `json`。示例：

```yaml
search:
  formats:
    - html
    - json
```

修改 SearXNG 配置后通常需要重启对应 Docker 容器。

## 使用方式

正常和 QQ 官方机器人聊天即可。当 AstrBot Agent 判断问题需要搜索实时网页信息时，会自动调用：

```text
web_search_searxng
```

工具会返回 JSON，包含标题、URL、摘要、搜索引擎、分类和引用索引。最终回复仍由 AstrBot Agent 总结并通过当前 QQ 官方消息回复链路发送，本插件不会直接调用 QQ 主动推送 API。

## 配置说明

- `enable`: 是否启用插件。
- `base_url`: SearXNG 地址，可以是服务根地址，也可以直接是 `/search` 地址。
- `timeout_seconds`: 单次搜索请求超时。
- `max_results`: 默认返回结果数，插件限制到 1-20。
- `language`: 默认搜索语言，例如 `zh-CN` 或 `en-US`。
- `categories`: SearXNG 分类，留空表示使用 SearXNG 默认分类。
- `engines`: 指定搜索引擎，留空表示使用 SearXNG 默认引擎。
- `safesearch`: SearXNG 安全搜索级别。
- `time_range`: 默认时间范围，可填 `day`、`week`、`month`、`year`。
- `result_snippet_chars`: 每条结果摘要的最大字符数。
- `restrict_to_qq_official`: 默认只允许 `qq_official` 和 `qq_official_webhook` 平台使用。
- `debug_log`: 输出调试日志。

## 空结果

当 SearXNG 正常返回但没有结果时，工具返回：

```json
{"results": [], "source": "searxng", "query": "原始查询"}
```

这会让 Agent 知道搜索已完成但没有命中结果。
