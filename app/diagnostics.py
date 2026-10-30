"""Health and degradation tracking for every operational component.

The rule going forward: any component that can fail silently (browser
missing, provider down, crawl degraded) must (1) record its state here and
(2) surface it in logs + /admin diagnostics + the JSON health endpoint."""
import logging
import sqlite3
import time

from . import db

log = logging.getLogger("quipeat.diagnostics")

_state: dict[str, dict] = {}


def record(component: str, status: str, detail: str = "") -> None:
    """status: 'ok' | 'degraded' | 'down'. Called by components on init and
    whenever they notice a failure or fallback."""
    prev = _state.get(component, {}).get("status")
    _state[component] = {"status": status, "detail": detail[:300],
                         "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    if prev != status:
        log.log(logging.WARNING if status != "ok" else logging.INFO,
                "diagnostics: %s -> %s%s", component, status,
                f" ({detail[:120]})" if detail else "")


def snapshot() -> dict[str, dict]:
    return dict(_state)


def check_database() -> tuple[str, str]:
    try:
        db.query_one("SELECT 1")
        return "ok", ""
    except sqlite3.Error as e:
        return "down", str(e)


def _probe_browser() -> None:
    """Launch and close a headless browser once. Runs in a worker thread —
    never on the event loop — so it is safe to call from sync and async
    contexts alike (asyncio.run inside a running loop raises)."""
    import asyncio

    async def _run():
        from crawl4ai import AsyncWebCrawler, BrowserConfig
        async with AsyncWebCrawler(config=BrowserConfig(headless=True)) as crawler:
            return True

    asyncio.run(_run())


def check_crawler() -> tuple[str, str]:
    """Is Crawl4AI importable and its Playwright browser actually launchable?
    Catches the 'browser executable missing' class of failure at startup and
    on demand, instead of discovering it per-crawl in a WARNING."""
    try:
        from crawl4ai import AsyncWebCrawler, BrowserConfig  # noqa: F401
    except Exception as e:
        return "down", f"crawl4ai not importable: {e}"

    import concurrent.futures
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_probe_browser)
            future.result(timeout=60)
        return "ok", "JS rendering available"
    except Exception as e:
        return "degraded", f"browser unusable, falling back to trafilatura: {str(e)[:200]}"


def check_llm_builtin() -> tuple[str, str]:
    from .config import settings
    if settings.llm_mock:
        return "ok", "mock provider"
    if settings.mistral_api_key:
        return "ok", "built-in Mistral key configured"
    return "degraded", "no built-in key: users must connect their own LLM key"


_last_full_check = 0.0
FULL_CHECK_INTERVAL = 300  # seconds; expensive probes (browser launch) are cached


def run_all_checks() -> dict[str, dict]:
    """Full checks are cached for FULL_CHECK_INTERVAL: the crawler probe
    launches a real browser, which is too slow to run on every admin page
    load. Fresh component state recorded at runtime via record() always
    wins over the cache."""
    global _last_full_check
    now = time.time()
    if _state and now - _last_full_check < FULL_CHECK_INTERVAL:
        return snapshot()
    _last_full_check = now
    checks = {"database": check_database, "crawler": check_crawler,
              "llm_builtin": check_llm_builtin}
    results = {}
    for name, fn in checks.items():
        try:
            status, detail = fn()
        except Exception as e:
            status, detail = "down", f"check crashed: {e}"
        results[name] = {"status": status, "detail": detail,
                         "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")}
        record(name, status, detail)
    return results
