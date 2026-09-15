"""为 AstrBot 网页搜索增加当前日期与证据约束。"""

from __future__ import annotations

import inspect
import json
import re
from datetime import datetime, timedelta
from time import monotonic
from typing import Any
from urllib.parse import urlsplit

import aiohttp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star
from astrbot.core.agent.message import TextPart
from astrbot.core.agent.tool import FunctionTool
from mcp.types import CallToolResult, TextContent

PLUGIN_NAME = "astrbot_plugin_web_search_guard"
SEARCH_TOOL_NAMES = frozenset(
    {
        "web_search_baidu",
        "web_search_tavily",
        "web_search_bocha",
        "web_search_brave",
        "web_search_firecrawl",
        "web_search_exa",
        "web_search_searxng",
    }
)
_POLICY_EXTRA = "_web_search_guard_policy"
_SOURCE_URLS_EXTRA = "_web_search_guard_source_urls"
_GITHUB_RELEASE_CACHE_EXTRA = "_web_search_guard_github_release_cache"
_OFFICIAL_RELEASES_EXTRA = "_web_search_guard_official_releases"
_GITHUB_PROCESS_CACHE_TTL_SECONDS = 600
_GITHUB_RELEASE_PROCESS_CACHE: dict[
    str,
    tuple[float, dict[str, str] | None],
] = {}

_YEAR_RE = re.compile(r"(?<!\d)(20\d{2})(?!\d)")
_CHINESE_YEAR_MONTH_RE = re.compile(r"(?<!\d)20\d{2}年(?:\d{1,2}月)?")
_URL_RE = re.compile(r"https?://[^\s\"'<>]+")
_GITHUB_REPO_PART_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_RELEASE_VERSION_RE = re.compile(
    r"版本|正式版|稳定版|发行版|发布版|"
    r"\b(?:version|release|stable\s+release|latest\s+tag)\b",
    re.IGNORECASE,
)
_SEMVER_RE = re.compile(r"(?<![\w.])v?\d+\.\d+(?:\.\d+)+(?![\w.])", re.IGNORECASE)
_AS_OF_CURRENT_RE = re.compile(
    r"截至(?:今天|今日|目前|现在)|截止(?:到)?(?:今天|今日|目前|现在)|"
    r"(?:按|以)(?:今天|今日|当前)(?:的)?(?:日期|时间)(?:为准|判断|核实)?|"
    r"as of (?:today|now|the current date)|up to (?:today|now)",
    re.IGNORECASE,
)
_TODAY_RE = re.compile(r"今天|今日|当天|刚刚|today|right now", re.IGNORECASE)
_RECENT_RE = re.compile(
    r"最近|近期|近来|这几天|刚发布|刚上线|recent|newly released",
    re.IGNORECASE,
)
_CURRENT_RE = re.compile(
    r"最新|当前|现在|目前|实际情况|是否属实|是真的吗|真假|核实|"
    r"搜一下|查一下|is that true|latest|current|newest|up[- ]to[- ]date|verify",
    re.IGNORECASE,
)

SEARCH_RULE = (
    "\n\n# Web Search Evidence Rules\n"
    "When using a web-search tool, treat the current date supplied in the runtime "
    "instruction as authoritative. For latest, current, recent, today, verification, "
    "or actual-status questions, build the query with the correct current year and "
    "use the tool's date filters. Never copy a model knowledge-cutoff year into a "
    "current search. If the user explicitly requests a historical year, preserve it. "
    "Do not attach an ambiguous version name, pronoun, or generic term to a nearby "
    "older brand merely because that brand appears in group history. Do not ask the "
    "user to clarify: resolve the subject from the most recent relevant visible "
    "messages or group-history tool; if it remains ambiguous, search the literal "
    "version/product terms without inventing a brand and state the scope. After each "
    "search, compare result publication/update dates with the current date, prefer "
    "official primary sources, and do not call old results current. If evidence is "
    "insufficient, search again with narrower terms instead of claiming confirmation. "
    "Every final answer that relies on web search must include one or more source URLs "
    "returned by the tool. For latest software release questions, an official GitHub "
    "Releases API verification included in a tool result overrides search ranking and "
    "older indexed release pages. Never invent a URL or a publication date.\n"
)
_TOOL_DESCRIPTION_RULE = (
    " For current, latest, recent, today, verification, or actual-status queries, "
    "use the authoritative runtime date, include the correct year, apply supported "
    "date filters, preserve user-requested historical years, avoid inventing an "
    "entity for ambiguous terms, verify result dates before answering, and treat an "
    "official GitHub Releases API check as authoritative over search ranking."
)


