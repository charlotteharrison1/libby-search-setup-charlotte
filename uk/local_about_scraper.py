#!/usr/bin/env python3
"""Drive a local, already-logged-in Chrome session to About-scrape a small,
specific list of group URLs — the machinery behind ``--about`` on
``uk.pipeline``/``uk.pipeline_ward``.

Why this exists: the About-scraper (``libby_download/scrape_group_about.py``)
normally runs on the shared ``libby`` device, requires a supervised session,
and is slow enough that applying it broadly isn't realistic (see
``uk/queue_unsure_for_about.py`` for the "escalate on Unsure, run on libby"
alternative). For a small handful of groups — exactly the Unsure-and-
uncached set from one area's own run — it's faster and less friction to
scrape them right here, on this machine, using your own Facebook login,
than to build a queue file and make a separate trip to a different device.

This is explicitly NOT a replacement for libby's main scrape (large,
overnight-paced, unattended-for-days) — it's a small, occasional, inline
supplement, capped by ``max_groups`` and with a circuit breaker on
consecutive failures so a bad batch can't silently balloon into a long
unattended run against a personal account.

Reuses libby_download's own, already-tested parsing (``extract_about_info``,
``extract_activity_info``) and page-health check (``page_loaded_ok``) rather
than reimplementing them — imported lazily, at call time, from
``uk.settings.LIBBY_DOWNLOAD_PATH``, so nothing in this repo needs selenium
or a sibling checkout unless ``--about`` is actually used. Chrome/chromedriver
setup itself (unlike script.py's Linux-path defaults) relies on Selenium
Manager's auto-resolution — no manual driver download needed, just
`pip install selenium` (4.6+).

One-time setup, before ``--about`` will do anything:

    pip install selenium
    python -m uk.local_about_scraper --login

That opens a real (non-headless) Chrome window against a persistent local
profile (uk/settings.LOCAL_CHROME_PROFILE_PATH — gitignored, this machine
only) and waits for you to log into Facebook by hand. After that, the
profile stays logged in and every subsequent ``--about`` run is unattended.
"""

import argparse
import json
import logging
import random
import sys
import time
from pathlib import Path

import pandas as pd

from uk.settings import ABOUT_PAGES_DIR, LIBBY_DOWNLOAD_PATH, LOCAL_CHROME_PROFILE_PATH

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)

# Same conservative pacing as about_settings.py's defaults — no reason to
# scrape faster just because this is now inline in a pipeline run, and
# every extra reason to stay conservative given this uses a personal
# account rather than a dedicated, expendable scraping one.
SLEEP_RANDOM_MIN_SECONDS = 2
SLEEP_RANDOM_MAX_SECONDS = 5
SLEEP_BASE_SECONDS = 5

# Safety valve: a single --about invocation can never trigger an
# unexpectedly large unattended burst, even if an area's Unsure rate is
# unusually high.
DEFAULT_MAX_GROUPS = 25

# Stop early if this many consecutive groups fail to load — likely a
# rate-limit/checkpoint, and grinding through the rest of the list is both
# unlikely to succeed and the wrong thing to keep doing to the account.
DEFAULT_MAX_CONSECUTIVE_FAILURES = 3

ABOUT_ROW_COLUMNS = ["url", "name", "search_details", "about", "activity", "processed"]


class LocalAboutScraperUnavailable(Exception):
    """Raised when selenium or the libby_download sibling checkout aren't
    available. --about should catch this and degrade to context-only,
    never crash the pipeline run over it."""


