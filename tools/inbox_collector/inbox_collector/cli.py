"""Command line. The default commands do not loop and do not open a shop."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Sequence

from inbox_collector.browser_plan import BrowserLaunchRefused, assert_isolated_profile, browser_plan
from inbox_collector.config import AppConfig, load_config, load_mapping_for, readiness_problems
from inbox_collector.demo_data import BASELINE_SNAPSHOT, NEW_SNAPSHOT
from inbox_collector.health import build_health
from inbox_collector.login_prep import run_login_prep
from inbox_collector.mapping import local_fixture_problems, mapping_problems
from inbox_collector.models import snapshot_from_dict
from inbox_collector.pipeline import Pipeline
from inbox_collector.providers import MockProvider, WebhookProvider, provider_for
from inbox_collector.runner import Runner
from inbox_collector.sources import FixtureSource
from inbox_collector.store import Store


def _print(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _mapping_and_runner(config: AppConfig, *, transport: object | None = None) -> Runner:
    mapping, map_errors = load_mapping_for(config)
    if config.mode == "fixture" and map_errors:
        mapping = None
    webhook = None
    if config.draft_provider == "webhook":
        webhook = WebhookProvider(
            config.webhook_url,
            config.webhook_secret_env,
            allow_network=config.webhook_allow_network,
            transport=transport if callable(transport) else None,
        )
    if config.draft_provider == "routine_bearer":
        from inbox_collector.keychain import MacKeychainStore, MemorySecretStore
        from inbox_collector.routine import RoutineBearerProvider

        secret_store = MacKeychainStore() if MacKeychainStore.available() else MemorySecretStore()
        provider = RoutineBearerProvider(
            config.webhook_url,
            secret_store,
            allow_network=config.webhook_allow_network,
            transport=transport if callable(transport) else None,
        )
    else:
        provider = provider_for(config.draft_provider, webhook=webhook)
    source = None
    problems = readiness_problems(config, mapping)
    if config.mode == "fixture" and not problems and config.fixture_path is not None:
        source = FixtureSource(config.fixture_path)
    if config.mode == "browser_local" and not problems and mapping is not None:
        from inbox_collector.browser_session import DedicatedBrowser
        from inbox_collector.dom_source import BrowserDomSource

        source = BrowserDomSource(
            DedicatedBrowser(config.data_dir / "own-browser-profile"),
            config.entry_url,
            mapping,
            timezone_name=config.timezone,
            login_wait_seconds=config.login_wait_seconds,
        )
    return Runner(config, mapping, source, provider)


def cmd_check(config_path: Path) -> int:
    config, problems = load_config(config_path)
    if config is None:
        _print({"ok": False, "problems": problems, "real_store_connected": False, "state": "stopped"})
        return 2
    mapping, _errors = load_mapping_for(config)
    ready = readiness_problems(config, mapping)
    if config.mode == "real":
        ready.append("real_mode_refused")
    _print(
        {
            "ok": not ready,
            "problems": ready,
            "state": "stopped",
            "real_store_connected": False,
            "mapping_validated": bool(mapping) and mapping_problems(mapping) == [],
            "local_fixture_mapping_ok": bool(mapping) and local_fixture_problems(mapping) == [],
            "live_dom_verified": False,
            "bot_integration": "blocked_not_connected",
            "production_running": False,
            "merchant_collection_enabled": False,
            "sample_interval_seconds": config.sample_interval_seconds,
            "interval_is_bot_routine": False,
        }
    )
    return 0 if not ready else 2


def cmd_validate_mapping(path: Path) -> int:
    try:
        from inbox_collector.mapping import load_mapping

        mapping = load_mapping(path)
    except (OSError, json.JSONDecodeError, ValueError):
        _print({"validated": False, "problems": ["mapping_unreadable"], "real_store_connected": False})
        return 2
    merchant_ok = mapping_problems(mapping) == []
    local_ok = local_fixture_problems(mapping) == []
    _print(
        {
            "mapping_id": mapping.get("mapping_id"),
            "merchant_mapping_validated": merchant_ok,
            "local_fixture_mapping_ok": local_ok,
            "live_dom_verified": False,
            "validated": merchant_ok,
            "problems": [] if local_ok or merchant_ok else mapping_problems(mapping),
            "real_store_connected": False,
            "fixture_selectors_applied_to_merchant_site": False,
        }
    )
    return 0 if local_ok or merchant_ok else 2


def _diagnose_summary(payload: dict[str, Any]) -> dict[str, Any]:
    preflight = payload.get("message_preflight") if isinstance(payload.get("message_preflight"), dict) else {}
    frames = payload.get("frames") if isinstance(payload.get("frames"), dict) else {}
    coverage = payload.get("coverage") if isinstance(payload.get("coverage"), dict) else {}
    kept = {
        key: preflight.get(key)
        for key in (
            "visible_rows",
            "hidden_rows",
            "merchant_message",
            "buyer_item",
            "direction_evidence",
            "time_evidence",
            "time_shapes",
            "rows_with_adjacent_date_header",
            "read_mark_rows",
            "reply_evidence",
            "same_text_different_id_rows",
            "id_stability",
            "text_merge_applied",
            "can_treat_time_as_message_time",
            "production_mapping_validated",
        )
    }
    return {
        "status": payload.get("status"),
        "reason": payload.get("reason"),
        "page_acceptance": payload.get("page_acceptance"),
        "message_preflight": kept,
        "frames": {
            "seen": frames.get("seen"),
            "entered": frames.get("entered"),
            "skipped": frames.get("skipped"),
            "by_category": frames.get("by_category"),
        },
        "coverage": {
            "incomplete": coverage.get("incomplete"),
            "reasons": coverage.get("reasons"),
            "top_page": coverage.get("top_page"),
            "max_depth_reached": coverage.get("max_depth_reached"),
        },
        "production_mapping_validated": False,
        "production_send_allowed": False,
        "phrase_probe": payload.get("phrase_probe"),
        "composer_report": payload.get("composer_report"),
        "nodes_omitted": True,
    }


def cmd_diagnose_dom(
    data_dir: Path,
    timeout_seconds: float,
    *,
    summary: bool = False,
    probe_phrase: str = "",
    op: str = "probe",
    keep_local_detail: bool = False,
    candidate_reply: str = "",
) -> int:
    """Ask the already-running login-prep process. Never starts a browser."""
    from inbox_collector.dom_diagnose import wait_for_diagnose_result

    if candidate_reply and op != "input-state-readonly":
        _print(
            {
                "status": "candidate_reply_not_a_send",
                "production_send_allowed": False,
                "send_authorized": False,
                "authorization_used": False,
            }
        )
        return 2
    if op == "input-state-readonly" and not candidate_reply:
        _print(
            {
                "status": "candidate_reply_required",
                "production_send_allowed": False,
                "send_authorized": False,
                "authorization_used": False,
            }
        )
        return 2
    if timeout_seconds <= 0:
        _print({"status": "diagnostic_error", "reason": "timeout_invalid", "platform_unsupported": False, "nodes": None})
        return 2
    data_dir.mkdir(parents=True, exist_ok=True)
    payload = wait_for_diagnose_result(
        data_dir,
        timeout_seconds=timeout_seconds,
        probe_phrase=probe_phrase,
        op=op,
        keep_local_detail=keep_local_detail,
        candidate_reply=candidate_reply,
    )
    if (
        summary
        and isinstance(payload, dict)
        and payload.get("handler") not in {"geometry-readonly", "request-router", "input-state-readonly", "confirmed-text"}
        and payload.get("status") not in {"diagnostic_error", "op_rejected", "field_rejected", "request_rejected"}
    ):
        payload = _diagnose_summary(payload)
    _print(payload)
    return diagnose_exit_code(op, payload)


def diagnose_exit_code(op: str, payload: dict[str, Any] | None) -> int:
    """Rejected operations exit 2. A completed read is not a send."""
    if not isinstance(payload, dict):
        return 2
    status = payload.get("status")
    if payload.get("handler") == "request-router" or status in {None, "diagnostic_error", "op_rejected", "field_rejected", "request_rejected", "phrase_leaked"}:
        return 2
    if op == "geometry-readonly":
        return 0 if status in {"geometry_ready", "structure_ready"} else 2
    if op == "input-state-readonly":
        return 0 if status == "input_state_ready" else 2
    if op == "confirmed-text":
        return 0 if status == "page_visible" and payload.get("delivery_claimed") is False and payload.get("production_send_allowed") is False else 2
    return 0


def cmd_routine_setup(url: str, config_path: Path) -> int:
    """Prompt for a key only after Keychain is available. There is no --key argument."""
    from inbox_collector.keychain import MacKeychainStore

    if not MacKeychainStore.available():
        _print(
            {
                "ok": False,
                "problems": ["keychain_unavailable"],
                "production_running": False,
                "real_store_connected": False,
            }
        )
        return 2
    import getpass

    secret = getpass.getpass("Routine sender key: ")
    from inbox_collector.routine import save_routine_setup

    result = save_routine_setup(url=url, secret=secret, store=MacKeychainStore(), config_path=str(config_path))
    result.pop("secret", None)
    _print(result)
    return 0 if result.get("ok") else 2


def cmd_login_prep(config_path: Path) -> int:
    config, problems = load_config(config_path)
    if config is None:
        _print({"ok": False, "problems": problems, "state": "stopped", "production_running": False})
        return 2
    mapping, _errors = load_mapping_for(config)
    report = run_login_prep(config, mapping)
    _print(report)
    if report.get("readiness_problems"):
        return 2
    if report.get("state") == "paused":
        return 3
    return 0


def cmd_run(config_path: Path, *, once: bool, cycles: int) -> int:
    config, problems = load_config(config_path)
    if config is None:
        _print({"ok": False, "problems": problems, "state": "stopped", "real_store_connected": False})
        return 2
    if config.mode == "login_prep":
        _print(
            {
                "ok": False,
                "problems": ["use_login_prep_command"],
                "state": "stopped",
                "production_running": False,
                "merchant_collection_enabled": False,
                "real_store_connected": False,
            }
        )
        return 2
    runner = _mapping_and_runner(config)
    try:
        if once:
            report = runner.run_once()
        else:
            report = runner.run_foreground(cycles, sleep_fn=time.sleep)
    finally:
        runner.close()
    _print(report)
    return _exit_for(report)


def cmd_health(data_dir: Path, config_path: Path | None) -> int:
    db_path = data_dir / "collector.sqlite"
    if not db_path.exists():
        _print(
            {
                "state": "stopped",
                "state_reason": "never_started",
                "real_store_connected": False,
                "browser_launched": False,
                "claims_user_wakeup": False,
                "mapping_validated": False,
                "sent_to_buyer": False,
            }
        )
        return 0
    config = None
    mapping = None
    if config_path is not None:
        config, _problems = load_config(config_path)
        if config is not None:
            mapping, _errors = load_mapping_for(config)
    store = Store(data_dir)
    try:
        _print(build_health(store, config, mapping, process_alive=False))
    finally:
        store.close()
    return 0


def cmd_resume(data_dir: Path, *, acknowledge_hard_stop: bool) -> int:
    if not (data_dir / "collector.sqlite").exists():
        _print({"ok": False, "problems": ["never_started"], "state": "stopped"})
        return 2
    store = Store(data_dir)
    try:
        state, reason = store.state_reason()
        if reason == "failure_ceiling" and not acknowledge_hard_stop:
            _print({"ok": False, "state": state, "state_reason": reason})
            return 4
        if state == "paused" or reason == "failure_ceiling":
            with store.immediate():
                store.clear_alerts()
                if acknowledge_hard_stop:
                    store.reset_failure()
                store.set_state("stopped", "resumed_idle")
        report_state, report_reason = store.state_reason()
        _print({"ok": True, "state": report_state, "state_reason": report_reason, "real_store_connected": False})
        return 0
    finally:
        store.close()


def cmd_demo(data_dir: Path) -> int:
    try:
        assert_isolated_profile(data_dir)
    except BrowserLaunchRefused as exc:
        _print({"ok": False, "problems": [exc.code], "real_store_connected": False})
        return 2
    if data_dir.exists() and any(data_dir.iterdir()):
        _print({"ok": False, "problems": ["demo_data_dir_not_empty"], "real_store_connected": False})
        return 2
    mapping_path = Path(__file__).resolve().parents[1] / "mappings" / "pinduoduo_chat_merchant.json"
    from inbox_collector.mapping import load_mapping

    mapping = load_mapping(mapping_path)
    store = Store(data_dir)
    try:
        pipeline = Pipeline(store, MockProvider(), retention_days=30)
        pipeline.observe(snapshot_from_dict(BASELINE_SNAPSHOT), failure_ceiling=3)
        pipeline.observe(snapshot_from_dict(NEW_SNAPSHOT), failure_ceiling=3)
        pipeline.generate()
        store.set_state("stopped", "foreground_run_finished")
        report = build_health(store, None, mapping, process_alive=False, provider=MockProvider())
        spool = sorted((data_dir / "spool").glob("*.json"))
        report["demo"] = True
        report["spool_files"] = [path.name for path in spool]
        report["browser_plan_launch"] = browser_plan_launch_refused()
        _print(report)
        return 0 if report["drafts_ready_for_review"] == 1 and report["real_store_connected"] is False else 2
    finally:
        store.close()


def browser_plan_launch_refused() -> bool:
    from inbox_collector.browser_plan import launch_independent_browser

    try:
        launch_independent_browser()
    except BrowserLaunchRefused:
        return True
    return False


def _exit_for(report: dict[str, Any]) -> int:
    if report.get("readiness_problems") or report.get("state_reason") == "config_incomplete":
        return 2
    if report.get("state") == "paused":
        return 3
    if report.get("state_reason") == "failure_ceiling":
        return 4
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="inbox-collector")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check-config")
    check.add_argument("--config", type=Path, required=True)

    validate = sub.add_parser("validate-mapping")
    validate.add_argument("--mapping", type=Path, required=True)

    run = sub.add_parser("run")
    run.add_argument("--config", type=Path, required=True)
    run.add_argument("--once", action="store_true")
    run.add_argument("--foreground", action="store_true")
    run.add_argument("--cycles", type=int, default=1)

    health = sub.add_parser("health")
    health.add_argument("--data-dir", type=Path, required=True)
    health.add_argument("--config", type=Path)

    resume = sub.add_parser("resume")
    resume.add_argument("--data-dir", type=Path, required=True)
    resume.add_argument("--acknowledge-hard-stop", action="store_true")

    demo = sub.add_parser("demo")
    demo.add_argument("--data-dir", type=Path, required=True)

    login_prep = sub.add_parser("login-prep")
    login_prep.add_argument("--config", type=Path, required=True)

    plan = sub.add_parser("browser-plan")
    plan.add_argument("--config", type=Path, required=True)

    diagnose = sub.add_parser("diagnose-dom")
    diagnose.add_argument("--data-dir", type=Path, required=True)
    diagnose.add_argument("--timeout-seconds", type=float, default=2.0)
    diagnose.add_argument("--summary", action="store_true")
    diagnose.add_argument("--probe-phrase", default="")
    diagnose.add_argument("--op", default="probe")
    diagnose.add_argument("--keep-local-detail", action="store_true")
    diagnose.add_argument("--candidate-reply", default="")

    test_send = sub.add_parser("test-send")
    test_send.add_argument("--data-dir", type=Path, required=True)
    test_send.add_argument("--probe-phrase", required=True)
    test_send.add_argument("--opt-in", required=True)
    test_send.add_argument("--timeout-seconds", type=float, default=60)

    confirmed = sub.add_parser("confirmed-text-send")
    confirmed.add_argument("--data-dir", type=Path, required=True)
    confirmed.add_argument("--challenge", required=True)
    confirmed.add_argument("--reply", required=True)
    confirmed.add_argument("--buyer-source", required=True)
    confirmed.add_argument("--expires-seconds", type=int, required=True)
    confirmed.add_argument("--opt-in", required=True)
    confirmed.add_argument("--timeout-seconds", type=float, default=60)

    routine_setup = sub.add_parser("routine-setup")
    routine_setup.add_argument("--url", required=True)
    routine_setup.add_argument("--config", type=Path, required=True)

    gates = sub.add_parser("auto-reply-gates")
    gates.add_argument("--config", type=Path, required=True)

    sub.add_parser("auto-reply-sim")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    if args.command == "check-config":
        return cmd_check(args.config)
    if args.command == "validate-mapping":
        return cmd_validate_mapping(args.mapping)
    if args.command == "run":
        if args.once == args.foreground:
            _print(
                {
                    "ok": False,
                    "problems": ["choose_exactly_one_of_once_or_foreground"],
                    "state": "stopped",
                    "real_store_connected": False,
                }
            )
            return 2
        if args.foreground and not 1 <= args.cycles <= 10:
            _print({"ok": False, "problems": ["cycles_out_of_range"], "state": "stopped"})
            return 2
        return cmd_run(args.config, once=args.once, cycles=args.cycles)
    if args.command == "health":
        return cmd_health(args.data_dir, args.config)
    if args.command == "resume":
        return cmd_resume(args.data_dir, acknowledge_hard_stop=args.acknowledge_hard_stop)
    if args.command == "demo":
        return cmd_demo(args.data_dir)
    if args.command == "login-prep":
        return cmd_login_prep(args.config)
    if args.command == "diagnose-dom":
        return cmd_diagnose_dom(
            args.data_dir,
            args.timeout_seconds,
            summary=args.summary,
            probe_phrase=args.probe_phrase,
            op=args.op,
            keep_local_detail=args.keep_local_detail,
            candidate_reply=args.candidate_reply,
        )
    if args.command == "test-send":
        from inbox_collector.dom_diagnose import wait_for_diagnose_result
        from inbox_collector.test_send import write_opt_in

        if args.opt_in != "single-test-conversation":
            _print({"status": "opt_in_refused", "production_send_allowed": False, "storewide_auto_reply": False})
            return 2
        if args.timeout_seconds <= 0:
            _print({"status": "timeout_invalid", "production_send_allowed": False})
            return 2
        write_opt_in(args.data_dir, args.probe_phrase)
        report = wait_for_diagnose_result(
            args.data_dir,
            timeout_seconds=args.timeout_seconds,
            probe_phrase=args.probe_phrase,
            op="test-send",
        )
        if args.probe_phrase and args.probe_phrase in json.dumps(report, ensure_ascii=False):
            report = {"status": "phrase_leaked", "production_send_allowed": False, "storewide_auto_reply": False}
        _print(report)
        return 0 if report.get("status") == "test_sent" and report.get("production_send_allowed") is False else 2
    if args.command == "confirmed-text-send":
        from inbox_collector.confirmed_text import (
            confirmed_send_block,
            read_window_id,
            reply_problem,
            write_authorization,
        )
        from inbox_collector.dom_diagnose import wait_for_diagnose_result

        refused = {
            "production_send_allowed": False,
            "storewide_auto_reply": False,
            "delivery_claimed": False,
            "retry_allowed": False,
        }
        if args.opt_in != "single-confirmed-text" or args.buyer_source != "own-account":
            _print({"status": "authorization_refused", **refused})
            return 2
        if reply_problem(args.reply) or not args.challenge.strip():
            _print({"status": "reply_rejected", **refused})
            return 2
        if not 30 <= args.expires_seconds <= 900 or args.timeout_seconds <= 0:
            _print({"status": "authorization_expired", **refused})
            return 2
        blocked = confirmed_send_block(args.data_dir, args.challenge, args.reply)
        if blocked:
            _print({"status": blocked, "ledger_sealed": True, **refused})
            return 2
        window_id = read_window_id(args.data_dir)
        if window_id is None:
            _print({"status": "window_unbound", **refused})
            return 2
        write_authorization(
            args.data_dir,
            challenge=args.challenge,
            reply=args.reply,
            window_id=window_id,
            expires_at=time.time() + args.expires_seconds,
        )
        report = wait_for_diagnose_result(
            args.data_dir,
            timeout_seconds=args.timeout_seconds,
            probe_phrase=args.challenge,
            op="confirmed-text",
        )
        blob = json.dumps(report, ensure_ascii=False)
        if args.challenge in blob or args.reply in blob:
            report = {"status": "phrase_leaked", **refused}
        _print(report)
        return 0 if report.get("status") == "page_visible" and report.get("delivery_claimed") is False and report.get("production_send_allowed") is False else 2
    if args.command == "auto-reply-sim":
        from inbox_collector.auto_reply import run_local_ollama_sim

        report = run_local_ollama_sim()
        _print(report)
        return 0 if report.get("status") == "simulated_sent" and report.get("real_buyer_send") is False else 2
    if args.command == "routine-setup":
        return cmd_routine_setup(args.url, args.config)
    if args.command == "auto-reply-gates":
        from inbox_collector.auto_reply import gate_report

        try:
            raw = json.loads(args.config.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
        report = gate_report(raw if isinstance(raw, dict) else {})
        report["ok"] = False
        _print(report)
        return 2
    if args.command == "browser-plan":
        config, problems = load_config(args.config)
        if config is None:
            _print({"launch": False, "problems": problems, "verified": False})
            return 2
        mapping, _errors = load_mapping_for(config)
        try:
            plan = browser_plan(config, mapping)
        except BrowserLaunchRefused as exc:
            _print({"launch": False, "verified": False, "problems": [exc.code]})
            return 2
        _print(plan)
        return 0 if plan.get("launch") else 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