def recency_kind(text: str) -> str:
    """返回 today、recent、current 或空字符串。"""
    value = str(text or "")
    # “截至今天”描述的是当前状态，不代表内容必须在今天发布。
    if _AS_OF_CURRENT_RE.search(value):
        return "current"
    if _TODAY_RE.search(value):
        return "today"
    if _RECENT_RE.search(value):
        return "recent"
    if _CURRENT_RE.search(value):
        return "current"
    return ""


def extract_years(text: str) -> set[int]:
    """提取文本中的四位公历年份。"""
    return {int(value) for value in _YEAR_RE.findall(str(text or ""))}


def _replace_unrequested_years(
    query: str,
    *,
    now: datetime,
    explicit_years: set[int],
) -> str:
    current_year = now.year
    historical_scope = bool(explicit_years and current_year not in explicit_years)
    target_year = max(explicit_years) if historical_scope else current_year

    def replace_chinese(match: re.Match[str]) -> str:
        original = match.group(0)
        year = int(original[:4])
        if year == target_year or year in explicit_years:
            return original
        if "月" in original:
            month_suffix = (
                original.split("年", 1)[1] if historical_scope else f"{now.month}月"
            )
            return f"{target_year}年{month_suffix}"
        return f"{target_year}年"

    replaced = _CHINESE_YEAR_MONTH_RE.sub(replace_chinese, query)

    def replace_plain(match: re.Match[str]) -> str:
        year = int(match.group(1))
        if year == target_year or year in explicit_years:
            return match.group(0)
        return str(target_year)

    return _YEAR_RE.sub(replace_plain, replaced)


def _date_scope(
    *,
    now: datetime,
    kind: str,
    explicit_years: set[int],
    recent_days: int,
) -> tuple[datetime, datetime, int]:
    historical_years = sorted(year for year in explicit_years if year != now.year)
    if historical_years and now.year not in explicit_years:
        year = historical_years[-1]
        return (
            now.replace(year=year, month=1, day=1, hour=0, minute=0, second=0),
            now.replace(year=year + 1, month=1, day=1, hour=0, minute=0, second=0),
            year,
        )

    end = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0)
    if kind == "today":
        start = now.replace(hour=0, minute=0, second=0)
    elif kind == "recent":
        start = (now - timedelta(days=recent_days)).replace(
            hour=0,
            minute=0,
            second=0,
        )
    else:
        start = now.replace(month=1, day=1, hour=0, minute=0, second=0)
    return start, end, now.year


def constrain_search_args(
    tool_name: str,
    tool_args: dict[str, Any],
    *,
    now: datetime,
    request_kind: str = "",
    explicit_years: set[int] | None = None,
    recent_days: int = 90,
) -> tuple[dict[str, Any], bool]:
    """纠正当前性搜索的年份，并按工具能力补充日期范围。"""
    args = dict(tool_args or {})
    query = str(args.get("query", "") or "").strip()
    if not query:
        return args, False

    years = set(explicit_years or set())
    kind = request_kind or recency_kind(query)
    if not kind:
        return args, False

    historical_scope = bool(years and now.year not in years)
    target_year = max(years) if historical_scope else now.year
    normalized_query = _replace_unrequested_years(
        query,
        now=now,
        explicit_years=years,
    )
    if target_year not in extract_years(normalized_query):
        normalized_query = f"{normalized_query} {target_year}"
    args["query"] = normalized_query

    start, end, _ = _date_scope(
        now=now,
        kind=kind,
        explicit_years=years,
        recent_days=max(1, recent_days),
    )
    if tool_name == "web_search_exa":
        args["start_published_date"] = f"{start:%Y-%m-%d}T00:00:00.000Z"
        args["end_published_date"] = f"{end:%Y-%m-%d}T00:00:00.000Z"
    elif tool_name == "web_search_tavily":
        args["start_date"] = f"{start:%Y-%m-%d}"
        args["end_date"] = f"{end:%Y-%m-%d}"
    elif tool_name == "web_search_brave":
        args["freshness"] = (
            "day" if kind == "today" else "month" if kind == "recent" else "year"
        )
    elif tool_name == "web_search_bocha":
        args["freshness"] = (
            "oneDay"
            if kind == "today"
            else "oneMonth"
            if kind == "recent"
            else "oneYear"
        )
    elif tool_name == "web_search_baidu":
        args["search_recency_filter"] = (
            "week" if kind == "today" else "month" if kind == "recent" else "year"
        )
    elif tool_name == "web_search_searxng":
        args["time_range"] = (
            "day" if kind == "today" else "month" if kind == "recent" else "year"
        )

    return args, args != tool_args


