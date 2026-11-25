"""
Open, zero-CAPTCHA multi-provider web search + SSRF-safe web page text fetcher.

Provider priority (auto-selected):
  1. SPLITCH_SEARXNG_URL (self-hosted SearXNG JSON API)
  2. BRAVE_API_KEY (official Brave Web Search API)
  3. TAVILY_API_KEY (official Tavily AI Search API)
  4. Built-in open zero-key aggregator (DuckDuckGo Instant Answer + Wikipedia API + HN Algolia)
"""

from __future__ import annotations

import asyncio
import html
import ipaddress
import os
import re
import socket
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import quote, urlparse

import httpx

from backend.router.config import default_data_root
from backend.shared.secrets import SecretVault, resolve_secret_value
from backend.version import __version__ as VERSION

USER_AGENT = f"Splitch/{VERSION} (+https://github.com/RaveAgainstTheMachine/splitch)"
TAG_STRIP_RE = re.compile(r"<[^>]+>")
WS_COLLAPSE_RE = re.compile(r"\s+")

WEB_TOOLS_SCHEMA: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the web for current facts, documentation, libraries, or news. "
                "Returns titles, URLs, and snippets."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query string"},
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum results to return (default 6, max 10)",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "Fetch a public HTTP/HTTPS web page and extract readable plain text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Public http:// or https:// URL"}
                },
                "required": ["url"],
            },
        },
    },
]


class _ReadableTextExtractor(HTMLParser):
    _SKIP_TAGS: ClassVar[frozenset[str]] = frozenset(
        {"script", "style", "noscript", "svg", "iframe", "head"}
    )

    def __init__(self) -> None:
        super().__init__()
        self._skip_depth = 0
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in self._SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag.lower() in ("p", "div", "li", "h1", "h2", "h3", "h4", "br", "tr"):
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            cleaned = data.strip()
            if cleaned:
                self._chunks.append(cleaned + " ")

    def text(self) -> str:
        raw = "".join(self._chunks)
        lines = [WS_COLLAPSE_RE.sub(" ", line).strip() for line in raw.splitlines()]
        return "\n".join(line for line in lines if line)


def _clean_snippet(raw: str) -> str:
    return WS_COLLAPSE_RE.sub(" ", html.unescape(TAG_STRIP_RE.sub("", raw or ""))).strip()


