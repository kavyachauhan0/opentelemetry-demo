import json
import os
import subprocess
import requests


class Backend:
    default_alert = "ALERT: High error rate detected on checkout and frontend services."

    def service_overview(self, window_minutes: int) -> dict: raise NotImplementedError
    def query_traces(self, service: str, error_only: bool, window_minutes: int, limit: int) -> dict: raise NotImplementedError
    def query_logs(self, service: str, level: str, keyword: str, window_minutes: int, limit: int) -> dict: raise NotImplementedError
    def query_metrics(self, promql: str, window_minutes: int) -> dict: raise NotImplementedError
    def get_container_health(self) -> dict: raise NotImplementedError
    def get_deploy_history(self, limit: int) -> dict: raise NotImplementedError


class LiveBackend(Backend):
    CORE_SERVICES = [
        "frontend", "checkout", "payment", "cart", "product-catalog",
        "recommendation", "ad", "currency", "shipping", "quote", "email",
    ]

    def __init__(self):
        self.prometheus_url = os.getenv("PROMETHEUS_URL", "http://localhost:9090/api/v1/query")
        self.jaeger_urls = [
            os.getenv("JAEGER_URL", "http://localhost:8080/jaeger/api/traces"),
            "http://localhost:16686/api/traces",
        ]
        self.opensearch_url = os.getenv("OPENSEARCH_URL", "http://localhost:9200/_search")

    def _get_jaeger(self, path_suffix: str = "", params: dict | None = None) -> dict:
        last_err = None
        for base in self.jaeger_urls:
            url = base if not path_suffix else base.replace("/api/traces", path_suffix)
            try:
                resp = requests.get(url, params=params, timeout=5)
                if resp.status_code == 200:
                    return resp.json()
            except Exception as e:
                last_err = e
        raise RuntimeError(f"Jaeger unreachable: {last_err}")

    def query_metrics(self, promql: str, window_minutes: int = 5) -> dict:
        try:
            resp = requests.get(self.prometheus_url, params={"query": promql}, timeout=5)
            data = resp.json()
            results = []
            for item in data.get("data", {}).get("result", [])[:25]:
                results.append({
                    "metric": item.get("metric", {}),
                    "value": item.get("value", [None, None])[1],
                })
            return {"status": data.get("status", "unknown"), "promql": promql, "results": results}
        except Exception as e:
            return {"error": str(e)}

    def service_overview(self, window_minutes: int = 5) -> dict:
        win = max(1, int(window_minutes or 5))
        services_summary = []

        # 1. Try Prometheus spanmetrics / OTel collector metrics first
        try:
            err_q = f'sum by (service_name) (rate(traces_spanmetrics_calls_total{{status_code="STATUS_CODE_ERROR"}}[{win}m])) / sum by (service_name) (rate(traces_spanmetrics_calls_total[{win}m]))'
            rps_q = f'sum by (service_name) (rate(traces_spanmetrics_calls_total[{win}m]))'
            p95_q = f'histogram_quantile(0.95, sum by (le, service_name) (rate(traces_spanmetrics_latency_bucket[{win}m])))'

            err_resp = requests.get(self.prometheus_url, params={"query": err_q}, timeout=4).json()
            rps_resp = requests.get(self.prometheus_url, params={"query": rps_q}, timeout=4).json()
            p95_resp = requests.get(self.prometheus_url, params={"query": p95_q}, timeout=4).json()

            by_svc: dict[str, dict] = {}
            for item in rps_resp.get("data", {}).get("result", []):
                svc = item.get("metric", {}).get("service_name")
                val = float(item.get("value", [0, "0"])[1] or 0)
                if svc:
                    by_svc.setdefault(svc, {"service": svc, "error_rate": 0.0, "p95_ms": 0.0, "calls_per_sec": round(val, 2)})

            for item in err_resp.get("data", {}).get("result", []):
                svc = item.get("metric", {}).get("service_name")
                raw = item.get("value", [0, "0"])[1]
                val = 0.0 if raw in ("NaN", None) else float(raw)
                if svc:
                    by_svc.setdefault(svc, {"service": svc, "error_rate": 0.0, "p95_ms": 0.0, "calls_per_sec": 0.0})
                    by_svc[svc]["error_rate"] = round(val, 3)

            for item in p95_resp.get("data", {}).get("result", []):
                svc = item.get("metric", {}).get("service_name")
                raw = item.get("value", [0, "0"])[1]
                val = 0.0 if raw in ("NaN", None) else float(raw)
                if svc and svc in by_svc:
                    by_svc[svc]["p95_ms"] = round(val, 1)

            if by_svc:
                services_summary = sorted(by_svc.values(), key=lambda x: (x["error_rate"], x["p95_ms"]), reverse=True)
                return {"window_minutes": win, "services": services_summary}
        except Exception:
            pass

        # 2. Fallback: Compute real error rate & p95 latency per service from recent Jaeger traces
        for svc in self.CORE_SERVICES:
            try:
                raw = self._get_jaeger(params={"service": svc, "limit": 20})
                traces = raw.get("data", [])
                if not traces:
                    continue
                total_spans = 0
                error_spans = 0
                durations = []
                for t in traces:
                    procs = {k: v.get("serviceName", "") for k, v in t.get("processes", {}).items()}
                    for sp in t.get("spans", []):
                        if procs.get(sp.get("processID"), "") != svc:
                            continue
                        total_spans += 1
                        dur_ms = round(sp.get("duration", 0) / 1000.0, 2)
                        durations.append(dur_ms)
                        tags = {tg.get("key"): tg.get("value") for tg in sp.get("tags", [])}
                        if tags.get("error") is True or str(tags.get("otel.status_code", "")).upper() == "ERROR":
                            error_spans += 1
                if total_spans > 0:
                    durations.sort()
                    p95_idx = min(len(durations) - 1, int(len(durations) * 0.95))
                    services_summary.append({
                        "service": svc,
                        "error_rate": round(error_spans / total_spans, 3),
                        "p95_ms": durations[p95_idx],
                        "sampled_spans": total_spans,
                    })
            except Exception:
                continue

        services_summary.sort(key=lambda x: (x.get("error_rate", 0), x.get("p95_ms", 0)), reverse=True)
        return {"window_minutes": win, "services": services_summary}

    def query_traces(self, service: str, error_only: bool = False, window_minutes: int = 15, limit: int = 5) -> dict:
        try:
            params: dict = {"service": service, "limit": max(1, min(int(limit or 5), 10))}
            if error_only:
                params["tags"] = '{"error":"true"}'
            raw = self._get_jaeger(params=params)
            data = raw.get("data", [])
            traces_out = []
            for t in data:
                trace_id = t.get("traceID")
                procs = {k: v.get("serviceName", "unknown") for k, v in t.get("processes", {}).items()}
                parsed_spans = []
                for sp in t.get("spans", []):
                    svc_name = procs.get(sp.get("processID"), "unknown")
                    tags = {tg.get("key"): tg.get("value") for tg in sp.get("tags", [])}
                    is_err = (
                        tags.get("error") is True
                        or str(tags.get("otel.status_code", "")).upper() == "ERROR"
                        or int(tags.get("rpc.grpc.status_code", 0) or 0) != 0
                        or int(tags.get("http.status_code", 200) or 200) >= 500
                    )
                    status_msg = (
                        tags.get("otel.status_description")
                        or tags.get("exception.message")
                        or tags.get("error.message")
                        or ""
                    )
                    if not status_msg:
                        for lg in sp.get("logs", []):
                            lfields = {f.get("key"): f.get("value") for f in lg.get("fields", [])}
                            if lfields.get("exception.message") or lfields.get("message") or lfields.get("event") == "error":
                                status_msg = str(lfields.get("exception.message") or lfields.get("message") or lfields)
                                break
                    parsed_spans.append({
                        "spanID": sp.get("spanID"),
                        "service": svc_name,
                        "operation": sp.get("operationName"),
                        "duration_ms": round(sp.get("duration", 0) / 1000.0, 2),
                        "error": is_err,
                        "grpc_status": tags.get("rpc.grpc.status_code"),
                        "http_status": tags.get("http.status_code"),
                        "status_message": status_msg[:240] if status_msg else "",
                    })
                # Sort error spans first, then slowest spans
                parsed_spans.sort(key=lambda s: (s["error"], s["duration_ms"]), reverse=True)
                traces_out.append({
                    "traceID": trace_id,
                    "total_spans": len(parsed_spans),
                    "spans": parsed_spans[:10],
                })
            return {"service": service, "error_only": error_only, "traces": traces_out}
        except Exception as e:
            return {"error": str(e)}

    def query_logs(self, service: str | None = None, level: str | None = None,
                   keyword: str | None = None, window_minutes: int = 15, limit: int = 20) -> dict:
        target = (service or "").strip()
        max_lines = max(1, min(int(limit or 20), 40))
        if not target:
            return {"error": "Please provide a service name (e.g., 'payment', 'cart', 'checkout')."}

        try:
            proc = subprocess.run(
                ["docker", "logs", "--tail", "250", target],
                capture_output=True, text=True, timeout=6
            )
            combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
            lines = [ln.strip() for ln in combined.splitlines() if ln.strip()]

            if level:
                lvl = level.upper()
                filtered = [ln for ln in lines if lvl in ln.upper() or (lvl == "ERROR" and any(k in ln.lower() for k in ("error", "exception", "fail", "refused", "unavailable", "timeout")))]
                if filtered:
                    lines = filtered

            if keyword:
                kw = keyword.lower()
                lines = [ln for ln in lines if kw in ln.lower()]

            selected = [ln[:300] for ln in lines[-max_lines:]]
            return {"service": target, "level": level, "count": len(selected), "logs": selected}
        except Exception as e:
            return {"service": target, "error": str(e)}

    def get_container_health(self) -> dict:
        try:
            ps_proc = subprocess.run(
                ["docker", "ps", "-a", "--format", "{{json .}}"],
                capture_output=True, text=True, timeout=6
            )
            stats_map: dict[str, dict] = {}
            try:
                st_proc = subprocess.run(
                    ["docker", "stats", "--no-stream", "--format", "{{json .}}"],
                    capture_output=True, text=True, timeout=8
                )
                for line in (st_proc.stdout or "").splitlines():
                    if not line.strip():
                        continue
                    row = json.loads(line)
                    stats_map[row.get("Name", "")] = {
                        "cpu_percent": row.get("CPUPerc", "0%"),
                        "mem_percent": row.get("MemPerc", "0%"),
                        "mem_usage": row.get("MemUsage", ""),
                    }
            except Exception:
                pass

            containers = []
            for line in (ps_proc.stdout or "").splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                name = row.get("Names", "")
                st = stats_map.get(name, {})
                containers.append({
                    "service": name,
                    "state": row.get("State", ""),
                    "status": row.get("Status", ""),
                    "cpu_percent": st.get("cpu_percent", "N/A"),
                    "mem_percent": st.get("mem_percent", "N/A"),
                })
            # Sort unhealthy/exited/restarting containers first
            containers.sort(key=lambda c: (c["state"] != "running", "unhealthy" in c["status"].lower()), reverse=True)
            return {"containers": containers}
        except Exception as e:
            return {"error": str(e)}

    def get_deploy_history(self, limit: int = 10) -> dict:
        try:
            git_proc = subprocess.run(
                ["git", "log", f"-n{int(limit or 5)}", "--oneline"],
                capture_output=True, text=True, timeout=4
            )
            commits = [ln.strip() for ln in (git_proc.stdout or "").splitlines() if ln.strip()]
            return {"recent_deploys": "No recent service redeployments detected.", "recent_commits": commits}
        except Exception as e:
            return {"recent_deploys": "None", "note": str(e)}


