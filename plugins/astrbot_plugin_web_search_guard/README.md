# 联网搜索约束

`astrbot_plugin_web_search_guard` 通过 AstrBot 官方插件钩子约束内置网页搜索，不修改 AstrBot 本体。

## 功能

- 当前日期：每轮请求使用服务器当前日期，不允许模型把知识截止年份当成当前年份。
- 查询纠偏：对“最新、当前、近期、今天、核实、实际情况”等查询，纠正模型自行添加的错误年份。
- 时间过滤：区分“今天发生/发布”的单日范围与“截至今天/按今天日期判断”的当前状态范围，再按照 Exa、Tavily、Brave、Bocha、百度和 SearXNG 的参数能力增加日期范围。
- 历史查询：用户明确指定历史年份时保留原年份和历史时间范围。
- 实体约束：上下文主语不明确时不允许把版本号、代词或通用词强行归到较早出现的品牌；直接使用可见的字面关键词继续搜索，不询问用户。
- 官方版本核验：当前软件版本查询若返回 GitHub Release 链接，或 `newreleases.io` 明确映射到 GitHub 仓库的 Release 链接，自动调用同仓库公开的 `releases/latest` API 核验最新非预发布版本；官方 API 证据会覆盖搜索索引中的旧版本排序。
- 结果核验：搜索结果会携带当前日期和过期资料警告，要求模型核对发布日期并优先官方来源。
- 来源兜底：模型未引用时，从实际工具结果中附加真实 URL；模型仍把旧 GitHub Release 称为最新时，最终回复会按官方 API 证据纠正。

## 支持工具

- AstrBot 内置：Exa、Tavily、Brave、Bocha、Firecrawl、百度搜索。
- 插件工具：`web_search_searxng`。

## 配置

| 配置项 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `enable` | bool | `true` | 总开关 |
| `recent_days` | int | `90` | “最近、近期”等查询的时间窗口 |
| `max_source_urls` | int | `2` | 回复缺少引用时附加的来源链接数，范围 1-5 |

## 边界

插件只约束网页搜索过程，不自行调用 LLM、不替换搜索供应商、不读取或存储搜索 API Key。仓库识别仅接受 GitHub 官方 Release URL 和 `newreleases.io/project/github/...` 的固定路径，且仓库名必须与查询匹配；最新正式版核验只访问固定域名 `api.github.com` 的公开接口，无需 GitHub Token，并在进程内短时缓存成功结果。
