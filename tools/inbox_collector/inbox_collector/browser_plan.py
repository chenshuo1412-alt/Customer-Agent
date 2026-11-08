"""Headed-browser plan. A live merchant page is refused before launch."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from inbox_collector.config import AppConfig, readiness_problems

_BLOCKED_PROFILE_PARTS = (
    "google/chrome",
    "google\\chrome",
    "application support/google",
    "chromium",
    "microsoft/edge",
    "user-data-dir",
    "cookies",
    "/default/cookies",
)


class BrowserLaunchRefused(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def assert_isolated_profile(path: Path) -> None:
    lowered = str(path).lower()
    if any(part in lowered for part in _BLOCKED_PROFILE_PARTS):
        raise BrowserLaunchRefused("refusing_existing_browser_profile")
    if path.name.lower() in {"cookies", "login data"}:
        raise BrowserLaunchRefused("refusing_cookie_store")


def browser_plan(config: AppConfig, mapping: dict[str, Any] | None = None) -> dict[str, object]:
    profile = config.data_dir / "own-browser-profile"
    assert_isolated_profile(profile)
    problems = readiness_problems(config, mapping)
    launch = config.mode in {"browser_local", "login_prep"} and not problems
    return {
        "verified": False,
        "launch": launch,
        "headless": False,
        "channel": "playwright_chromium_new_profile",
        "attach_to_existing_chrome": False,
        "use_cdp_endpoint": False,
        "read_cookies": False,
        "autofill_credentials": False,
        "profile_dir": str(profile),
        "will_open": config.entry_url if launch else None,
        "collects_messages": config.mode == "browser_local" and launch,
        "production_running": False,
        "merchant_entry_blocked": "https://mms.pinduoduo.com/chat-merchant/index.html#/",
        "sample_interval_seconds": config.sample_interval_seconds,
        "interval_is_application_setting": True,
        "interval_is_bot_routine": False,
        "open_conversation": config.open_conversation,
        "open_conversation_may_mark_read": True,
        "captcha_bypass": False,
        "auto_relogin": False,
        "refusal": None if launch else (problems[0] if problems else "real_dom_unverified"),
        "real_store_connected": False,
        "real_store_login_verified": False,
    }


def launch_independent_browser(
    config: AppConfig | None = None,
    mapping: dict[str, Any] | None = None,
) -> Any:
    """Start the dedicated headed browser, or refuse before creating a process."""
    if config is None or config.mode == "real":
        raise BrowserLaunchRefused("real_dom_unverified")
    if config.mode == "login_prep":
        raise BrowserLaunchRefused("use_login_prep_command")
    if config.mode != "browser_local":
        raise BrowserLaunchRefused("real_dom_unverified")
    problems = readiness_problems(config, mapping)
    if problems:
        raise BrowserLaunchRefused(problems[0])
    from inbox_collector.browser_session import DedicatedBrowser

    browser = DedicatedBrowser(config.data_dir / "own-browser-profile")
    browser.start()
    return browser
