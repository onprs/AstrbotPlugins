from __future__ import annotations

import asyncio
import html
import json
import re
import uuid
from typing import Any

import aiohttp

from astrbot.api import llm_tool, logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context, Star


PLUGIN_NAME = "astrbot_plugin_searxng_search"
SUPPORTED_PLATFORMS = {"qq_official", "qq_official_webhook"}
DEFAULT_BASE_URL = "http://127.0.0.1:8080"
ALLOWED_TIME_RANGES = {"day", "week", "month", "year"}
TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")


class SearxngSearchPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}

    @llm_tool(name="web_search_searxng")
    async def web_search_searxng(
        self,
        event: AstrMessageEvent,
        query: str,
        max_results: int = 0,
        language: str = "",
        time_range: str = "",
    ) -> str:
        """Search the web with the locally configured SearXNG instance.

        Args:
            query(string): Required. Search query.
            max_results(number): Optional. Maximum number of results to return. Default uses plugin config. Range is 1-20.
            language(string): Optional. SearXNG language code, for example zh-CN or en-US. Default uses plugin config.
            time_range(string): Optional. Time range filter. One of day, week, month, year. Default uses plugin config.
        """
        if not self._cfg_bool("enable", True):
            return "Error: SearXNG search plugin is disabled."

        query = str(query or "").strip()
        if not query:
            return "Error: query must be a non-empty string."

        if self._cfg_bool("restrict_to_qq_official", True):
            platform_name = self._event_platform_name(event)
            if platform_name not in SUPPORTED_PLATFORMS:
                return (
                    "Error: web_search_searxng is only enabled for QQ official "
                    f"platforms, current platform is {platform_name or 'unknown'}."
                )

        limit = self._resolve_max_results(max_results)
        snippet_chars = self._cfg_int("result_snippet_chars", 300, 40, 2000)
        params = self._build_search_params(
            query=query,
            language=language,
            time_range=time_range,
        )
        url = self._search_url()

        try:
            timeout = aiohttp.ClientTimeout(
                total=self._cfg_float("timeout_seconds", 10.0, 1.0, 120.0)
            )
            async with aiohttp.ClientSession(
                trust_env=True,
                timeout=timeout,
            ) as session:
                async with session.get(url, params=params) as response:
                    if response.status != 200:
                        return await self._http_error(response)
                    try:
                        data = await response.json()
                    except Exception as exc:
                        logger.warning(
                            "[%s] failed to parse SearXNG JSON response: %s",
                            PLUGIN_NAME,
                            exc,
                            exc_info=True,
                        )
                        return f"Error: failed to parse SearXNG JSON response: {exc}"
        except asyncio.TimeoutError:
            return "Error: SearXNG search request timed out."
        except Exception as exc:
            logger.warning(
                "[%s] SearXNG search request failed: %s",
                PLUGIN_NAME,
                exc,
                exc_info=True,
            )
            return f"Error: SearXNG search request failed: {type(exc).__name__}: {exc}"

        results = self._map_results(
            data=data,
            limit=limit,
            snippet_chars=snippet_chars,
        )
        return json.dumps(
            {"results": results, "source": "searxng", "query": query},
            ensure_ascii=False,
        )

    def _build_search_params(
        self,
        *,
        query: str,
        language: str,
        time_range: str,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "q": query,
            "format": "json",
            "pageno": 1,
        }

        resolved_language = str(language or self._cfg("language", "zh-CN") or "").strip()
        if resolved_language:
            params["language"] = resolved_language

        categories = self._cfg_list("categories", ["general"])
        if categories:
            params["categories"] = ",".join(categories)

        engines = self._cfg_list("engines", [])
        if engines:
            params["engines"] = ",".join(engines)

        params["safesearch"] = self._cfg_int("safesearch", 0, 0, 2)

        resolved_time_range = str(
            time_range or self._cfg("time_range", "") or ""
        ).strip()
        if resolved_time_range in ALLOWED_TIME_RANGES:
            params["time_range"] = resolved_time_range

        return params

    def _map_results(
        self,
        *,
        data: dict[str, Any],
        limit: int,
        snippet_chars: int,
    ) -> list[dict[str, str]]:
        rows = data.get("results", [])
        if not isinstance(rows, list):
            rows = []

        ref = f"sxng.{uuid.uuid4().hex[:4]}"
        mapped: list[dict[str, str]] = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            if not url:
                continue

            snippet = item.get("content") or item.get("snippet") or ""
            mapped.append(
                {
                    "index": f"{ref}.{len(mapped) + 1}",
                    "title": self._clean_text(item.get("title") or "Untitled", 300),
                    "url": url,
                    "snippet": self._clean_text(snippet, snippet_chars),
                    "engine": self._engine_name(item),
                    "category": str(item.get("category") or ""),
                    "published_date": str(
                        item.get("publishedDate")
                        or item.get("published_date")
                        or item.get("published")
                        or ""
                    ),
                }
            )
            if len(mapped) >= limit:
                break
        return mapped

    async def _http_error(self, response: Any) -> str:
        text = await response.text()
        detail = self._clean_text(text, 500)
        msg = f"Error: SearXNG search failed with HTTP {response.status}: {detail}"
        if response.status == 403:
            msg += (
                " Check SearXNG settings.yml and ensure search.formats includes json."
            )
        return msg

    def _search_url(self) -> str:
        base_url = str(self._cfg("base_url", DEFAULT_BASE_URL) or DEFAULT_BASE_URL)
        base_url = base_url.strip().rstrip("/")
        if base_url.endswith("/search"):
            return base_url
        return f"{base_url}/search"

    def _resolve_max_results(self, max_results: int) -> int:
        try:
            requested = int(max_results)
        except Exception:
            requested = 0
        if requested <= 0:
            requested = self._cfg_int("max_results", 5, 1, 20)
        return max(1, min(requested, 20))

    def _event_platform_name(self, event: AstrMessageEvent) -> str:
        get_platform_name = getattr(event, "get_platform_name", None)
        if callable(get_platform_name):
            try:
                return str(get_platform_name() or "")
            except Exception:
                return ""
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

    def _cfg_list(self, key: str, default: list[str]) -> list[str]:
        value = self._cfg(key, default)
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, (tuple, set)):
            return [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return list(default)

    @staticmethod
    def _engine_name(item: dict[str, Any]) -> str:
        engine = item.get("engine")
        if engine:
            return str(engine)
        engines = item.get("engines")
        if isinstance(engines, list):
            return ",".join(str(value) for value in engines if str(value).strip())
        return ""

    @staticmethod
    def _clean_text(value: Any, limit: int) -> str:
        text = html.unescape(str(value or ""))
        text = TAG_RE.sub("", text)
        text = SPACE_RE.sub(" ", text).strip()
        if limit > 0 and len(text) > limit:
            return text[: max(0, limit - 3)].rstrip() + "..."
        return text