def extract_source_urls(result: Any) -> list[str]:
    """从网页搜索工具结果中提取并去重 URL。"""
    urls: list[str] = []

    def append(value: str) -> None:
        url = value.rstrip(".,);]}")
        if url and url not in urls:
            urls.append(url)

    def walk(value: Any, key: str = "") -> None:
        if isinstance(value, dict):
            for child_key, child in value.items():
                if child_key.lower() == "url" and isinstance(child, str):
                    append(child)
                else:
                    walk(child, child_key)
        elif isinstance(value, list):
            for child in value:
                walk(child, key)
        elif isinstance(value, str):
            if key.lower() == "url":
                append(value)
            else:
                for match in _URL_RE.findall(value):
                    append(match)

    if isinstance(result, str):
        try:
            walk(json.loads(result))
        except (json.JSONDecodeError, TypeError):
            walk(result)
    elif isinstance(result, CallToolResult):
        for content in result.content:
            if isinstance(content, TextContent):
                walk(content.text)
    else:
        walk(result)
    return urls


def is_current_release_request(
    text: str,
    *,
    now: datetime,
    explicit_years: set[int],
) -> bool:
    """判断是否需要核验当前软件正式版。"""
    historical_scope = bool(explicit_years and now.year not in explicit_years)
    return bool(
        not historical_scope
        and recency_kind(text)
        and _RELEASE_VERSION_RE.search(str(text or ""))
    )


def extract_github_release_repositories(urls: list[str]) -> list[str]:
    """从受信任的 GitHub Release URL 形态提取 owner/repository。"""
    repositories: list[str] = []
    seen: set[str] = set()
    for url in urls:
        try:
            parsed = urlsplit(url)
        except ValueError:
            continue
        if parsed.scheme not in {"http", "https"}:
            continue

        hostname = (parsed.hostname or "").lower()
        parts = [part for part in parsed.path.split("/") if part]
        if hostname in {"github.com", "www.github.com"}:
            if len(parts) < 3 or parts[2].lower() != "releases":
                continue
            owner, repository = parts[0], parts[1]
        elif hostname in {"newreleases.io", "www.newreleases.io"}:
            if (
                len(parts) < 6
                or parts[0].lower() != "project"
                or parts[1].lower() != "github"
                or parts[4].lower() != "release"
            ):
                continue
            owner, repository = parts[2], parts[3]
        else:
            continue

        repository = repository.removesuffix(".git")
        if not _GITHUB_REPO_PART_RE.fullmatch(owner) or not _GITHUB_REPO_PART_RE.fullmatch(
            repository
        ):
            continue
        value = f"{owner}/{repository}"
        key = value.lower()
        if key not in seen:
            seen.add(key)
            repositories.append(value)
    return repositories


def _valid_github_repository(repository: str) -> tuple[str, str] | None:
    parts = str(repository or "").split("/")
    if len(parts) != 2:
        return None
    owner, name = parts
    if not _GITHUB_REPO_PART_RE.fullmatch(owner) or not _GITHUB_REPO_PART_RE.fullmatch(
        name
    ):
        return None
    return owner, name


