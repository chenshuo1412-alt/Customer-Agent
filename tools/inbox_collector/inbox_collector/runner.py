"""Foreground collector. It does not install itself as a service."""

from __future__ import annotations

import os
from typing import Any, Callable

from inbox_collector.config import AppConfig, readiness_problems
from inbox_collector.health import build_health
from inbox_collector.models import SnapshotRejected, snapshot_from_dict
from inbox_collector.pipeline import Pipeline
from inbox_collector.redact import configure_logging, log_event
from inbox_collector.sources import CollectFailure
from inbox_collector.store import Store


class Runner:
    def __init__(
        self,
        config: AppConfig,
        mapping: dict[str, Any] | None,
        source: object | None,
        provider: object,
        store: Store | None = None,
    ) -> None:
        self.config = config
        self.mapping = mapping
        self.source = source
        self.provider = provider
        self.store = store or Store(config.data_dir)
        secrets: list[str] = []
        if config.webhook_secret_env:
            secret = os.environ.get(config.webhook_secret_env)
            if secret:
                secrets.append(secret)
        configure_logging(self.store.data_dir / "collector.log", secrets)
        self.pipeline = Pipeline(
            self.store,
            provider,
            retention_days=config.retention_days,
        )
        if getattr(self.source, "on_launched", None) is None and hasattr(self.source, "browser"):
            self.source.on_launched = self._remember_browser

    def health(self, process_alive: bool) -> dict[str, Any]:
        return build_health(
            self.store,
            self.config,
            self.mapping,
            process_alive=process_alive,
            provider=self.provider,
        )

    def run_once(self) -> dict[str, Any]:
        problems = readiness_problems(self.config, self.mapping)
        if self.config.mode == "real":
            problems.append("real_mode_refused")
        elif self.config.mode not in {"fixture", "browser_local"}:
            problems.append("mode_invalid")
        if problems:
            self.store.set_state("stopped", "config_incomplete")
            self.store.set_meta("readiness_problems", ",".join(dict.fromkeys(problems)))
            log_event("not_running", alert="config_incomplete")
            return self.health(False)
        state, reason = self.store.state_reason()
        if state == "paused" or reason == "failure_ceiling":
            log_event("not_running", state=state, alert=reason)
            return self.health(False)
        if self.source is None:
            self._fail("collect_failed")
            return self.health(False)
        self.store.set_state("running", "collecting")
        raw: object | None = None
        failure: str | None = None
        for _attempt in range(self.config.max_retries):
            try:
                raw = self.source.collect()
                failure = None
                break
            except CollectFailure as exc:
                failure = exc.code
            except Exception:
                failure = "collect_failed"
        if failure is not None or raw is None:
            self._fail(failure or "collect_failed")
            return self.health(False)
        try:
            snapshot = snapshot_from_dict(raw)
        except SnapshotRejected:
            self._fail("parse_ambiguous")
            return self.health(False)
        outcome = self.pipeline.observe(
            snapshot,
            failure_ceiling=self.config.max_consecutive_failures,
        )
        if outcome in {"paused", "stopped"}:
            log_event("not_running", alert=self.store.state_reason()[1])
            return self.health(False)
        self.pipeline.generate()
        self.store.set_state("stopped", "foreground_run_finished")
        log_event("collect_finished", state="stopped", count=self.store.count_inbounds())
        return self.health(True)

    def run_foreground(self, cycles: int, sleep_fn: Callable[[int], None]) -> dict[str, Any]:
        if cycles < 1:
            raise ValueError("cycles")
        report: dict[str, Any] = {}
        for index in range(cycles):
            report = self.run_once()
            state = self.store.state_reason()
            if state[0] == "paused" or state[1] in {"failure_ceiling", "config_incomplete"}:
                return report
            if index < cycles - 1:
                sleep_fn(self.config.sample_interval_seconds)
        return report

    def resume(self, *, acknowledge_hard_stop: bool) -> int:
        _state, reason = self.store.state_reason()
        if reason == "failure_ceiling" and not acknowledge_hard_stop:
            return 4
        if self.store.state_reason()[0] == "paused" or reason == "failure_ceiling":
            with self.store.immediate():
                self.store.clear_alerts()
                if acknowledge_hard_stop:
                    self.store.reset_failure()
                self.store.set_state("stopped", "resumed_idle")
        return 0

    def _fail(self, code: str) -> None:
        with self.store.immediate():
            streak = self.store.increment_failure()
            self.store.open_alert(code)
            if streak >= self.config.max_consecutive_failures:
                self.store.set_state("stopped", "failure_ceiling")
            else:
                self.store.set_state("paused", code)
        log_event("alert", alert=code, count=streak)

    def close(self) -> None:
        source = self.source
        closer = getattr(source, "close", None)
        if closer is not None:
            closer()

    def _remember_browser(self, browser: object) -> None:
        self.store.set_meta("browser_launched", "1")
        self.store.set_meta("browser_headless", "0")
        self.store.set_meta("browser_pid", str(getattr(browser, "pid", "") or ""))
        self.store.set_meta("real_store_login_verified", "0")
        self.store.set_meta("bot_reply_received", "0")
