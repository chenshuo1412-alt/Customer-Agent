"""Explicit local configuration. Secrets are environment names, never values."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from inbox_collector.mapping import (
    entry_allowed_by_mapping,
    load_mapping,
    local_entry_problems,
    local_fixture_problems,
    login_prep_entry_problems,
    mapping_problems,
)

_ALLOWED_SECRET_ENV_KEYS = {"webhook_secret_env", "alert_secret_env", "model_api_key_env"}
_FORBIDDEN_FRAGMENTS = ("password", "cookie", "token", "api_key", "authorization")


@dataclass(frozen=True)
class AppConfig:
    mode: str
    timezone: str
    sample_interval_seconds: int
    max_retries: int
    max_consecutive_failures: int
    open_conversation: bool
    allow_browser_launch: bool
    mapping_path: Path
    fixture_path: Path | None
    data_dir: Path
    draft_provider: str
    webhook_enabled: bool
    webhook_url: str
    webhook_secret_env: str
    webhook_allow_network: bool
    alert_sink_enabled: bool
    alert_sink_url: str
    model_enabled: bool
    retention_days: int
    entry_url: str
    login_wait_seconds: int
    browser_executable: str
    config_path: Path


def _reject_secret_keys(value: object, where: str, problems: list[str]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            name = str(key)
            if name not in _ALLOWED_SECRET_ENV_KEYS:
                lowered = name.lower()
                if lowered in {"secret", "webhook_secret"} or lowered.endswith("_secret"):
                    problems.append(f"inline_secret_forbidden:{where}{name}")
                if any(fragment in lowered for fragment in _FORBIDDEN_FRAGMENTS):
                    problems.append(f"forbidden_config_key:{where}{name}")
            _reject_secret_keys(child, f"{where}{name}.", problems)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_secret_keys(child, f"{where}{index}.", problems)


def _resolve(base: Path, value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def load_config(path: Path) -> tuple[AppConfig | None, list[str]]:
    problems: list[str] = []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, ["config_unreadable"]
    if not isinstance(raw, dict):
        return None, ["config_not_object"]
    _reject_secret_keys(raw, "", problems)
    if "://" in path.as_posix():
        problems.append("remote_config_refused")

    mode = str(raw.get("mode") or "").strip()
    if mode not in {"fixture", "real", "browser_local", "login_prep"}:
        problems.append("mode_invalid")
    timezone = str(raw.get("timezone") or "").strip()
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        problems.append("timezone_invalid")
    interval = raw.get("sample_interval_seconds", 120)
    if not isinstance(interval, int) or isinstance(interval, bool) or not 60 <= interval <= 120:
        problems.append("sample_interval_out_of_range")
        interval = 0
    retries = raw.get("max_retries", 2)
    if not isinstance(retries, int) or isinstance(retries, bool) or not 1 <= retries <= 5:
        problems.append("max_retries_invalid")
        retries = 0
    ceiling = raw.get("max_consecutive_failures", 3)
    if not isinstance(ceiling, int) or isinstance(ceiling, bool) or not 1 <= ceiling <= 10:
        problems.append("failure_ceiling_invalid")
        ceiling = 0
    retention = raw.get("retention_days", 30)
    if not isinstance(retention, int) or isinstance(retention, bool) or not 1 <= retention <= 365:
        problems.append("retention_days_invalid")
        retention = 0
    if raw.get("open_conversation") not in {True, False}:
        problems.append("open_conversation_invalid")
    if raw.get("allow_browser_launch") not in {True, False}:
        problems.append("allow_browser_launch_invalid")
    provider = str(raw.get("draft_provider") or "none").strip()
    if provider not in {"none", "mock", "webhook", "model"}:
        problems.append("draft_provider_invalid")
    login_wait = raw.get("login_wait_seconds", 180)
    if not isinstance(login_wait, int) or isinstance(login_wait, bool) or not 1 <= login_wait <= 600:
        problems.append("login_wait_invalid")
        login_wait = 0
    entry_url = str(raw.get("entry_url") or "").strip()
    browser_executable = raw.get("browser_executable", "")
    if not isinstance(browser_executable, str):
        problems.append("browser_executable_invalid")
        browser_executable = ""
    if mode == "browser_local":
        problems.extend(local_entry_problems(entry_url))
    elif mode == "real" and entry_url:
        problems.extend(local_entry_problems(entry_url))
    elif mode == "login_prep":
        problems.extend(login_prep_entry_problems(entry_url))

    base = path.parent
    mapping_value = raw.get("mapping_path")
    fixture_value = raw.get("fixture_path")
    data_value = raw.get("data_dir")
    if not isinstance(mapping_value, str) or not mapping_value:
        problems.append("mapping_path_missing")
        mapping_path = base / "missing-mapping.json"
    else:
        mapping_path = _resolve(base, mapping_value)
    if not isinstance(data_value, str) or not data_value:
        problems.append("data_dir_missing")
        data_dir = base / "data"
    else:
        data_dir = _resolve(base, data_value)
    fixture_path: Path | None = None
    if mode == "fixture":
        if not isinstance(fixture_value, str) or not fixture_value or "://" in fixture_value:
            problems.append("fixture_path_missing")
        else:
            fixture_path = _resolve(base, fixture_value)
            if not fixture_path.is_file():
                problems.append("fixture_missing")

    webhook = raw.get("webhook") if isinstance(raw.get("webhook"), dict) else {}
    alert = raw.get("alert_sink") if isinstance(raw.get("alert_sink"), dict) else {}
    model = raw.get("model") if isinstance(raw.get("model"), dict) else {}
    webhook_enabled = webhook.get("enabled") is True
    webhook_url = str(webhook.get("url") or "")
    webhook_secret_env = str(webhook.get("webhook_secret_env") or "")
    webhook_allow_network = webhook.get("allow_network") is True
    if webhook_enabled:
        if not webhook_url.startswith("https://"):
            problems.append("webhook_url_must_be_https")
        if not webhook_secret_env or not webhook_secret_env.isidentifier():
            problems.append("webhook_secret_env_invalid")
    if provider == "webhook" and not webhook_enabled:
        problems.append("webhook_provider_not_enabled")
    alert_enabled = alert.get("enabled") is True
    alert_url = str(alert.get("url") or "")
    if alert_enabled and not alert_url.startswith("https://"):
        problems.append("alert_sink_url_must_be_https")
    model_enabled = model.get("enabled") is True
    if provider == "model" and not model_enabled:
        problems.append("model_provider_not_enabled")
    if model_enabled:
        model_url = str(model.get("base_url") or "")
        model_env = str(model.get("model_api_key_env") or "")
        if not model_url.startswith("https://"):
            problems.append("model_url_must_be_https")
        if not model_env or not model_env.isidentifier():
            problems.append("model_api_key_env_invalid")

    if problems:
        return None, problems
    return (
        AppConfig(
            mode=mode,
            timezone=timezone,
            sample_interval_seconds=int(interval),
            max_retries=int(retries),
            max_consecutive_failures=int(ceiling),
            open_conversation=bool(raw.get("open_conversation")),
            allow_browser_launch=bool(raw.get("allow_browser_launch")),
            mapping_path=mapping_path,
            fixture_path=fixture_path,
            data_dir=data_dir,
            draft_provider=provider,
            webhook_enabled=webhook_enabled,
            webhook_url=webhook_url,
            webhook_secret_env=webhook_secret_env,
            webhook_allow_network=webhook_allow_network,
            alert_sink_enabled=alert_enabled,
            alert_sink_url=alert_url,
            model_enabled=model_enabled,
            retention_days=int(retention),
            entry_url=entry_url,
            login_wait_seconds=int(login_wait),
            browser_executable=browser_executable,
            config_path=path.resolve(),
        ),
        [],
    )


def readiness_problems(config: AppConfig, mapping: dict[str, Any] | None) -> list[str]:
    problems: list[str] = []
    if config.mode == "real":
        problems.extend(mapping_problems(mapping, open_conversation=config.open_conversation))
        problems.append("real_store_not_authorized")
        problems.append("live_browser_blocked_until_dom_verified")
    elif config.mode == "browser_local":
        if config.open_conversation:
            problems.append("open_conversation_not_permitted")
        problems.extend(local_fixture_problems(mapping))
        problems.extend(entry_allowed_by_mapping(config.entry_url, mapping))
    elif config.mode == "login_prep":
        if config.open_conversation:
            problems.append("open_conversation_not_permitted")
        problems.extend(login_prep_entry_problems(config.entry_url))
        if config.draft_provider != "none":
            problems.append("login_prep_provider_must_be_none")
        if config.webhook_enabled or config.webhook_allow_network:
            problems.append("login_prep_webhook_forbidden")
        if config.alert_sink_enabled:
            problems.append("login_prep_alert_sink_forbidden")
        if config.model_enabled:
            problems.append("login_prep_model_forbidden")
    elif config.mode != "fixture":
        problems.append("mode_invalid")
    if config.mode == "fixture" and (config.fixture_path is None or not config.fixture_path.is_file()):
        problems.append("fixture_missing")
    if config.draft_provider == "webhook" and config.webhook_enabled:
        import os

        if not os.environ.get(config.webhook_secret_env):
            problems.append("webhook_secret_missing")
    return problems


def load_mapping_for(config: AppConfig) -> tuple[dict[str, Any] | None, list[str]]:
    if not config.mapping_path.is_file():
        return None, ["mapping_file_missing"]
    try:
        return load_mapping(config.mapping_path), []
    except (OSError, json.JSONDecodeError, ValueError):
        return None, ["mapping_unreadable"]