def select_github_release_repository(
    query: str,
    repositories: list[str],
) -> str | None:
    """只选择仓库名确实出现在查询中的 Release 仓库。"""
    query_text = str(query or "").casefold()
    query_compact = re.sub(r"[^a-z0-9]+", "", query_text)
    ranked: list[tuple[int, int, str]] = []
    for index, repository in enumerate(repositories):
        validated = _valid_github_repository(repository)
        if validated is None:
            continue
        owner, name = validated
        name_folded = name.casefold()
        name_compact = re.sub(r"[^a-z0-9]+", "", name_folded)
        if len(name_compact) < 3 or name_compact not in query_compact:
            continue
        full_compact = re.sub(r"[^a-z0-9]+", "", f"{owner}/{name}".casefold())
        score = 2 if full_compact in query_compact else 1
        ranked.append((score, -index, repository))
    if not ranked:
        return None
    return max(ranked)[2]


async def fetch_github_latest_release(repository: str) -> dict[str, str] | None:
    """从 GitHub 官方 API 获取仓库最新的非预发布 Release。"""
    validated = _valid_github_repository(repository)
    if validated is None:
        return None
    owner, name = validated
    endpoint = f"https://api.github.com/repos/{owner}/{name}/releases/latest"
    timeout = aiohttp.ClientTimeout(total=10, connect=5)
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"{PLUGIN_NAME}/1",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        async with (
            aiohttp.ClientSession(
                trust_env=True,
                timeout=timeout,
                headers=headers,
            ) as session,
            session.get(endpoint) as response,
        ):
            if response.status != 200:
                logger.warning(
                    "%s GitHub latest release verification returned HTTP %s for %s",
                    PLUGIN_NAME,
                    response.status,
                    repository,
                )
                return None
            payload = await response.json(content_type=None)
    except (TimeoutError, aiohttp.ClientError, ValueError) as exc:
        logger.warning(
            "%s GitHub latest release verification failed for %s: %s",
            PLUGIN_NAME,
            repository,
            type(exc).__name__,
        )
        return None

    if not isinstance(payload, dict) or payload.get("draft") or payload.get("prerelease"):
        return None
    tag_name = payload.get("tag_name")
    published_at = payload.get("published_at")
    html_url = payload.get("html_url")
    if not all(isinstance(value, str) and value for value in (tag_name, published_at, html_url)):
        return None

    try:
        parsed_url = urlsplit(html_url)
    except ValueError:
        return None
    expected_path = f"/{owner}/{name}/releases/tag/".lower()
    if (
        parsed_url.scheme != "https"
        or (parsed_url.hostname or "").lower() != "github.com"
        or not parsed_url.path.lower().startswith(expected_path)
    ):
        return None

    return {
        "repository": f"{owner}/{name}",
        "tag_name": tag_name,
        "published_at": published_at,
        "html_url": html_url,
        "api_url": endpoint,
    }


async def get_official_github_release(
    event: Any,
    repository: str,
) -> dict[str, str] | None:
    """在单次事件内缓存 GitHub Release 核验结果。"""
    cache: dict[str, dict[str, str] | None] = {}
    if event is not None:
        existing = event.get_extra(_GITHUB_RELEASE_CACHE_EXTRA, {})
        if isinstance(existing, dict):
            cache = dict(existing)
    key = repository.lower()
    if key in cache:
        return cache[key]

    cached_process = _GITHUB_RELEASE_PROCESS_CACHE.get(key)
    if cached_process is not None and monotonic() < cached_process[0]:
        release = cached_process[1]
    else:
        try:
            release = await fetch_github_latest_release(repository)
        except Exception as exc:  # noqa: BLE001 - 搜索核验失败不能中断主回复
            logger.warning(
                "%s GitHub latest release verification raised for %s: %s",
                PLUGIN_NAME,
                repository,
                type(exc).__name__,
            )
            release = None
        if release is not None:
            _GITHUB_RELEASE_PROCESS_CACHE[key] = (
                monotonic() + _GITHUB_PROCESS_CACHE_TTL_SECONDS,
                release,
            )
    if release is not None:
        cache[key] = release
    if event is None:
        return release

    if release is not None:
        event.set_extra(_GITHUB_RELEASE_CACHE_EXTRA, cache)
        existing_releases = event.get_extra(_OFFICIAL_RELEASES_EXTRA, [])
        releases = list(existing_releases) if isinstance(existing_releases, list) else []
        releases = [
            item
            for item in releases
            if not (
                isinstance(item, dict)
                and str(item.get("repository", "")).lower() == key
            )
        ]
        releases.insert(0, release)
        event.set_extra(_OFFICIAL_RELEASES_EXTRA, releases[:2])
    return release


