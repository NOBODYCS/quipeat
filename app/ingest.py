import asyncio
import importlib.util
import re
from collections import deque
from pathlib import Path
from urllib.parse import urljoin, urldefrag, urlsplit

import httpx
from pypdf import PdfReader
from docx import Document as DocxDocument

from .config import settings

_CRAWL4AI_AVAILABLE = importlib.util.find_spec("crawl4ai") is not None

CHUNK_TOKENS = 450
OVERLAP_TOKENS = 60

_HEADERS = {"User-Agent": "QuipeatBot/1.0"}
_CHALLENGE_PATTERNS = (
    "enable javascript",
    "enable js and cookies",
    "checking your browser",
    "verify you are human",
    "just a moment",
    "attention required",
    "access denied",
)


class IngestError(Exception):
    pass


def extract_text_from_file(path: Path, filename: str) -> str:
    suffix = path.suffix.lower()
    try:
        if suffix == ".pdf":
            reader = PdfReader(str(path))
            return "\n\n".join((page.extract_text() or "") for page in reader.pages)
        if suffix == ".docx":
            doc = DocxDocument(str(path))
            return "\n\n".join(p.text for p in doc.paragraphs)
        if suffix in (".txt", ".md", ".markdown", ".csv"):
            return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        raise IngestError("__err_parse")
    raise IngestError("__err_unsupported")


def _looks_like_challenge(html: str) -> bool:
    lowered = html[:20000].lower()
    return any(p in lowered for p in _CHALLENGE_PATTERNS)


def _extract_page(html: str) -> str:
    import trafilatura
    text = trafilatura.extract(html) or ""
    if len(text) < 200:
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text)
    return text


def _fetch(url: str) -> str:
    with httpx.Client(timeout=30, follow_redirects=True, headers=_HEADERS) as client:
        resp = client.get(url)
    if _looks_like_challenge(resp.text):
        raise IngestError("__err_challenge")
    resp.raise_for_status()
    return resp.text


def _extract_links(html: str, base_url: str, host: str) -> list[str]:
    links, seen = [], set()
    for href in re.findall(r'<a\b[^>]*?href\s*=\s*["\']([^"\'#]+)["\']', html, re.I):
        if href.startswith(("mailto:", "javascript:", "tel:", "data:")):
            continue
        url = urljoin(base_url, href)
        url, _ = urldefrag(url)
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or parts.netloc != host:
            continue
        if parts.path.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".svg", ".pdf",
                                         ".zip", ".tar.gz", ".tgz", ".xml", ".css", ".js")):
            continue
        if url not in seen:
            seen.add(url)
            links.append(url)
    return links


def _crawl4ai_extract(url: str, max_pages: int) -> str:
    """Playwright-backed extraction via Crawl4AI: renders JS (gets past
    client-side challenge walls), emits boilerplate-free Markdown with
    headings/tables/code preserved, and deep-crawls same-domain pages."""
    from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig
    from crawl4ai.deep_crawling import BFSDeepCrawlStrategy
    from crawl4ai.content_filter_strategy import PruningContentFilter
    from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator

    browser_cfg = BrowserConfig(headless=True, text_mode=True)
    run_cfg = CrawlerRunConfig(
        cache_mode="BYPASS",
        page_timeout=60000,
        excluded_tags=["nav", "footer", "header", "aside"],
        markdown_generator=DefaultMarkdownGenerator(
            content_filter=PruningContentFilter(
                threshold=0.48, threshold_type="fixed", min_ignore_threshold=0)),
        deep_crawl_strategy=BFSDeepCrawlStrategy(
            max_depth=2, include_external=False, max_pages=max_pages),
        stream=False,
    )

    async def _run() -> list[str]:
        texts = []
        async with AsyncWebCrawler(config=browser_cfg) as crawler:
            results = await crawler.arun(url, config=run_cfg)
            if not isinstance(results, list):
                results = [results]
            for r in results:
                if not r.success:
                    continue
                md = getattr(r, "markdown", None)
                raw_text = getattr(md, "raw_markdown", None) or (md if isinstance(md, str) else "") or ""
                if _looks_like_challenge(r.html or ""):
                    raise IngestError("__err_challenge")
                if len(raw_text.strip()) >= 200:
                    texts.append(raw_text.strip())
        return texts

    texts = asyncio.run(_run())
    if not texts:
        raise IngestError("__err_extract")
    return "\n\n".join(texts)


def extract_text_from_url(url: str, max_pages: int = 1) -> str:
    """Fetch a URL and extract its text. Prefers Crawl4AI (JS rendering +
    clean Markdown + deep crawl); falls back to the httpx+trafilatura path
    when the package isn't installed. Raises IngestError with a specific key
    on blocked/empty pages so the UI shows a clear message."""
    if _CRAWL4AI_AVAILABLE:
        try:
            return _crawl4ai_extract(url, max_pages)
        except IngestError:
            raise
        except Exception:
            pass
    return _plain_extract_text_from_url(url, max_pages)


def _plain_extract_text_from_url(url: str, max_pages: int = 1) -> str:
    try:
        host = urlsplit(url).netloc
        first_html = _fetch(url)
    except IngestError:
        raise
    except Exception:
        raise IngestError("__err_fetch")

    if _looks_like_challenge(first_html):
        raise IngestError("__err_challenge")

    texts: list[str] = []
    try:
        first_text = _extract_page(first_html)
    except Exception:
        raise IngestError("__err_extract")
    if len(first_text) < 200:
        raise IngestError("__err_extract")
    texts.append(first_text)

    if max_pages <= 1:
        return first_text

    pages_fetched = 1
    queue = deque(_extract_links(first_html, url, host))
    visited = {url}
    while queue and pages_fetched < max_pages:
        next_url = queue.popleft()
        if next_url in visited:
            continue
        visited.add(next_url)
        try:
            html = _fetch(next_url)
        except Exception:
            continue
        if _looks_like_challenge(html):
            continue
        pages_fetched += 1
        try:
            text = _extract_page(html)
        except Exception:
            continue
        if len(text) >= 200:
            texts.append(text)
        for link in _extract_links(html, next_url, host):
            if link not in visited and len(visited) < max_pages * 4:
                queue.append(link)
    if not texts:
        raise IngestError("__err_extract")
    return "\n\n".join(texts)


def _tokens(text: str) -> list[str]:
    return text.split()


def chunk_text(text: str, chunk_tokens: int = CHUNK_TOKENS,
               overlap_tokens: int = OVERLAP_TOKENS) -> list[str]:
    text = re.sub(r"\s+", " ", text).strip()
    words = _tokens(text)
    if not words:
        return []
    chunks = []
    step = max(1, chunk_tokens - overlap_tokens)
    for start in range(0, len(words), step):
        piece = " ".join(words[start:start + chunk_tokens])
        if piece:
            chunks.append(piece)
        if start + chunk_tokens >= len(words):
            break
    return chunks