class MockBackend(Backend):
    default_alert = "ALERT: High error rate on checkout service (HTTP 500s spiking)."

    def service_overview(self, window_minutes: int = 5) -> dict:
        return {
            "window_minutes": window_minutes,
            "services": [
                {"service": "payment", "error_rate": 0.95, "p95_ms": 420.5, "calls_per_sec": 4.2},
                {"service": "checkout", "error_rate": 0.91, "p95_ms": 480.0, "calls_per_sec": 4.2},
                {"service": "frontend", "error_rate": 0.35, "p95_ms": 510.2, "calls_per_sec": 12.0},
                {"service": "cart", "error_rate": 0.0, "p95_ms": 18.4, "calls_per_sec": 8.1},
            ],
        }

    def query_traces(self, service: str, error_only: bool = False, window_minutes: int = 15, limit: int = 5) -> dict:
        return {
            "service": service,
            "error_only": error_only,
            "traces": [
                {
                    "traceID": "7f3a9b2c1d4e5f608192a3b4c5d6e7f8",
                    "total_spans": 4,
                    "spans": [
                        {
                            "spanID": "a1b2c3d4e5f60718",
                            "service": "payment",
                            "operation": "oteldemo.PaymentService/Charge",
                            "duration_ms": 385.2,
                            "error": True,
                            "grpc_status": 13,
                            "status_message": "PaymentService Charge failed: card charge declined due to failure injection",
                        },
                        {
                            "spanID": "1122334455667788",
                            "service": "checkout",
                            "operation": "oteldemo.CheckoutService/PlaceOrder",
                            "duration_ms": 412.0,
                            "error": True,
                            "grpc_status": 13,
                            "status_message": "failed to charge card: rpc error: code = Internal desc = PaymentService Charge failed",
                        },
                    ],
                }
            ],
        }

    def query_logs(self, service: str = "", level: str = "ERROR", keyword: str = "", window_minutes: int = 15, limit: int = 20) -> dict:
        if service == "payment":
            return {
                "service": "payment",
                "level": "ERROR",
                "logs": [
                    "2026-10-23T12:01:14Z ERROR [payment] Charge request failed for order: PaymentService failure encountered during charge",
                ],
            }
        return {
            "service": service or "checkout",
            "level": level or "ERROR",
            "logs": [
                f"2026-10-23T12:01:15Z ERROR [{service or 'checkout'}] upstream call to payment:50051 failed with code INTERNAL",
            ],
        }

    def query_metrics(self, promql: str, window_minutes: int = 15) -> dict:
        return {
            "status": "success",
            "promql": promql,
            "results": [{"metric": {"service_name": "payment"}, "value": "0.95"}],
        }

    def get_container_health(self) -> dict:
        return {
            "containers": [
                {"service": "payment", "state": "running", "status": "Up 15 minutes (healthy)", "cpu_percent": "3.2%", "mem_percent": "4.1%"},
                {"service": "checkout", "state": "running", "status": "Up 15 minutes (healthy)", "cpu_percent": "2.1%", "mem_percent": "3.0%"},
            ]
        }

    def get_deploy_history(self, limit: int = 10) -> dict:
        return {"recent_deploys": "No recent service redeployments detected."}


SCENARIOS = {"payment_failure": MockBackend}


def make_backend(backend_type: str, scenario: str = "payment_failure") -> Backend:
    if backend_type == "live":
        return LiveBackend()
    return SCENARIOS.get(scenario, MockBackend)()