def _import_scraper_internals():
    """Lazily import selenium + the specific libby_download functions this
    module reuses. Only the small, stable surface needed (driver-health-
    check, HTML parsing) — not the Linux-path-oriented driver constructor,
    which this module has its own Mac/Selenium-Manager-friendly version of
    below."""
    try:
        from selenium import webdriver  # noqa: F401
        from selenium.webdriver.chrome.options import Options  # noqa: F401
    except ImportError as e:
        raise LocalAboutScraperUnavailable(
            "selenium is not installed — run `pip install selenium` to use --about"
        ) from e

    if not LIBBY_DOWNLOAD_PATH.is_dir():
        raise LocalAboutScraperUnavailable(
            f"libby_download not found at {LIBBY_DOWNLOAD_PATH} — set the "
            "LIBBY_DOWNLOAD_PATH env var to your local checkout, or skip --about"
        )
    if str(LIBBY_DOWNLOAD_PATH) not in sys.path:
        sys.path.insert(0, str(LIBBY_DOWNLOAD_PATH))
    try:
        from script import page_loaded_ok
        from extract_about import extract_about_info, extract_activity_info
    except ImportError as e:
        raise LocalAboutScraperUnavailable(
            f"Could not import scraper internals from {LIBBY_DOWNLOAD_PATH} ({e}) "
            "— libby_download may have changed; --about can't run until this is fixed"
        ) from e

    return page_loaded_ok, extract_about_info, extract_activity_info


def _create_local_driver(headless: bool):
    """Persistent-profile Chrome driver for this machine. Unlike
    script.py's create_persistent_chrome_driver, deliberately does not
    hardcode a chrome_binary_path/driver_path (those default to Linux
    paths, built for libby) — Selenium Manager (selenium>=4.6) resolves a
    matching chromedriver for whatever Chrome is actually installed here."""
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options

    LOCAL_CHROME_PROFILE_PATH.mkdir(parents=True, exist_ok=True)
    options = Options()
    options.add_argument(f"--user-data-dir={LOCAL_CHROME_PROFILE_PATH}")
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    return webdriver.Chrome(options=options)


# script.py's page_loaded_ok() bad_markers are tuned for CONTENT pages
# (a group/about page that errored or is gated) — facebook.com's own
# logged-out homepage triggers none of them, since it's a perfectly valid
# page, just a marketing/login page rather than a newsfeed. Verified this
# empirically: page_loaded_ok alone reports "fine" even on a completely
# fresh, never-authenticated profile. These markers only ever appear on
# the logged-out homepage, never on a real logged-in newsfeed.
_LOGGED_OUT_MARKERS = ("create new account", "forgotten password")


def is_logged_in(driver, page_loaded_ok) -> bool:
    driver.get("https://www.facebook.com")
    if not page_loaded_ok(driver):
        return False
    page_text = driver.page_source.lower()
    return not any(marker in page_text for marker in _LOGGED_OUT_MARKERS)


def login():
    """One-time interactive setup: open a real Chrome window against the
    local persistent profile and wait for you to log into Facebook by hand.
    Never call this automatically — logging in is a deliberate action only
    you should take, same reasoning script.py's own manual-login wait is
    built on."""
    page_loaded_ok, _, _ = _import_scraper_internals()
    driver = _create_local_driver(headless=False)
    driver.get("https://www.facebook.com")
    print("Log into Facebook in the opened Chrome window, then press Enter here...")
    input()
    if is_logged_in(driver, page_loaded_ok):
        print(f"Logged in. Profile saved at {LOCAL_CHROME_PROFILE_PATH} — future --about runs will reuse it.")
    else:
        print("Doesn't look logged in yet (page still shows a login wall) — try again.")
    driver.quit()


