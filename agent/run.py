"""Usage (from repo root):
    python -m agent.run --backend mock --incident payment_failure
    python -m agent.run --backend live --alert "ALERT: ..."
"""
import argparse
import json
import logging
import time
from pathlib import Path

from .agent import OnCallAgent
from .backends import SCENARIOS, make_backend
from .llm import LLMClient


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=["mock", "live"], default="mock")
    ap.add_argument("--incident", default="payment_failure", help=f"mock scenario: {list(SCENARIOS)}")
    ap.add_argument("--alert", default=None)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING)

    backend = make_backend(a.backend, a.incident)
    agent = OnCallAgent(LLMClient(), backend)
    result = agent.investigate(a.alert or backend.default_alert)

    out = json.dumps(result.to_official(), indent=2)
    print(out)
    Path("reports").mkdir(exist_ok=True)
    Path(f"reports/incident_{int(time.time())}.json").write_text(out)


if __name__ == "__main__":
    main()