def record_source_urls(
    event: Any,
    urls: list[str],
    *,
    max_source_urls: int,
    prepend: bool = False,
) -> None:
    """把实际获取到的来源 URL 记录到当前事件。"""
    if event is None or not urls:
        return
    existing = event.get_extra(_SOURCE_URLS_EXTRA, [])
    current = list(existing) if isinstance(existing, list) else []
    candidates = [*urls, *current] if prepend else [*current, *urls]
    merged: list[str] = []
    for url in candidates:
        if isinstance(url, str) and url and url not in merged:
            merged.append(url)
    event.set_extra(_SOURCE_URLS_EXTRA, merged[:max_source_urls])


def build_official_release_note(
    release: dict[str, str],
    *,
    now: datetime,
) -> str:
    """生成进入工具结果的 GitHub 官方 Release 证据。"""
    return (
        "<official_release_verification>"
        f"Verified at {now:%Y-%m-%d} using the official GitHub Releases API: "
        f"{release['api_url']}. Repository: {release['repository']}. "
        "The latest non-draft, non-prerelease release returned by that endpoint is "
        f"{release['tag_name']!r}, published_at={release['published_at']!r}, "
        f"html_url={release['html_url']!r}. This official API evidence overrides "
        "older release pages or lower versions ranked by the search index. When "
        "answering the current latest formal/stable release, use this exact tag, "
        "publication timestamp, and URL."
        "</official_release_verification>"
    )


def build_official_release_summary(
    release: dict[str, str],
    *,
    now: datetime,
) -> str:
    """生成最终回复中的确定性官方核验结论。"""
    return (
        f"GitHub 官方 Releases 核验：截至 {now:%Y-%m-%d}，"
        f"{release['repository']} 最新正式版为 {release['tag_name']}，"
        f"GitHub published_at 为 {release['published_at']}。\n"
        f"来源：{release['html_url']}"
    )


def apply_official_release_correction(
    text: str,
    release: dict[str, str],
    *,
    now: datetime,
) -> str:
    """当模型遗漏或误报版本时，以官方 API 证据替换版本结论。"""
    summary = build_official_release_summary(release, now=now)
    tag_name = release["tag_name"]
    html_url = release["html_url"]
    official_version = tag_name.casefold().removeprefix("v")
    mentioned_versions = {
        match.group(0).casefold().removeprefix("v")
        for match in _SEMVER_RE.finditer(text)
    }
    contains_conflicting_version = any(
        version != official_version for version in mentioned_versions
    )
    if tag_name.lower() in text.lower() and not contains_conflicting_version:
        if html_url in text:
            return text
        return f"{text.rstrip()}\n\n来源：{html_url}"

    lines = text.splitlines()
    trigger_index: int | None = None
    for index, line in enumerate(lines):
        if (
            _RELEASE_VERSION_RE.search(line)
            or _SEMVER_RE.search(line)
            or "/releases/" in line.lower()
        ):
            trigger_index = index
            break
    if trigger_index is None:
        return f"{text.rstrip()}\n\n{summary}" if text.strip() else summary

    preserved = "\n".join(lines[:trigger_index]).rstrip()
    return f"{preserved}\n\n{summary}" if preserved else summary


def _result_visible_text(result: Any) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, CallToolResult):
        return "\n".join(
            content.text
            for content in result.content
            if isinstance(content, TextContent)
        )
    return str(result or "")


