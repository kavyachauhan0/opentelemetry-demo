"""Evidence verification layer: every claimed excerpt/reference must literally
exist in a tool output the agent actually received. Invented evidence is rejected."""
from __future__ import annotations
import re
from . import config
from .schemas import Diagnosis

TOOL_TO_SOURCE = {
    "query_traces": "trace",
    "query_logs": "log",
    "query_metrics": "metric",
    "get_container_health": "container_health",
    "get_deploy_history": "deploy_history",
    "get_service_overview": "metric",
}
MIN_EXCERPT_CHARS = 6


def normalize(s: str) -> str:
    """Remove ALL whitespace + lowercase, so '"error_rate": 0.42' == '"error_rate":0.42'."""
    return re.sub(r"\s+", "", str(s)).lower()


def flatten(obj) -> str:
    """All leaf values as plain text lines (avoids JSON-escaping mismatches)."""
    out: list[str] = []

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                walk(v)
        elif isinstance(o, (list, tuple)):
            for v in o:
                walk(v)
        elif o is not None:
            out.append(str(o))

    walk(obj)
    return "\n".join(out)


class EvidenceStore:
    def __init__(self):
        self.records: list[tuple[str, list[str]]] = []  # (source, normalized variants)

    def add(self, tool_name: str, variants: list[str]):
        source = TOOL_TO_SOURCE.get(tool_name, tool_name)
        self.records.append((source, [normalize(v) for v in variants]))

    def contains(self, text: str, source: str | None = None) -> bool:
        needle = normalize(text)
        if not needle:
            return False
        for src, variants in self.records:
            if source and src != source:
                continue
            if any(needle in v for v in variants):
                return True
        return False


def verify_diagnosis(diag: Diagnosis, store: EvidenceStore) -> list[str]:
    """Returns a list of problems. Empty list = fully verified."""
    problems: list[str] = []

    if not store.contains(diag.root_cause_service):
        problems.append(
            f"root_cause_service '{diag.root_cause_service}' never appears in any tool output."
        )

    for i, ev in enumerate(diag.evidence):
        tag = f"evidence[{i}] ({ev.source})"
        if len(ev.excerpt.strip()) < MIN_EXCERPT_CHARS:
            problems.append(f"{tag}: excerpt too short to be meaningful.")
            continue
        if not store.contains(ev.excerpt, ev.source):
            problems.append(
                f"{tag}: excerpt not found verbatim in any '{ev.source}' tool output: "
                f"'{ev.excerpt[:80]}'"
            )
        if ev.reference and not store.contains(ev.reference, ev.source):
            problems.append(
                f"{tag}: reference '{ev.reference}' not found in any '{ev.source}' tool output."
            )

    if diag.proposed_action:
        target = diag.proposed_action.target
        known_flags_norm = {normalize(f) for f in getattr(config, "KNOWN_FEATURE_FLAGS", set())}
        is_valid_flag = (
            diag.proposed_action.type == "toggle_feature_flag"
            and normalize(target) in known_flags_norm
        )
        is_loadgen = (
            diag.proposed_action.type == "throttle_load_generator"
            and normalize(target) in {"load-generator", "loadgenerator", "locust", "frontend"}
        )
        if not (store.contains(target) or is_valid_flag or is_loadgen):
            problems.append(
                f"proposed_action.target '{target}' never appears in tool output and is not an allowlisted flag/target."
            )
    return problems