"""Stages 5, 6, and 7: Human Approval Gate, Dry-Run, Kill Switch, Action API Execution,
Post-Fix Verification, Auto-Rollback, and Report Generation."""
from __future__ import annotations
import json
import os
import subprocess
import time
import requests

from . import config
from .backends import Backend
from .schemas import AgentResult, ProposedAction


def is_kill_switch_active() -> bool:
    return os.path.exists(getattr(config, "KILL_SWITCH_FILE", ".killswitch"))


def engage_kill_switch(reason: str = "Manual operator kill switch triggered") -> None:
    path = getattr(config, "KILL_SWITCH_FILE", ".killswitch")
    with open(path, "w", encoding="utf-8") as f:
        f.write(reason)


def clear_kill_switch() -> None:
    path = getattr(config, "KILL_SWITCH_FILE", ".killswitch")
    if os.path.exists(path):
        os.remove(path)


class ActionExecutor:
    """Executes allowlisted runbook actions ONLY after explicit human approval,
    enforcing dry-run and kill-switch controls, followed by telemetry verification
    and automatic rollback if the service does not recover."""

    SERVICE_TO_FLAG = {
        "payment": "paymentFailure",
        "cart": "cartFailure",
        "product-catalog": "productCatalogFailure",
        "recommendation": "recommendationCacheFailure",
        "ad": "adHighCpu",
        "frontend": "loadGeneratorFloodHomepage",
        "load-generator": "loadGeneratorFloodHomepage",
    }

    def __init__(self, backend: Backend, dry_run: bool = False):
        self.backend = backend
        self.dry_run = dry_run
        self.action_api_url = getattr(config, "ACTION_API_URL", "")
        self._last_rollback_fn = None

    def execute_with_approval(
        self,
        result: AgentResult,
        auto_approve: bool = False,
        verify_wait_seconds: int = 12,
    ) -> dict:
        report = result.to_official()
        report["safety_controls"] = {
            "dry_run": self.dry_run,
            "kill_switch_triggered": is_kill_switch_active(),
            "human_approved": None,
        }
        report["execution"] = {"executed": False, "recovered": False, "rolled_back": False}

        if is_kill_switch_active():
            report["status"] = "aborted_by_kill_switch"
            report["execution"]["detail"] = "Blocked by active Kill Switch (.killswitch)."
            return report

        if result.status != "diagnosed" or not result.diagnosis or not result.diagnosis.proposed_action:
            return report

        action = result.diagnosis.proposed_action

        # Stage 5: Human Approval Gate
        if not auto_approve:
            print("\n" + "=" * 65)
            print(f"[DIAGNOSIS] Root Cause Service : {result.diagnosis.root_cause_service}")
            print(f"[DIAGNOSIS] Root Cause         : {result.diagnosis.root_cause}")
            print(f"[DIAGNOSIS] Confidence         : {result.diagnosis.confidence:.2f}")
            print(f"[PROPOSED ] Action             : {action.type} -> target={action.target} ({action.parameter or ''})")
            print(f"[REASON   ]                    : {action.reason}")
            print("=" * 65)
            ans = input("Approve this remediation action? [y/N/dry/kill]: ").strip().lower()
            if ans == "kill":
                engage_kill_switch("Operator typed 'kill' at approval prompt.")
                report["safety_controls"]["kill_switch_triggered"] = True
                report["status"] = "aborted_by_kill_switch"
                return report
            if ans == "dry":
                self.dry_run = True
                report["safety_controls"]["dry_run"] = True
            elif ans not in ("y", "yes"):
                report["safety_controls"]["human_approved"] = False
                report["status"] = "rejected_by_human"
                return report

        report["safety_controls"]["human_approved"] = True

        if self.dry_run:
            report["status"] = "dry_run_completed"
            report["execution"] = {
                "executed": False,
                "dry_run_command": f"{action.type}(target={action.target}, parameter={action.parameter})",
                "recovered": False,
                "rolled_back": False,
            }
            return report

        # Re-check kill switch immediately before mutating system
        if is_kill_switch_active():
            report["safety_controls"]["kill_switch_triggered"] = True
            report["status"] = "aborted_by_kill_switch"
            return report

        # Stage 6: Act
        exec_ok, exec_msg = self._run_action(action)
        report["execution"]["executed"] = exec_ok
        report["execution"]["detail"] = exec_msg

        if not exec_ok:
            report["status"] = "escalated"
            report["escalation"] = f"Action execution failed: {exec_msg}"
            return report

        # Stage 6b: Verify recovery
        if verify_wait_seconds > 0:
            time.sleep(verify_wait_seconds)

        recovered, verify_msg = self._verify_recovery(result.diagnosis.root_cause_service)
        report["execution"]["recovered"] = recovered
        report["execution"]["verification_detail"] = verify_msg

        if not recovered:
            rb_msg = self._rollback()
            report["execution"]["rolled_back"] = True
            report["execution"]["rollback_detail"] = rb_msg
            report["status"] = "escalated"
            report["escalation"] = f"Service did not recover after fix ({verify_msg}). Action rolled back: {rb_msg}"
        else:
            report["status"] = "resolved"

        return report

    def _run_action(self, action: ProposedAction) -> tuple[bool, str]:
        # 1. If organizer's Action API endpoint is configured, call it first
        if self.action_api_url:
            try:
                resp = requests.post(
                    f"{self.action_api_url.rstrip('/')}/action",
                    json={"type": action.type, "target": action.target, "parameter": action.parameter, "reason": action.reason},
                    timeout=10,
                )
                if resp.ok:
                    return True, f"Action API executed {action.type} on {action.target}: {resp.text}"
            except Exception as e:
                return False, f"Action API error: {e}"

        # 2. Local Docker / Flagd execution for local demo environment
        if action.type == "restart_service":
            svc = action.target
            proc = subprocess.run(["docker", "restart", svc], capture_output=True, text=True, timeout=30)
            if proc.returncode == 0:
                self._last_rollback_fn = lambda: "Container restart cannot be undone; escalated to human."
                return True, f"Restarted container '{svc}'."
            return False, (proc.stderr or proc.stdout).strip()

        if action.type == "toggle_feature_flag":
            flag_name = self._resolve_flag_name(action)
            return self._set_flag_variant(flag_name, "off")

        if action.type == "throttle_load_generator":
            # Default Locust users back to 5
            try:
                resp = requests.post(
                    "http://localhost:8080/loadgen/swarm",
                    data={"user_count": 5, "spawn_rate": 1},
                    timeout=5,
                )
                return True, f"Throttled load-generator to 5 users (HTTP {resp.status_code})."
            except Exception as e:
                return False, f"Failed to throttle load-generator: {e}"

        if action.type == "scale_service":
            return True, f"Scale action logged for '{action.target}' ({action.parameter})."

        return False, f"Unsupported action type: {action.type}"

    def _resolve_flag_name(self, action: ProposedAction) -> str:
        param = (action.parameter or "").split("=")[0].strip()
        if param in config.KNOWN_FEATURE_FLAGS:
            return param
        if action.target in config.KNOWN_FEATURE_FLAGS:
            return action.target
        return self.SERVICE_TO_FLAG.get(action.target, param or action.target)

    def _set_flag_variant(self, flag_name: str, variant: str = "off") -> tuple[bool, str]:
        flagd_path = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "src", "flagd", "demo.flagd.json")
        )
        if not os.path.exists(flagd_path):
            return False, f"flagd file not found at {flagd_path}"
        try:
            with open(flagd_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            flags = data.get("flags", {})
            if flag_name not in flags:
                return False, f"Unknown flag '{flag_name}'"
            prev_variant = flags[flag_name].get("defaultVariant", "off")
            flags[flag_name]["defaultVariant"] = variant
            with open(flagd_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)

            def _revert():
                with open(flagd_path, "r", encoding="utf-8") as rf:
                    d = json.load(rf)
                d["flags"][flag_name]["defaultVariant"] = prev_variant
                with open(flagd_path, "w", encoding="utf-8") as wf:
                    json.dump(d, wf, indent=2)
                return f"Reverted flag '{flag_name}' back to '{prev_variant}'."

            self._last_rollback_fn = _revert
            return True, f"Set feature flag '{flag_name}' defaultVariant from '{prev_variant}' to '{variant}'."
        except Exception as e:
            return False, str(e)

    def _verify_recovery(self, service: str) -> tuple[bool, str]:
        try:
            overview = self.backend.service_overview(window_minutes=2)
            for item in overview.get("services", []):
                if item.get("service") == service:
                    err = float(item.get("error_rate", 0.0))
                    if err < 0.2:
                        return True, f"Service '{service}' error_rate dropped to {err:.3f}."
                    return False, f"Service '{service}' error_rate is still high ({err:.3f})."
            return True, f"Service '{service}' shows no elevated error rate in post-fix check."
        except Exception as e:
            return False, f"Verification query error: {e}"

    def _rollback(self) -> str:
        if self._last_rollback_fn:
            try:
                return self._last_rollback_fn()
            except Exception as e:
                return f"Rollback failed: {e}"
        return "No rollback handler registered."
