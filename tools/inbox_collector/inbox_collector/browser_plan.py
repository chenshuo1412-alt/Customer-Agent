"""Future headed-browser plan. This build always refuses to launch it."""

from __future__ import annotations

from pathlib import Path

from inbox_collector.config import AppConfig

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


def browser_plan(config: AppConfig) -> dict[str, object]:
    profile = config.data_dir / "own-browser-profile"
    assert_isolated_profile(profile)
    return {
        "verified": False,
        "launch": False,
        "headless": False,
        "channel": "chrome_or_chromium_own_profile",
        "attach_to_existing_chrome": False,
        "use_cdp_endpoint": False,
        "read_cookies": False,
        "autofill_credentials": False,
        "profile_dir": str(profile),
        "entry_url_constant": "https://mms.pinduoduo.com/chat-merchant/index.html#/",
        "sample_interval_seconds": config.sample_interval_seconds,
        "interval_is_application_setting": True,
        "interval_is_bot_routine": False,
        "open_conversation": config.open_conversation,
        "open_conversation_may_mark_read": True,
        "captcha_bypass": False,
        "auto_relogin": False,
        "refusal": "real_dom_unverified",
    }


def launch_independent_browser(config: AppConfig | None = None) -> None:
    """The plan exists so a later verified adapter has a safe shape.

    Reaching this function still does not start Chrome, attach to a running
    browser, read cookies, or open a debugging port.
    """
    del config
    raise BrowserLaunchRefused("real_dom_unverified")
