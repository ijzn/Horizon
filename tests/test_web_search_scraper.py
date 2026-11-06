"""Public discovery, source honesty and bounded SSRF-safe full-text reading."""

import asyncio
from datetime import datetime, timedelta, timezone
import threading

import httpx
import ipaddress
import pytest
import trafilatura

from src.models import SourceType, WebSearchConfig
from src.scrapers import web_search
from src.scrapers.web_search import WebSearchScraper
from src import url_security


SINCE = datetime.now(timezone.utc) - timedelta(days=1)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    async def resolve(hostname, port):
        try:
            return {str(ipaddress.ip_address(hostname))}
        except ValueError:
            pass
        return {"10.0.0.9"} if hostname == "private.test" else {"8.8.8.8"}
    monkeypatch.setattr(url_security, "_resolve_hostname", resolve)


def search(monkeypatch, responses):
    calls = []

    class Engine:
        def __init__(self, **kwargs):
            assert kwargs == {"timeout": 15}

        def text(self, query, **kwargs):
            assert threading.current_thread() is not threading.main_thread()
            calls.append((query, kwargs))
            result = responses[query]
            if isinstance(result, Exception):
                raise result
            return result

    monkeypatch.setattr(web_search, "DDGS", Engine)
    return calls


def row(url="https://example.com/article", title="有价值的工具", body="Search summary"):
    return {"title": title, "href": url, "body": body}


def client(handler=None):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler or (lambda request: httpx.Response(200, text="article"))))


def test_searches_chinese_and_english_and_marks_discovery_not_publication(monkeypatch):
    calls = search(monkeypatch, {"AI tool": [row()], "独立开发": [row("HTTPS://EXAMPLE.COM:443/article#comments")]})
    cfg = WebSearchConfig(enabled=True, queries=["AI tool", "独立开发"], fetch_body=False, category="ai-tools", profile="tech-news")
    output = run(WebSearchScraper(cfg, client()).fetch(SINCE))
    assert output.successful == 2 and not output.failures
    assert len(output.items) == 1
    item = output.items[0]
    assert item.source_type == SourceType.WEB_SEARCH
    assert str(item.url) == "https://example.com/article"
    assert item.metadata["published_at_unknown"] is True
    assert item.metadata["published_at_is_discovery_time"] is True
    assert item.metadata["discovered_at"] == item.published_at.isoformat()
    assert item.metadata["search_queries"] == ["AI tool", "独立开发"]
    assert item.metadata["category"] == "ai-tools" and item.profile == "tech-news"
    assert item.metadata["content_level"] == "search_snippet"
    assert not item.metadata["extraction_attempted"] and not item.metadata["extraction_failed"]
    assert "publication date unknown" in item.content and "Search snippet only" in item.content
    assert [c[0] for c in calls] == ["AI tool", "独立开发"]
    assert calls[0][1] == {"region": "wt-wt", "timelimit": "w", "backend": "bing", "max_results": 8}


def test_one_failed_query_does_not_discard_other_results(monkeypatch):
    search(monkeypatch, {"bad": RuntimeError("engine unavailable"), "good": [row()]})
    output = run(WebSearchScraper(WebSearchConfig(enabled=True, queries=["bad", "good"], fetch_body=False), client()).fetch(SINCE))
    assert output.status == "partial_failure"
    assert output.successful == 1
    assert output.failures == {"bad": "RuntimeError: engine unavailable"}
    assert len(output.items) == 1


def test_disabled_source_does_not_search_or_fetch(monkeypatch):
    calls = search(monkeypatch, {})
    output = run(WebSearchScraper(WebSearchConfig(enabled=False, queries=["unused"]), client()).fetch(SINCE))
    assert output.status == "empty"
    assert output.successful == 0 and not output.items and not calls


def test_per_query_and_total_result_budgets(monkeypatch):
    calls = search(monkeypatch, {
        "a": [row(f"https://example.com/a/{i}") for i in range(20)],
        "b": [row(f"https://example.com/b/{i}") for i in range(20)],
    })
    cfg = WebSearchConfig(enabled=True, queries=["a", "b"], max_results_per_query=3, max_total_results=4, fetch_body=False)
    output = run(WebSearchScraper(cfg, client()).fetch(SINCE))
    assert len(output.items) == 4
    assert [str(item.url) for item in output.items][-1] == "https://example.com/b/0"
    assert all(call[1]["max_results"] == 3 for call in calls)


@pytest.mark.parametrize("unsafe", [
    "file:///etc/passwd", "http://localhost/a", "http://test.localhost/a", "http://127.0.0.1/a",
    "http://169.254.169.254/latest", "http://10.1.2.3/a", "http://[::1]/a", "https://u:p@example.com/a",
    "https://private.test/a",
])
def test_private_and_non_http_search_results_never_fetch_or_enter_pipeline(monkeypatch, unsafe):
    search(monkeypatch, {"q": [row(unsafe)]})
    requests = []

    def handle(request):
        requests.append(str(request.url))
        return httpx.Response(200, text="sensitive")

    output = run(WebSearchScraper(WebSearchConfig(enabled=True, queries=["q"]), client(handle)).fetch(SINCE))
    assert output.items == [] and requests == []


def test_redirect_to_private_network_is_blocked_and_keeps_labeled_snippet(monkeypatch):
    search(monkeypatch, {"q": [row()]})
    requests = []

    def handle(request):
        requests.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://127.0.0.1/secret"})

    output = run(WebSearchScraper(WebSearchConfig(enabled=True, queries=["q"]), client(handle)).fetch(SINCE))
    assert requests == ["https://example.com/article"]
    assert output.items[0].metadata["extraction_failed"] is True
    assert output.items[0].metadata["content_level"] == "search_snippet"
    assert "Full-text extraction failed" in output.items[0].content
    assert "Search summary" in output.items[0].content


