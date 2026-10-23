"""Tool specs (what the LLM sees) + dispatcher that records every output for verification."""
from __future__ import annotations
import json

from . import config
from .backends import Backend
from .verifier import EvidenceStore, flatten

TOOL_SPECS = [
    {"type": "function", "function": {
        "name": "get_service_overview",
        "description": "START HERE. One call returns error_rate (0-1), p95_ms and calls_per_sec for EVERY service, sorted by error rate. The alerting service is often just a victim; look for the deepest failing/slow dependency.",
        "parameters": {"type": "object", "properties": {
            "window_minutes": {"type": "integer"}}, "required": []}}},
    {"type": "function", "function": {
        "name": "query_traces",
        "description": "Search recent distributed traces for ONE service (required, e.g. frontend, checkout, payment). Use error_only=true to find failing requests. Returns trace IDs and spans (error spans first).",
        "parameters": {"type": "object", "properties": {
            "service": {"type": "string", "description": "Service name, e.g. 'payment'"},
            "error_only": {"type": "boolean"},
            "window_minutes": {"type": "integer"},
            "limit": {"type": "integer"}}, "required": ["service"]}}},
    {"type": "function", "function": {
        "name": "query_logs",
        "description": "Search recent logs. Filter by service, level (ERROR/WARN/INFO) and keyword.",
        "parameters": {"type": "object", "properties": {
            "service": {"type": "string"}, "level": {"type": "string"}, "keyword": {"type": "string"},
            "window_minutes": {"type": "integer"}, "limit": {"type": "integer"}}, "required": []}}},
    {"type": "function", "function": {
        "name": "query_metrics",
        "description": "Run a PromQL instant query (error rates, latency, CPU, memory).",
        "parameters": {"type": "object", "properties": {
            "promql": {"type": "string"}, "window_minutes": {"type": "integer"}}, "required": ["promql"]}}},
    {"type": "function", "function": {
        "name": "get_container_health",
        "description": "Container state, restart counts, CPU% and memory% per service.",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {
        "name": "get_deploy_history",
        "description": "Recent deployments/changes per service.",
        "parameters": {"type": "object", "properties": {"limit": {"type": "integer"}}, "required": []}}},
    {"type": "function", "function": {
        "name": "submit_diagnosis",
        "description": "Submit your FINAL diagnosis. Every evidence excerpt must be copied verbatim from a tool output you received. Be honest in 'confidence' (0-1).",
        "parameters": {"type": "object", "properties": {
            "root_cause_service": {"type": "string"},
            "root_cause": {"type": "string"},
            "confidence": {"type": "number"},
            "evidence": {"type": "array", "items": {"type": "object", "properties": {
                "source": {"type": "string", "enum": ["trace", "log", "metric", "container_health", "deploy_history"]},
                "reference": {"type": "string", "description": "trace id / metric name / log timestamp"},
                "excerpt": {"type": "string", "description": "a contiguous fragment copied verbatim from the raw tool output JSON (e.g. a log body, or \"service\": \"payment\", \"error_rate\": 0.42)"}},
                "required": ["source", "excerpt"]}},
            "proposed_action": {"type": "object", "properties": {
                "type": {"type": "string", "enum": ["restart_service", "toggle_feature_flag", "throttle_load_generator", "scale_service"]},
                "target": {"type": "string"},
                "parameter": {"type": "string", "description": "flag name/value, replica count, etc."},
                "reason": {"type": "string"}}, "required": ["type", "target", "reason"]},
            "reasoning_summary": {"type": "string"}},
            "required": ["root_cause_service", "root_cause", "confidence", "evidence", "reasoning_summary"]}}},
    {"type": "function", "function": {
        "name": "escalate_to_human",
        "description": "Use when you cannot reach a confident, evidence-backed diagnosis. This is a correct outcome.",
        "parameters": {"type": "object", "properties": {
            "reason": {"type": "string"}, "findings": {"type": "string", "description": "what you found so far"}},
            "required": ["reason", "findings"]}}},
]

INVESTIGATION_TOOLS = {"get_service_overview", "query_traces", "query_logs", "query_metrics", "get_container_health", "get_deploy_history"}


class ToolRunner:
    def __init__(self, backend: Backend, store: EvidenceStore):
        self.backend = backend
        self.store = store

    BLOCKED_TERMS = ("feature_flag", "flagd")  # rules: diagnosing from flag settings = disqualification

    def run(self, name: str, args: dict) -> str:
        try:
            blob = json.dumps(args).lower()
            if any(t in blob for t in self.BLOCKED_TERMS):
                obj = {"error": "blocked: feature-flag data is off-limits for diagnosis. Use other telemetry."}
                text = json.dumps(obj)
                self.store.add(name, [text])
                return text
            obj = self._dispatch(name, args)
        except NotImplementedError as e:
            obj = {"error": f"tool unavailable: {e}"}
        except Exception as e:  # network errors, bad args... let the agent adapt
            obj = {"error": f"{type(e).__name__}: {e}"}

        text = json.dumps(obj, ensure_ascii=False)
        truncated = len(text) > config.MAX_TOOL_CHARS
        if truncated:
            text = text[: config.MAX_TOOL_CHARS] + '..."[TRUNCATED]"'
        # Store exactly what the model could see (plus an unescaped view if not truncated)
        variants = [text] if truncated else [text, flatten(obj)]
        self.store.add(name, variants)
        return text

    def _dispatch(self, name: str, a: dict):
        b = self.backend
        if "__parse_error__" in a:
            return {"error": "arguments were not valid JSON; retry with valid JSON"}
        if name == "get_service_overview":
            return b.service_overview(int(a.get("window_minutes", 5)))
        if name == "query_traces":
            return b.query_traces(a.get("service"), bool(a.get("error_only", False)),
                                  int(a.get("window_minutes", 15)), int(a.get("limit", 5)))
        if name == "query_logs":
            return b.query_logs(a.get("service"), a.get("level"), a.get("keyword"),
                                int(a.get("window_minutes", 15)), int(a.get("limit", 20)))
        if name == "query_metrics":
            return b.query_metrics(a["promql"], int(a.get("window_minutes", 15)))
        if name == "get_container_health":
            return b.get_container_health()
        if name == "get_deploy_history":
            return b.get_deploy_history(int(a.get("limit", 10)))
        return {"error": f"unknown tool '{name}'"}