def _is_public_url(url: str) -> tuple[bool, str]:
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False, "Only public http:// or https:// URLs are allowed"
    host = parsed.hostname.lower()
    if host == "localhost" or host.endswith((".local", ".internal")):
        return False, f"Blocked non-public host: {host}"
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except OSError as exc:
        return False, f"DNS resolution failed: {exc}"
    for info in infos:
        ip_str = info[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return False, f"Blocked private/local IP address: {ip_str}"
    return True, ""


def _format_results(provider: str, query: str, items: list[dict[str, str]]) -> str:
    if not items:
        return f"No web results found for '{query}' (provider: {provider})."
    lines = [f"Web search results for '{query}' (via {provider}):", ""]
    for idx, it in enumerate(items, 1):
        title = it.get("title") or it.get("url") or "Result"
        url = it.get("url") or ""
        snippet = it.get("snippet") or ""
        lines.append(f"{idx}. **{title}**")
        if url:
            lines.append(f"   URL: {url}")
        if snippet:
            lines.append(f"   {snippet[:320]}")
    return "\n".join(lines)


async def _search_searxng(
    client: httpx.AsyncClient, base_url: str, query: str, limit: int
) -> list[dict[str, str]]:
    r = await client.get(
        f"{base_url.rstrip('/')}/search",
        params={"q": query, "format": "json", "language": "en"},
    )
    r.raise_for_status()
    out: list[dict[str, str]] = []
    for item in (r.json().get("results") or [])[:limit]:
        if isinstance(item, dict) and item.get("url"):
            out.append(
                {
                    "title": str(item.get("title") or item["url"]),
                    "url": str(item["url"]),
                    "snippet": _clean_snippet(str(item.get("content") or "")),
                }
            )
    return out


async def _search_brave(
    client: httpx.AsyncClient, api_key: str, query: str, limit: int
) -> list[dict[str, str]]:
    r = await client.get(
        "https://api.search.brave.com/res/v1/web/search",
        params={"q": query, "count": limit},
        headers={"X-Subscription-Token": api_key, "Accept": "application/json"},
    )
    r.raise_for_status()
    web = (r.json().get("web") or {}).get("results") or []
    return [
        {
            "title": str(it.get("title") or it.get("url") or ""),
            "url": str(it.get("url") or ""),
            "snippet": _clean_snippet(str(it.get("description") or "")),
        }
        for it in web[:limit]
        if isinstance(it, dict) and it.get("url")
    ]


async def _search_tavily(
    client: httpx.AsyncClient, api_key: str, query: str, limit: int
) -> list[dict[str, str]]:
    r = await client.post(
        "https://api.tavily.com/search",
        json={"api_key": api_key, "query": query, "max_results": limit},
    )
    r.raise_for_status()
    return [
        {
            "title": str(it.get("title") or it.get("url") or ""),
            "url": str(it.get("url") or ""),
            "snippet": _clean_snippet(str(it.get("content") or "")),
        }
        for it in (r.json().get("results") or [])[:limit]
        if isinstance(it, dict) and it.get("url")
    ]


async def _search_open_sources(
    client: httpx.AsyncClient, query: str, limit: int
) -> list[dict[str, str]]:
    """Aggregate official, zero-key, non-CAPTCHA JSON endpoints concurrently."""

    async def _ddg_ia() -> list[dict[str, str]]:
        try:
            r = await client.get(
                "https://api.duckduckgo.com/",
                params={"q": query, "format": "json", "no_html": 1, "skip_disambig": 1},
            )
            if r.status_code != 200:
                return []
            d = r.json()
            res: list[dict[str, str]] = []
            if d.get("AbstractText") and d.get("AbstractURL"):
                res.append(
                    {
                        "title": str(d.get("Heading") or query),
                        "url": str(d["AbstractURL"]),
                        "snippet": _clean_snippet(str(d["AbstractText"])),
                    }
                )
            for rel in (d.get("RelatedTopics") or [])[:3]:
                if isinstance(rel, dict) and rel.get("FirstURL") and rel.get("Text"):
                    res.append(
                        {
                            "title": str(rel["Text"]).split(" - ", 1)[0][:100],
                            "url": str(rel["FirstURL"]),
                            "snippet": _clean_snippet(str(rel["Text"])),
                        }
                    )
            return res
        except (httpx.HTTPError, ValueError):
            return []

    async def _wiki() -> list[dict[str, str]]:
        try:
            r = await client.get(
                "https://en.wikipedia.org/w/api.php",
                params={
                    "action": "query",
                    "list": "search",
                    "srsearch": query,
                    "srlimit": 4,
                    "format": "json",
                },
            )
            if r.status_code != 200:
                return []
            hits = (r.json().get("query") or {}).get("search") or []
            out: list[dict[str, str]] = []
            for h in hits:
                title = str(h.get("title") or "")
                if not title:
                    continue
                out.append(
                    {
                        "title": f"{title} — Wikipedia",
                        "url": f"https://en.wikipedia.org/wiki/{quote(title.replace(' ', '_'))}",
                        "snippet": _clean_snippet(str(h.get("snippet") or "")),
                    }
                )
            return out
        except (httpx.HTTPError, ValueError):
            return []

    async def _hn() -> list[dict[str, str]]:
        try:
            r = await client.get(
                "https://hn.algolia.com/api/v1/search",
                params={"query": query, "tags": "story", "hitsPerPage": 4},
            )
            if r.status_code != 200:
                return []
            out: list[dict[str, str]] = []
            for h in r.json().get("hits") or []:
                title = str(h.get("title") or "")
                url = str(h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID', '')}")
                if not title:
                    continue
                pts = h.get("points") or 0
                date_s = str(h.get("created_at") or "")[:10]
                out.append(
                    {
                        "title": title,
                        "url": url,
                        "snippet": f"HN discussion ({pts} pts, {date_s})",
                    }
                )
            return out
        except (httpx.HTTPError, ValueError):
            return []

    async def _github() -> list[dict[str, str]]:
        try:
            r = await client.get(
                "https://api.github.com/search/repositories",
                params={"q": query, "sort": "stars", "order": "desc", "per_page": 4},
                headers={"Accept": "application/vnd.github+json"},
            )
            if r.status_code != 200:
                return []
            out: list[dict[str, str]] = []
            for repo in r.json().get("items") or []:
                full_name = str(repo.get("full_name") or "")
                html_url = str(repo.get("html_url") or "")
                if not full_name or not html_url:
                    continue
                stars = int(repo.get("stargazers_count") or 0)
                desc = _clean_snippet(str(repo.get("description") or ""))
                out.append(
                    {
                        "title": f"{full_name} (★{stars}) — GitHub",
                        "url": html_url,
                        "snippet": desc or f"GitHub repository {full_name}",
                    }
                )
            return out
        except (httpx.HTTPError, ValueError):
            return []

    ddg_res, wiki_res, hn_res, gh_res = await asyncio.gather(
        _ddg_ia(), _wiki(), _hn(), _github()
    )
    merged: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for bucket in (ddg_res, gh_res, wiki_res, hn_res):
        for item in bucket:
            u = item.get("url", "")
            if u and u not in seen_urls:
                seen_urls.add(u)
                merged.append(item)
                if len(merged) >= limit:
                    return merged
    return merged


async def execute_web_search(query: str, max_results: int = 6) -> tuple[bool, str]:
    clean_q = query.strip()
    if not clean_q:
        return False, "Missing 'query' argument"
    provider_pref = os.getenv("SPLITCH_WEB_SEARCH_PROVIDER", "auto").strip().lower() or "auto"
    if provider_pref == "off":
        return False, "Web search is disabled in router settings"
    limit = max(1, min(int(max_results or 6), 10))

    data_dir = Path(os.getenv("SPLITCH_DATA_DIR", str(default_data_root() / "data")))
    vault = SecretVault(data_dir)
    searxng_url = os.getenv("SPLITCH_SEARXNG_URL", "").strip()
    try:
        brave_key = resolve_secret_value(os.getenv("BRAVE_API_KEY", ""), vault=vault) or (
            vault.get("brave_api_key") or ""
        )
    except (OSError, PermissionError, ValueError):
        brave_key = ""
    try:
        tavily_key = resolve_secret_value(os.getenv("TAVILY_API_KEY", ""), vault=vault) or (
            vault.get("tavily_api_key") or ""
        )
    except (OSError, PermissionError, ValueError):
        tavily_key = ""

    async with httpx.AsyncClient(
        timeout=10.0, follow_redirects=True, headers={"User-Agent": USER_AGENT}
    ) as client:
        if provider_pref == "searxng":
            if not searxng_url:
                return False, "SearXNG selected as web search provider, but SearXNG URL is empty"
            try:
                items = await _search_searxng(client, searxng_url, clean_q, limit)
                return True, _format_results("SearXNG", clean_q, items)
            except (httpx.HTTPError, ValueError) as exc:
                return False, f"SearXNG search error: {exc}"

        if provider_pref == "brave":
            if not brave_key:
                return False, "Brave Search selected, but Brave API key is not configured"
            try:
                items = await _search_brave(client, brave_key, clean_q, limit)
                return True, _format_results("Brave Search", clean_q, items)
            except (httpx.HTTPError, ValueError) as exc:
                return False, f"Brave search error: {exc}"

        if provider_pref == "tavily":
            if not tavily_key:
                return False, "Tavily selected, but Tavily API key is not configured"
            try:
                items = await _search_tavily(client, tavily_key, clean_q, limit)
                return True, _format_results("Tavily", clean_q, items)
            except (httpx.HTTPError, ValueError) as exc:
                return False, f"Tavily search error: {exc}"

        if searxng_url:
            try:
                items = await _search_searxng(client, searxng_url, clean_q, limit)
                return True, _format_results("SearXNG", clean_q, items)
            except (httpx.HTTPError, ValueError) as exc:
                return False, f"SearXNG search error: {exc}"

        if brave_key:
            try:
                items = await _search_brave(client, brave_key, clean_q, limit)
                return True, _format_results("Brave Search", clean_q, items)
            except (httpx.HTTPError, ValueError) as exc:
                return False, f"Brave search error: {exc}"

        if tavily_key:
            try:
                items = await _search_tavily(client, tavily_key, clean_q, limit)
                return True, _format_results("Tavily", clean_q, items)
            except (httpx.HTTPError, ValueError) as exc:
                return False, f"Tavily search error: {exc}"

        items = await _search_open_sources(client, clean_q, limit)
        return True, _format_results("Open Web (DDG IA + GitHub + Wikipedia + HN)", clean_q, items)


async def execute_web_fetch(url: str) -> tuple[bool, str]:
    clean_url = url.strip()
    if not clean_url:
        return False, "Missing 'url' argument"
    ok_host, reason = await asyncio.to_thread(_is_public_url, clean_url)
    if not ok_host:
        return False, reason

    candidates: list[str] = []
    if clean_url.lower().startswith("http://"):
        https_url = "https://" + clean_url[7:]
        ok_https, _ = await asyncio.to_thread(_is_public_url, https_url)
        if ok_https:
            candidates.append(https_url)
    candidates.append(clean_url)

    try:
        async with httpx.AsyncClient(
            timeout=12.0, follow_redirects=True, headers={"User-Agent": USER_AGENT}
        ) as client:
            r: httpx.Response | None = None
            last_exc: httpx.HTTPError | None = None
            for target_url in candidates:
                try:
                    resp = await client.get(target_url)
                    resp.raise_for_status()
                    r = resp
                    break
                except httpx.HTTPError as exc:
                    last_exc = exc
                    continue
            if r is None:
                assert last_exc is not None
                raise last_exc
            final_ok, final_reason = await asyncio.to_thread(_is_public_url, str(r.url))
            if not final_ok:
                return False, f"Redirect blocked: {final_reason}"
            ctype = r.headers.get("content-type", "").lower()
            raw = r.text[:200_000]
            if "html" in ctype:
                parser = _ReadableTextExtractor()
                parser.feed(raw)
                extracted = parser.text()
            else:
                extracted = raw
            if len(extracted) > 10_000:
                extracted = extracted[:10_000] + "\n... (truncated)"
            return True, f"Fetched {r.url}:\n\n{extracted or '(empty page)'}"
    except httpx.HTTPError as exc:
        return False, f"Fetch failed: {exc}"