def build_result_guard_note(
    *,
    now: datetime,
    query: str,
    kind: str,
    result: Any,
) -> str:
    """生成直接进入 Agent 工具结果的日期核验提示。"""
    result_years = sorted(extract_years(_result_visible_text(result)))
    stale_only = bool(kind and result_years and max(result_years) < now.year)
    warning = (
        " Detected result years are older than the authoritative current year; "
        "these results do not establish current status. Search again."
        if stale_only
        else " Verify each result's publication or update date before using it."
    )
    return (
        "<web_search_guard>"
        f"Authoritative current date: {now:%Y-%m-%d}. "
        f"Executed query: {query!r}."
        f"{warning} Prefer official primary sources and include returned source URLs "
        "in the final answer. If the evidence is insufficient or ambiguous, run a "
        "narrower literal-term search without inventing a product entity; do not ask "
        "the user to clarify and do not claim confirmation."
        "</web_search_guard>"
    )


class GuardedSearchTool(FunctionTool):
    """单次请求内代理 AstrBot 原搜索工具，不修改全局工具注册表。"""

    def __init__(
        self,
        original: FunctionTool,
        *,
        now: datetime,
        request_kind: str,
        explicit_years: set[int],
        recent_days: int,
        max_source_urls: int,
    ) -> None:
        super().__init__(
            name=original.name,
            description=original.description + _TOOL_DESCRIPTION_RULE,
            parameters=original.parameters,
            handler=None,
            handler_module_path=original.handler_module_path,
            active=original.active,
            is_background_task=original.is_background_task,
        )
        object.__setattr__(self, "_original", original)
        object.__setattr__(self, "_now", now)
        object.__setattr__(self, "_request_kind", request_kind)
        object.__setattr__(self, "_explicit_years", explicit_years)
        object.__setattr__(self, "_recent_days", recent_days)
        object.__setattr__(self, "_max_source_urls", max_source_urls)

    async def call(self, context, **kwargs):
        normalized, changed = constrain_search_args(
            self.name,
            kwargs,
            now=self._now,
            request_kind=self._request_kind,
            explicit_years=self._explicit_years,
            recent_days=self._recent_days,
        )
        if changed:
            logger.info(
                "%s constrained %s args: %s",
                PLUGIN_NAME,
                self.name,
                normalized,
            )

        result = await self._call_original(context, normalized)
        event = getattr(getattr(context, "context", None), "event", None)
        query = str(normalized.get("query", ""))
        urls = extract_source_urls(result)
        official_release: dict[str, str] | None = None
        if is_current_release_request(
            query,
            now=self._now,
            explicit_years=self._explicit_years,
        ):
            repositories = extract_github_release_repositories(urls)
            repository = select_github_release_repository(query, repositories)
            if repository is not None:
                official_release = await get_official_github_release(event, repository)

        if official_release is not None:
            record_source_urls(
                event,
                [official_release["html_url"]],
                max_source_urls=self._max_source_urls,
                prepend=True,
            )
        record_source_urls(
            event,
            urls,
            max_source_urls=self._max_source_urls,
        )

        note = build_result_guard_note(
            now=self._now,
            query=query,
            kind=self._request_kind or recency_kind(query),
            result=result,
        )
        release_note = (
            build_official_release_note(official_release, now=self._now)
            if official_release is not None
            else ""
        )
        additions = "\n\n".join(part for part in (release_note, note) if part)
        if isinstance(result, str):
            return f"{result}\n\n{additions}"
        if isinstance(result, CallToolResult):
            if release_note:
                result.content.append(TextContent(type="text", text=release_note))
            result.content.append(TextContent(type="text", text=note))
        return result

    async def _call_original(self, context, kwargs: dict[str, Any]) -> Any:
        original = self._original
        if original.handler is not None:
            event = getattr(getattr(context, "context", None), "event", None)
            value = original.handler(event, **kwargs)
        elif type(original).call is not FunctionTool.call:
            value = original.call(context, **kwargs)
        elif hasattr(original, "run"):
            event = getattr(getattr(context, "context", None), "event", None)
            value = original.run(event, **kwargs)
        else:
            raise RuntimeError(f"搜索工具 {original.name} 没有可调用实现")

        if inspect.isawaitable(value):
            return await value
        return value


