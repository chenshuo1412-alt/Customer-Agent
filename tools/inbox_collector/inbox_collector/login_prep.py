"""Open a dedicated browser for a person to log in. Does not collect by itself."""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlsplit

from inbox_collector.browser_plan import BrowserLaunchRefused
from inbox_collector.browser_session import DedicatedBrowser
from inbox_collector.config import AppConfig, readiness_problems
from inbox_collector.health import build_health
from inbox_collector.mapping import login_prep_entry_problems
from inbox_collector.store import Store


def run_login_prep(
    config: AppConfig,
    mapping: dict[str, Any] | None,
    *,
    hold_seconds: float | None = None,
) -> dict[str, Any]:
    """Foreground manual-login window.

    ``hold_seconds`` lets a local test close the window. The command-line
    path leaves it None and waits until the person closes the browser.
    Nothing is read from the page into the draft database.
    """
    store = Store(config.data_dir)
    problems = readiness_problems(config, mapping)
    if config.mode != "login_prep":
        problems.append("not_login_prep")
    if problems:
        store.set_state("stopped", "config_incomplete")
        store.set_meta("readiness_problems", ",".join(dict.fromkeys(problems)))
        report = build_health(store, config, mapping, process_alive=False)
        report["opened_host"] = None
        store.close()
        return report

    store.set_state("login_prep", "waiting_for_manual_login")
    store.set_meta("collection_enabled", "0")
    store.set_meta("real_store_login_verified", "0")
    store.set_meta("production_running", "0")
    store.set_meta("bot_reply_received", "0")
    browser = DedicatedBrowser(config.data_dir / "own-browser-profile", config.browser_executable)
    opened_host: str | None = None
    window_id: str | None = None
    try:
        browser.start()
        from inbox_collector.confirmed_text import publish_window

        window_id = publish_window(config.data_dir)
        store.set_meta("browser_launched", "1")
        store.set_meta("browser_headless", "0")
        store.set_meta("browser_pid", str(browser.pid or ""))
        browser.page.goto(config.entry_url, wait_until="domcontentloaded", timeout=30000)
        opened_host = (urlsplit(browser.page.url).hostname or "").lower()
        if login_prep_entry_problems(browser.page.url):
            store.open_alert("login_prep_left_allowed_page")
            store.set_state("paused", "login_prep_left_allowed_page")
            return _finish(store, config, mapping, opened_host)
        _wait_for_close(browser.page, hold_seconds, config.data_dir, window_id)
        store.set_state("stopped", "login_prep_closed")
    except BrowserLaunchRefused as exc:
        store.open_alert(exc.code)
        store.set_state("stopped", exc.code)
    except Exception:
        store.open_alert("network")
        store.set_state("paused", "login_prep_navigation_failed")
    finally:
        if window_id is not None:
            from inbox_collector.confirmed_text import clear_window

            clear_window(config.data_dir)
        browser.close()
    return _finish(store, config, mapping, opened_host)


def _wait_for_close(page: Any, hold_seconds: float | None, data_dir: Any, window_id: str | None = None) -> None:
    from inbox_collector.dom_diagnose import service_diagnose_request

    started = time.monotonic()
    while not page.is_closed():
        service_diagnose_request(page, data_dir, window_id=window_id)
        if hold_seconds is not None and time.monotonic() - started >= hold_seconds:
            return
        time.sleep(0.2)


def _finish(
    store: Store,
    config: AppConfig,
    mapping: dict[str, Any] | None,
    opened_host: str | None,
) -> dict[str, Any]:
    report = build_health(store, config, mapping, process_alive=False)
    report["opened_host"] = opened_host
    report["production_running"] = False
    report["merchant_collection_enabled"] = False
    report["real_store_login_verified"] = False
    report["messages_collected"] = store.count_messages()
    report["drafts_created"] = len(store.drafts())
    store.close()
    return report
