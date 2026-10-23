"""CLI entrypoint to run the On-Call Agent end-to-end:
   python -m agent --backend mock
   python -m agent --backend live --alert "ALERT: Checkout 500 error rate > 50%"
   python -m agent --backend live --dry-run
   python -m agent --kill
"""
from __future__ import annotations
import argparse
import json

from .agent import OnCallAgent
from .backends import make_backend
from .llm import LLMClient
from .remediation import ActionExecutor, clear_kill_switch, engage_kill_switch


def main():
    parser = argparse.ArgumentParser(description="Incident Zero: AI On-Call Engineer")
    parser.add_argument("--backend", choices=["live", "mock"], default="mock", help="Telemetry backend to query")
    parser.add_argument("--scenario", default="payment_failure", help="Mock scenario name")
    parser.add_argument("--alert", default="", help="Alert message that triggered the investigation")
    parser.add_argument("--dry-run", action="store_true", help="Run in dry-run mode without mutating system")
    parser.add_argument("--auto-approve", action="store_true", help="Skip interactive prompt (for automated eval)")
    parser.add_argument("--kill", action="store_true", help="Engage the emergency kill switch immediately")
    parser.add_argument("--reset-kill", action="store_true", help="Clear the emergency kill switch")
    parser.add_argument("--output", default="", help="Optional path to save JSON incident report")
    args = parser.parse_args()

    if args.kill:
        engage_kill_switch("Triggered via CLI --kill flag")
        print("🚨 KILL SWITCH ENGAGED (.killswitch created). All agent loops and actions are now blocked.")
        return

    if args.reset_kill:
        clear_kill_switch()
        print("✅ Kill switch cleared.")
        return

    backend = make_backend(args.backend, args.scenario)
    alert_text = args.alert or backend.default_alert

    llm = LLMClient()
    agent = OnCallAgent(llm=llm, backend=backend)

    print(f"🔍 Investigating alert ({args.backend} mode): {alert_text}")
    result = agent.investigate(alert_text)

    executor = ActionExecutor(backend=backend, dry_run=args.dry_run)
    final_report = executor.execute_with_approval(
        result,
        auto_approve=args.auto_approve,
        verify_wait_seconds=0 if args.backend == "mock" else 12,
    )

    formatted = json.dumps(final_report, indent=2)
    print("\n📄 FINAL INCIDENT REPORT (JSON):")
    print(formatted)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(formatted)
        print(f"\nSaved report to {args.output}")


if __name__ == "__main__":
    main()