class WebSearchGuard(Star):
    """约束网页搜索的日期、实体归属、证据核验和来源输出。"""

    def __init__(self, context: Context, config: dict[str, Any] | None = None):
        super().__init__(context, config)
        self.config = config or {}
        self.enabled = bool(self.config.get("enable", True))
        self.recent_days = max(1, int(self.config.get("recent_days", 90)))
        self.max_source_urls = max(
            1,
            min(5, int(self.config.get("max_source_urls", 2))),
        )

    @filter.on_llm_request(priority=50)
    async def guard_search_request(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> None:
        if not self.enabled or req.func_tool is None:
            return
        if not any(name in SEARCH_TOOL_NAMES for name in req.func_tool.names()):
            return

        now = datetime.now().astimezone()
        request_text = f"{event.message_str or ''}\n{req.prompt or ''}"
        policy = {
            "now": now,
            "request_kind": recency_kind(request_text),
            "explicit_years": extract_years(request_text),
        }
        event.set_extra(_POLICY_EXTRA, policy)

        if SEARCH_RULE not in (req.system_prompt or ""):
            req.system_prompt = (req.system_prompt or "").rstrip() + SEARCH_RULE
        req.extra_user_content_parts.append(
            TextPart(
                text=(
                    "<web_search_runtime>"
                    f"Authoritative current date: {now:%Y-%m-%d}. "
                    "Use this date for every current-information search and result-date "
                    "comparison. Do not use a claimed model knowledge-cutoff date."
                    "</web_search_runtime>"
                )
            ).mark_as_temp()
        )

        for index, tool in enumerate(list(req.func_tool.tools)):
            if tool.name not in SEARCH_TOOL_NAMES or isinstance(
                tool,
                GuardedSearchTool,
            ):
                continue
            req.func_tool.tools[index] = GuardedSearchTool(
                tool,
                now=now,
                request_kind=policy["request_kind"],
                explicit_years=policy["explicit_years"],
                recent_days=self.recent_days,
                max_source_urls=self.max_source_urls,
            )

    @filter.on_using_llm_tool(priority=50)
    async def constrain_late_search_call(
        self,
        event: AstrMessageEvent,
        tool: FunctionTool,
        tool_args: dict[str, Any] | None,
    ) -> None:
        """覆盖其他插件在请求钩子之后加入的搜索工具。"""
        if not self.enabled or tool.name not in SEARCH_TOOL_NAMES or tool_args is None:
            return
        policy = event.get_extra(_POLICY_EXTRA, {})
        now = policy.get("now") if isinstance(policy, dict) else None
        if not isinstance(now, datetime):
            now = datetime.now().astimezone()
        normalized, changed = constrain_search_args(
            tool.name,
            tool_args,
            now=now,
            request_kind=(policy.get("request_kind", "") if policy else ""),
            explicit_years=(policy.get("explicit_years", set()) if policy else set()),
            recent_days=self.recent_days,
        )
        if changed:
            tool_args.clear()
            tool_args.update(normalized)

    @filter.on_llm_response(priority=50)
    async def ensure_source_urls(
        self,
        event: AstrMessageEvent,
        response: LLMResponse,
    ) -> None:
        """纠正官方 Release 结论，并兜底附加真实搜索 URL。"""
        if not self.enabled or response.role != "assistant":
            return
        text = response.completion_text
        if not isinstance(text, str):
            return

        releases = event.get_extra(_OFFICIAL_RELEASES_EXTRA, [])
        if isinstance(releases, list):
            release = next((item for item in releases if isinstance(item, dict)), None)
            if release is not None:
                policy = event.get_extra(_POLICY_EXTRA, {})
                now = policy.get("now") if isinstance(policy, dict) else None
                if not isinstance(now, datetime):
                    now = datetime.now().astimezone()
                text = apply_official_release_correction(text, release, now=now)
                response.completion_text = text

        urls = event.get_extra(_SOURCE_URLS_EXTRA, [])
        if not isinstance(urls, list) or not urls:
            return
        selected = [url for url in urls if isinstance(url, str)][: self.max_source_urls]
        if not selected or any(url in text for url in selected):
            return
        response.completion_text = f"{text.rstrip()}\n\n来源：" + "\n".join(selected)
