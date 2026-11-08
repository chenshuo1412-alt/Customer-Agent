"""Status report. A stopped process is never described as running."""

from __future__ import annotations

from typing import Any

from inbox_collector.config import AppConfig, readiness_problems
from inbox_collector.mapping import mapping_validated
from inbox_collector.providers import WebhookProvider
from inbox_collector.store import Store


def build_health(
    store: Store,
    config: AppConfig | None,
    mapping: dict[str, Any] | None,
    *,
    process_alive: bool,
    provider: object | None = None,
) -> dict[str, Any]:
    state, reason = store.state_reason()
    problems = [] if config is None else readiness_problems(config, mapping)
    if config is not None and config.mode != "fixture":
        problems.append("real_mode_refused")
    if problems and state == "running":
        state = "stopped"
        reason = "config_incomplete"
    if state == "running" and not process_alive:
        state = "stopped"
        reason = "process_not_alive"
    webhook_allowed = isinstance(provider, WebhookProvider) and provider.network_allowed()
    return {
        "state": state,
        "state_reason": reason,
        "readiness_problems": problems,
        "mode": None if config is None else config.mode,
        "mapping_validated": bool(mapping) and mapping_validated(mapping),
        "source_kind": store.get_meta("last_source_kind"),
        "real_store_connected": False,
        "browser_launched": False,
        "claims_user_wakeup": False,
        "alert_sink_enabled": bool(config and config.alert_sink_enabled),
        "alert_sink_delivered": False,
        "webhook_enabled": bool(config and config.webhook_enabled),
        "webhook_network_allowed": webhook_allowed,
        "last_success_collect_at": store.get_meta("last_success_collect_at"),
        "last_inbound_at": store.get_meta("last_inbound_at"),
        "baseline_done": store.baseline_done(),
        "messages": store.count_messages(),
        "inbounds": store.count_inbounds(),
        "pending_generation": store.count_drafts("pending_generation"),
        "drafts_ready_for_review": store.count_drafts("ready_for_review"),
        "drafts_invalidated": store.count_drafts("invalidated"),
        "drafts_needs_human_review": store.count_drafts("needs_human_review"),
        "drafts_skipped_already_replied": store.count_drafts("skipped_already_replied"),
        "webhook_awaiting_receipt": store.count_drafts("webhook_transport_accepted_awaiting_receipt"),
        "webhook_transport_failed": store.count_drafts("webhook_transport_failed"),
        "open_alerts": store.open_alert_count(),
        "sample_interval_seconds": None if config is None else config.sample_interval_seconds,
        "interval_is_bot_routine": False,
        "sent_to_buyer": False,
    }