def test_public_redirects_are_validated_per_hop_and_article_is_extracted(monkeypatch):
    search(monkeypatch, {"q": [row()]})
    requests = []

    def handle(request):
        requests.append(str(request.url))
        if request.url.path == "/article":
            return httpx.Response(302, headers={"location": "/final"})
        return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, text="<html>real body</html>")

    monkeypatch.setattr(trafilatura, "extract", lambda html, **kwargs: "Extracted full article" if "real body" in html else None)
    output = run(WebSearchScraper(WebSearchConfig(enabled=True, queries=["q"]), client(handle)).fetch(SINCE))
    assert requests == ["https://example.com/article", "https://example.com/final"]
    item = output.items[0]
    assert "Extracted full article" in item.content
    assert item.metadata["content_level"] == "article_text"
    assert item.metadata["extraction_attempted"] and not item.metadata["extraction_failed"]
    assert item.metadata["published_at_unknown"]


def test_http_error_or_unsupported_content_keeps_snippet(monkeypatch):
    search(monkeypatch, {"q": [row("https://example.com/error"), row("https://example.com/pdf")]})
    def handle(request):
        return httpx.Response(500) if request.url.path == "/error" else httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF")
    output = run(WebSearchScraper(WebSearchConfig(enabled=True, queries=["q"]), client(handle)).fetch(SINCE))
    assert len(output.items) == 2
    assert all(item.metadata["extraction_failed"] for item in output.items)
    assert all(item.metadata["content_level"] == "search_snippet" for item in output.items)


def test_oversized_declared_response_is_rejected_before_extraction(monkeypatch):
    search(monkeypatch, {"q": [row()]})
    calls = []
    monkeypatch.setattr(trafilatura, "extract", lambda *a, **kw: calls.append(True))
    output = run(WebSearchScraper(WebSearchConfig(enabled=True, queries=["q"]), client(
        lambda request: httpx.Response(200, headers={"content-length": str(10_000_000)}, content=b"small")
    )).fetch(SINCE))
    assert output.items[0].metadata["extraction_failed"]
    assert calls == []


def test_streamed_decompressed_size_is_bounded(monkeypatch):
    search(monkeypatch, {"q": [row()]})
    reads = []

    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            for i in range(10):
                reads.append(i)
                yield b"a" * 1024

    scraper = WebSearchScraper(WebSearchConfig(enabled=True, queries=["q"]), client(lambda request: httpx.Response(200, stream=Chunks())))
    scraper.MAX_RESPONSE_BYTES = 1500
    output = run(scraper.fetch(SINCE))
    assert output.items[0].metadata["extraction_failed"]
    assert reads == [0, 1]


def test_extraction_empty_or_exception_is_not_reported_as_deep_read(monkeypatch):
    search(monkeypatch, {"q": [row()]})
    monkeypatch.setattr(trafilatura, "extract", lambda *args, **kwargs: None)
    output = run(WebSearchScraper(WebSearchConfig(enabled=True, queries=["q"]), client()).fetch(SINCE))
    assert output.items[0].metadata["extraction_failed"]
    assert "Search snippet only" in output.items[0].content


def test_content_cap_and_total_http_concurrency(monkeypatch):
    search(monkeypatch, {"q": [row(f"https://example.com/{i}") for i in range(8)]})
    monkeypatch.setattr(trafilatura, "extract", lambda *args, **kwargs: "a" * 50_000)
    active = peak = 0

    async def handle(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return httpx.Response(200, text="article")

    cfg = WebSearchConfig(enabled=True, queries=["q"], max_body_chars=1000)
    output = run(WebSearchScraper(cfg, client(handle)).fetch(SINCE))
    assert len(output.items) == 8
    assert all(len(item.content) == 1000 for item in output.items)
    assert peak <= WebSearchScraper.FETCH_CONCURRENCY


def test_malformed_rows_do_not_discard_valid_page(monkeypatch):
    search(monkeypatch, {"q": [None, {}, row(title=""), {"title": "bad", "href": 123}, row()]})
    output = run(WebSearchScraper(WebSearchConfig(enabled=True, queries=["q"], fetch_body=False), client()).fetch(SINCE))
    assert len(output.items) == 1 and output.successful == 1


@pytest.mark.parametrize("rows", [[], [row("/url?opaque=abcdef")], [{"title": "no href"}]])
def test_empty_or_invalid_engine_results_are_diagnostic_failures(monkeypatch, rows):
    search(monkeypatch, {"q": rows})
    output = run(WebSearchScraper(WebSearchConfig(enabled=True, queries=["q"], fetch_body=False), client()).fetch(SINCE))
    assert output.status == "failure"
    assert "no usable public HTTP URLs" in output.failures["q"]
    assert not output.items and output.successful == 0


def test_discovery_only_private_dns_result_is_a_failure(monkeypatch):
    search(monkeypatch, {"q": [row("https://private.test/article")]})
    output = run(WebSearchScraper(WebSearchConfig(enabled=True, queries=["q"], fetch_body=False), client()).fetch(SINCE))
    assert output.status == "failure"
    assert "public URL validation" in output.failures["q"]
    assert not output.items


def test_configured_search_backend_is_forwarded(monkeypatch):
    calls = search(monkeypatch, {"q": [row()]})
    run(WebSearchScraper(WebSearchConfig(enabled=True, queries=["q"], backend="brave", fetch_body=False), client()).fetch(SINCE))
    assert calls[0][1]["backend"] == "brave"