def scrape_about_locally(
    urls: list[str],
    name_by_url: dict[str, str] | None = None,
    max_groups: int = DEFAULT_MAX_GROUPS,
    max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES,
    headless: bool = True,
) -> pd.DataFrame:
    """About-scrape up to max_groups of the given URLs using the local
    persistent Chrome profile. Returns a DataFrame in the exact shape
    libby_download's scrape_group_about.py writes (ABOUT_ROW_COLUMNS) —
    caller is expected to write it under uk/data/about_pages/ and reread it
    through uk.about_context.load_about_context, reusing that module's
    existing parsing/sanitization rather than duplicating it here.

    Raises LocalAboutScraperUnavailable if selenium/libby_download aren't
    available, or RuntimeError if the local profile doesn't look logged in
    — both meant to be caught by the caller and treated as "no local
    About-scrape this run", never as a reason to abort the whole pipeline.
    """
    name_by_url = name_by_url or {}
    page_loaded_ok, extract_about_info, extract_activity_info = _import_scraper_internals()

    if len(urls) > max_groups:
        logger.warning(
            "%d groups requested, capping at max_groups=%d (raise with --about-limit if you mean it)",
            len(urls), max_groups,
        )
    urls = urls[:max_groups]

    logger.info(
        "=== Using the LOCAL About scraper: opening a Chrome window on this "
        "machine (your own Facebook login) to fetch %d group About page(s) ===",
        len(urls),
    )
    driver = _create_local_driver(headless=headless)
    try:
        if not is_logged_in(driver, page_loaded_ok):
            raise RuntimeError(
                f"Local Chrome profile at {LOCAL_CHROME_PROFILE_PATH} doesn't look logged "
                "into Facebook. Run `python -m uk.local_about_scraper --login` once."
            )

        rows = []
        consecutive_failures = 0
        for i, url in enumerate(urls):
            about_url = f"{url.rstrip('/')}/about"
            logger.info("  [%d/%d] %s", i + 1, len(urls), about_url)
            try:
                driver.get(about_url)
                if not page_loaded_ok(driver):
                    logger.warning("    page did not load ok — skipping")
                    consecutive_failures += 1
                else:
                    consecutive_failures = 0
                    html = driver.page_source
                    about_info = extract_about_info(html)
                    activity_info = extract_activity_info(html)
                    rows.append({
                        "url": url,
                        "name": name_by_url.get(url, ""),
                        "search_details": "",
                        "about": json.dumps(about_info),
                        "activity": json.dumps(activity_info),
                        "processed": True,
                    })
            except Exception:
                logger.exception("    error scraping %s", url)
                consecutive_failures += 1

            if consecutive_failures >= max_consecutive_failures:
                logger.warning(
                    "  Stopping early after %d consecutive failures (rate-limit/checkpoint?) — "
                    "%d/%d groups attempted", consecutive_failures, i + 1, len(urls),
                )
                break

            if i < len(urls) - 1:
                sleep_s = random.randint(SLEEP_RANDOM_MIN_SECONDS, SLEEP_RANDOM_MAX_SECONDS) + SLEEP_BASE_SECONDS
                time.sleep(sleep_s)
    finally:
        driver.quit()

    logger.info("Scraped %d/%d group(s) successfully", len(rows), len(urls))
    return pd.DataFrame(rows, columns=ABOUT_ROW_COLUMNS)


def write_local_about(df: pd.DataFrame, slug: str, out_dir: Path = ABOUT_PAGES_DIR) -> Path:
    """Write scrape_about_locally's result under uk/data/about_pages/,
    joining the permanent global cache for every future run — not just
    this one. "_local" suffix keeps it visually distinct from a "real"
    libby-pulled about-scrape for the same slug (see pull_about.sh) without
    risking a collision. out_dir defaults to the real ABOUT_PAGES_DIR but
    is overridable (tests use this — writing here is a real side effect,
    not something to silently redirect via module-global monkeypatching)."""
    out_path = out_dir / f"{slug}_about_local.csv"
    if out_path.exists():
        existing = pd.read_csv(out_path, dtype=str, encoding="utf-8", encoding_errors="surrogatepass")
        df = pd.concat([existing, df], ignore_index=True).drop_duplicates(subset=["url"], keep="last")
    df.to_csv(out_path, index=False, encoding="utf-8", errors="surrogatepass")
    logger.info("Wrote %d row(s) → %s", len(df), out_path)
    return out_path


def main():
    parser = argparse.ArgumentParser(description="One-time local login setup for --about")
    parser.add_argument("--login", action="store_true", help="Open Chrome and wait for you to log into Facebook")
    args = parser.parse_args()

    if args.login:
        login()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
