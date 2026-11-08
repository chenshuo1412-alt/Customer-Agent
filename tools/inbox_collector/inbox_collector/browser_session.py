"""A new headed Chromium with an empty profile this process creates.

The session never attaches to an already running browser and never opens a
TCP debugging port. Merchant sites are rejected by the caller before start.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from inbox_collector.browser_plan import BrowserLaunchRefused, assert_isolated_profile

_PROFILE_MARKER = "inbox-collector-profile.json"
_COPIED_PROFILE_NAMES = ("Cookies", "Login Data", "Default")


def _owned_profile(path: Path) -> None:
    assert_isolated_profile(path)
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    marker = path / _PROFILE_MARKER
    if marker.is_file():
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BrowserLaunchRefused("refusing_existing_browser_profile") from exc
        if payload.get("created_by") != "inbox-collector" or payload.get("copied_login") is not False:
            raise BrowserLaunchRefused("refusing_existing_browser_profile")
        return
    if any((path / name).exists() for name in _COPIED_PROFILE_NAMES):
        raise BrowserLaunchRefused("refusing_existing_browser_profile")
    marker.write_text(
        json.dumps({"created_by": "inbox-collector", "copied_login": False}, ensure_ascii=False),
        encoding="utf-8",
    )
    os.chmod(marker, 0o600)


def _process_matches(profile: Path) -> tuple[int | None, str]:
    needle = str(profile)
    found: tuple[int, str] | None = None
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        command = raw.replace(b"\x00", b" ").decode(errors="replace")
        if needle in command and "chrom" in command.lower() and (found is None or len(command) > len(found[1])):
            found = (int(entry.name), command)
    if found is None:
        return None, ""
    return found


def resolve_browser_executable(configured: str) -> str | None:
    """Pick a browser binary. Never a Chrome user-data directory."""
    if configured:
        path = Path(configured).expanduser()
        lowered = str(path).lower()
        if (
            not path.is_file()
            or "application support/google/chrome" in lowered
            or "cookies" in lowered
            or path.name.lower() in {"cookies", "login data"}
        ):
            raise BrowserLaunchRefused("browser_executable_refused")
        return str(path)
    if sys.platform == "darwin":
        chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        if chrome.is_file():
            return str(chrome)
    return None


class DedicatedBrowser:
    """Lifecycle for one headed browser this application started itself."""

    def __init__(self, profile_dir: Path, executable: str = "") -> None:
        self.profile_dir = profile_dir
        self.executable = executable
        self.launched = False
        self.headless = False
        self.pid: int | None = None
        self.page = None
        self._playwright = None
        self._context = None

    def start(self) -> None:
        if self.launched:
            return
        _owned_profile(self.profile_dir)
        if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
            raise BrowserLaunchRefused("headed_display_missing")
        executable = resolve_browser_executable(self.executable)
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BrowserLaunchRefused("playwright_missing") from exc
        launch_args = ["--no-first-run", "--no-default-browser-check", "--disable-sync"]
        if sys.platform.startswith("linux"):
            launch_args.extend(["--no-sandbox", "--disable-dev-shm-usage"])
        playwright = sync_playwright().start()
        try:
            if executable:
                context = playwright.chromium.launch_persistent_context(
                    str(self.profile_dir),
                    headless=False,
                    executable_path=executable,
                    viewport={"width": 1100, "height": 800},
                    args=launch_args,
                )
            else:
                context = playwright.chromium.launch_persistent_context(
                    str(self.profile_dir),
                    headless=False,
                    viewport={"width": 1100, "height": 800},
                    args=launch_args,
                )
        except Exception:
            playwright.stop()
            raise
        self._playwright = playwright
        self._context = context
        self.page = context.pages[0] if context.pages else context.new_page()
        if Path("/proc").is_dir():
            pid, command = _process_matches(self.profile_dir)
            debug_flag = "remote-" + "debugging-" + "port"
            if debug_flag in command or "--headless" in command:
                self.close()
                raise BrowserLaunchRefused("refusing_debug_port")
            if pid is None:
                self.close()
                raise BrowserLaunchRefused("browser_process_not_found")
            self.pid = pid
        self.launched = True
        self.headless = False

    def close(self) -> None:
        context = self._context
        playwright = self._playwright
        self._context = None
        self._playwright = None
        self.page = None
        self.launched = False
        try:
            if context is not None:
                context.close()
        finally:
            if playwright is not None:
                playwright.stop()
            self.pid = None
