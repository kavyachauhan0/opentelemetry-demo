"""Agent loop: investigate with tools -> submit_diagnosis -> schema check ->
evidence verification -> confidence gate -> (diagnosed | escalated)."""
from __future__ import annotations
import json
import os

from pydantic import ValidationError

from . import config
from .backends import Backend
from .llm import assistant_message
from .schemas import AgentResult, Diagnosis, ToolLog
from .tools import INVESTIGATION_TOOLS, TOOL_SPECS, ToolRunner
from .verifier import EvidenceStore, verify_diagnosis

SYSTEM_PROMPT = """You are an AI on-call engineer investigating a production incident in a cloud-native
microservices e-commerce app (OpenTelemetry Astronomy Shop).

Method (structure before reasoning):
1. Start with get_service_overview to see every service's error rate and latency at once. The service that
   raised the alert is often only a VICTIM; failures ripple upstream. Follow the call chain to the deepest
   failing or slow dependency, then confirm with error traces (query_traces) and that service's logs
   (query_logs level=ERROR), then container health and deploy history. Decide each next tool from what you found.
2. Only claim what the tool outputs show. Never invent trace IDs, log lines, numbers or service names.
3. Rule out alternatives (e.g. no recent deploy, healthy containers) before concluding.
4. When ready, call submit_diagnosis. Every evidence excerpt MUST be a contiguous fragment copied verbatim from
   a tool output (for metrics, copy the JSON fragment, e.g. "service": "payment", "error_rate": 0.42).
5. Propose at most one remedy from the allowed set (restart_service, toggle_feature_flag,
   throttle_load_generator, scale_service) with a short reason. You may NOT read feature-flag settings
   to diagnose, and never query feature_flag/flagd data; diagnosis must come from telemetry.
   When proposing toggle_feature_flag, set target to the root cause service name (e.g. "payment", "cart",
   "product-catalog", "recommendation", "ad") and parameter to the flag name (e.g. "paymentFailure=off",
   "cartFailure=off", "productCatalogFailure=off", "recommendationCacheFailure=off", "adHighCpu=off",
   "adManualGc=off", "adFailure=off", "loadGeneratorFloodHomepage=off").
6. Report confidence honestly (0-1). If evidence is thin or contradictory, call escalate_to_human with what
   you found. Escalating is a correct outcome and scores better than a confident wrong fix.
Be efficient: you have a limited number of steps."""


class OnCallAgent:
    def __init__(self, llm, backend: Backend, max_steps: int | None = None,
                 confidence_threshold: float | None = None):
        self.llm = llm
        self.backend = backend
        self.max_steps = max_steps or config.MAX_STEPS
        self.threshold = config.CONFIDENCE_THRESHOLD if confidence_threshold is None else confidence_threshold

    def investigate(self, alert: str) -> AgentResult:
        store = EvidenceStore()
        runner = ToolRunner(self.backend, store)
        messages = [{"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": f"Incident alert:\n{alert}"}]
        tool_log: list[ToolLog] = []
        state = {"schema_errors": 0, "verify_failures": 0, "last_diag": None}

        for step in range(1, self.max_steps + 1):
            if os.path.exists(getattr(config, "KILL_SWITCH_FILE", ".killswitch")):
                return self._escalate("KILL SWITCH ENGAGED: Investigation immediately halted by operator.",
                                      state, tool_log, step)
            try:
                resp = self.llm.chat(messages, TOOL_SPECS)
            except RuntimeError as e:
                return self._escalate(f"LLM unavailable: {e}", state, tool_log, step)

            messages.append(assistant_message(resp))
            if not resp.tool_calls:
                messages.append({"role": "user", "content":
                                 "Continue: call an investigation tool, or submit_diagnosis, or escalate_to_human."})
                continue

            for tc in resp.tool_calls:
                if tc.name in INVESTIGATION_TOOLS:
                    out = runner.run(tc.name, tc.arguments)
                    tool_log.append(ToolLog(step=step, name=tc.name, arguments=tc.arguments,
                                            output_preview=out[:300]))
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": out})

                elif tc.name == "escalate_to_human":
                    return self._escalate(tc.arguments.get("reason", "agent chose to escalate"),
                                          state, tool_log, step,
                                          findings=tc.arguments.get("findings", ""))

                elif tc.name == "submit_diagnosis":
                    reply, result = self._handle_submit(tc.arguments, store, state, tool_log, step)
                    if result:
                        return result
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": reply})

                else:
                    messages.append({"role": "tool", "tool_call_id": tc.id,
                                     "content": json.dumps({"error": f"unknown tool {tc.name}"})})

        return self._escalate(f"Step budget ({self.max_steps}) exhausted without a conclusion.",
                              state, tool_log, self.max_steps)

    # ------------------------------------------------------------------
    def _handle_submit(self, args, store, state, tool_log, step):
        # 1) strict structured output
        try:
            if "__parse_error__" in args:
                raise ValueError("arguments were not valid JSON")
            diag = Diagnosis.model_validate(args)
        except (ValidationError, ValueError) as e:
            state["schema_errors"] += 1
            if state["schema_errors"] > config.MAX_SCHEMA_RETRIES:
                return "", self._escalate("Could not produce schema-valid output.", state, tool_log, step)
            return f"REJECTED: output failed schema validation: {e}. Fix and resubmit.", None

        state["last_diag"] = diag

        # 2) evidence verification
        problems = verify_diagnosis(diag, store)
        if problems:
            state["verify_failures"] += 1
            if state["verify_failures"] >= config.MAX_VERIFY_FAILURES:
                return "", self._escalate(
                    "Evidence could not be verified against telemetry: " + "; ".join(problems),
                    state, tool_log, step)
            return ("REJECTED: evidence verification failed. Copy excerpts verbatim from tool outputs "
                    "you received, or gather more data, or escalate. Problems:\n- " + "\n- ".join(problems)), None

        # 3) confidence gate
        if diag.confidence < self.threshold:
            return "", self._escalate(
                f"Confidence {diag.confidence:.2f} below threshold {self.threshold:.2f}.",
                state, tool_log, step, verified=True)

        return "", AgentResult(status="diagnosed", diagnosis=diag, evidence_verified=True,
                               findings_summary=diag.reasoning_summary, steps=step, tool_calls=tool_log)

    def _escalate(self, reason, state, tool_log, step, findings: str = "", verified: bool = False):
        diag = state.get("last_diag")
        summary = findings or (diag.reasoning_summary if diag else "No conclusion reached.")
        return AgentResult(status="escalated", diagnosis=diag, evidence_verified=verified,
                           escalation_reason=reason, findings_summary=summary,
                           steps=step, tool_calls=tool_log)