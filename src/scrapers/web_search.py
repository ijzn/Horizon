"""Bounded public-web discovery with explicit discovery/publication semantics."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import ipaddress
from itertools import islice
import threading
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlsplit, urlunsplit

from ddgs import DDGS
import httpx

from .base import BaseScraper, FetchResult
from ..extractors.trafilatura import ARTICLE_HEADERS
from ..models import ContentItem, SourceType
from ..url_security import UnsafeURLError, validate_http_url, validate_public_http_url

if TYPE_CHECKING:
    from ..models import WebSearchConfig


_SEARCH_SLOTS = threading.BoundedSemaphore(2)


class WebSearchScraper(BaseScraper):
    """Search multiple queries, then read public pages within strict limits.

    Search engines can return old pages despite their time filter. ``published_at``
    is therefore a compatibility discovery timestamp, never a claimed publication
    date; metadata makes the unknown publication time explicit to downstream AI.
    """

    MAX_RESPONSE_BYTES = 2 * 1024 * 1024
    MAX_REDIRECTS = 5
    FETCH_CONCURRENCY = 3
    PAGE_TIMEOUT = 25.0
    SEARCH_TIMEOUT = 25.0

    def __init__(self, config: WebSearchConfig, http_client: httpx.AsyncClient):
        super().__init__({"web_search": config}, http_client)
        self.search_config = config

    def _search(self, query: str) -> list[dict]:
        config = self.search_config
        # Timed-out to_thread work cannot be killed; bound its real concurrency
        # as well, so slow engines cannot accumulate unrestricted live requests.
        if not _SEARCH_SLOTS.acquire(timeout=1):
            raise TimeoutError("Public search workers are busy")
        try:
            rows = DDGS(timeout=15).text(
                query, region=config.region, timelimit="w", backend=config.backend,
                max_results=config.max_results_per_query,
            )
            return list(islice(rows, config.max_results_per_query))
        finally:
            _SEARCH_SLOTS.release()

    @staticmethod
    def _canonical_url(value: str) -> str:
        validate_http_url(value)
        parsed = urlsplit(value)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        try:
            literal = ipaddress.ip_address(hostname)
        except ValueError:
            pass
        else:
            if not literal.is_global or literal.is_multicast:
                raise UnsafeURLError("Search result points to a non-public address")
        port = parsed.port
        if port == (443 if parsed.scheme.lower() == "https" else 80):
            port = None
        host = f"[{hostname}]" if ":" in hostname else hostname
        authority = f"{host}:{port}" if port else host
        return urlunsplit((parsed.scheme.lower(), authority, parsed.path or "/", parsed.query, ""))

    async def fetch(self, since: datetime) -> FetchResult:
        result = FetchResult()
        config = self.search_config
        if not config.enabled:
            return result
        discovered = datetime.now(timezone.utc)
        candidates: dict[str, tuple[str, str, list[str]]] = {}
        # Serial queries keep DDGS sessions and their result budgets bounded.
        for raw_query in config.queries:
            query = raw_query.strip()
            if not query:
                continue
            try:
                rows = await asyncio.wait_for(asyncio.to_thread(self._search, query), timeout=self.SEARCH_TIMEOUT)
            except Exception as exc:
                result.fail(query, exc)
                continue
            valid_urls = 0
            for row in rows:
                if not isinstance(row, dict):
                    continue
                title = row.get("title")
                href = row.get("href") or row.get("url")
                if not isinstance(title, str) or not title.strip() or not isinstance(href, str):
                    continue
                try:
                    url = self._canonical_url(href.strip())
                except (ValueError, TypeError):
                    continue
                valid_urls += 1
                if url in candidates:
                    candidates[url][2].append(query)
                    continue
                if len(candidates) >= config.max_total_results:
                    continue
                snippet = row.get("body") or row.get("snippet") or ""
                if not isinstance(snippet, str):
                    snippet = ""
                candidates[url] = (title.strip()[:1000], snippet[:min(4000, config.max_body_chars)], [query])

            if not valid_urls:
                result.fail(query, ValueError("Search returned no usable public HTTP URLs"))
            else:
                result.successful += 1

        semaphore = asyncio.Semaphore(self.FETCH_CONCURRENCY)

        async def collect(url, entry):
            async with semaphore:
                title, snippet, queries = entry
                try:
                    # Also validate discovery-only URLs. Never retain a result
                    # directing later enrichment toward a private destination.
                    await asyncio.wait_for(validate_public_http_url(url), timeout=8)
                except Exception:
                    return None
                body = None
                extraction_failed = False
                if config.fetch_body:
                    try:
                        body = await asyncio.wait_for(self._extract_body(url), timeout=self.PAGE_TIMEOUT)
                        extraction_failed = not bool(body and body.strip())
                    except Exception:
                        extraction_failed = True
                metadata = {
                    "discovered_at": discovered.isoformat(),
                    "published_at_unknown": True,
                    "published_at_is_discovery_time": True,
                    "search_queries": list(dict.fromkeys(queries)),
                    "search_time_filter": "week",
                    "requested_since": since.isoformat(),
                    "search_snippet": snippet,
                    "content_level": "article_text" if body else "search_snippet",
                    "extraction_attempted": config.fetch_body,
                    "extraction_failed": extraction_failed,
                }
                if config.category is not None:
                    metadata["category"] = config.category
                reading = "Article text was extracted." if body else "Search snippet only; article text was not retrieved."
                if extraction_failed:
                    reading += " Full-text extraction failed."
                prefix = (
                    f"[Public web discovery at {discovered.isoformat()}; publication date unknown. "
                    f"The discovery time is not the article publication time. {reading}]\n\n"
                )
                return ContentItem(
                    id=self._generate_id("web_search", "page", hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]),
                    source_type=SourceType.WEB_SEARCH,
                    title=title, url=url, content=(prefix + (body or snippet))[:config.max_body_chars],
                    published_at=discovered, fetched_at=discovered,
                    profile=config.profile, metadata=metadata,
                )

        # gather preserves search order even when HTTP responses complete out of order.
        items = await asyncio.gather(*(collect(url, entry) for url, entry in candidates.items()))
        result.items = [item for item in items if item is not None]
        accepted_queries = {query for item in result.items for query in item.metadata["search_queries"]}
        candidate_queries = {query for _, _, queries in candidates.values() for query in queries}
        for query in candidate_queries - accepted_queries:
            if query not in result.failures:
                result.successful -= 1
                result.fail(query, ValueError("All search destinations failed public URL validation"))
        return result

    async def _extract_body(self, url: str) -> str | None:
        import trafilatura

        html = await self._fetch_html(url)
        text = await asyncio.to_thread(trafilatura.extract, html, favor_precision=True)
        return text.strip()[:self.search_config.max_body_chars] if text and text.strip() else None

    async def _fetch_html(self, url: str) -> str:
        """Stream with per-hop SSRF checks and a decompressed response limit."""
        current = url
        for hop in range(self.MAX_REDIRECTS + 1):
            await validate_public_http_url(current)
            async with self.client.stream(
                "GET", current, follow_redirects=False, headers=ARTICLE_HEADERS,
                timeout=httpx.Timeout(15.0),
            ) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location or hop == self.MAX_REDIRECTS:
                        raise ValueError("Invalid or excessive article redirects")
                    current = urljoin(current, location)
                    continue
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if content_type and content_type not in {"text/html", "application/xhtml+xml", "text/plain"}:
                    raise ValueError("Article response is not HTML or text")
                length = response.headers.get("content-length")
                if length and int(length) > self.MAX_RESPONSE_BYTES:
                    raise ValueError("Article exceeds response limit")
                body = bytearray()
                async for part in response.aiter_bytes():
                    if len(body) + len(part) > self.MAX_RESPONSE_BYTES:
                        raise ValueError("Article exceeds response limit")
                    body.extend(part)
                return bytes(body).decode(response.encoding or "utf-8", errors="replace")
        raise ValueError("Too many article redirects")
