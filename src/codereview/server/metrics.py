"""Minimal Prometheus text exposition without an extra dependency."""
from __future__ import annotations

import threading

_BUCKETS = (0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600)


def _labels(labels: dict | None) -> str:
    if not labels:
        return ""
    parts = []
    for key, value in sorted(labels.items()):
        escaped = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        parts.append(f'{key}="{escaped}"')
    return "{" + ",".join(parts) + "}"


class Metrics:
    def __init__(self):
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, str], float] = {}
        self._gauges: dict[tuple[str, str], float] = {}
        self._hist: dict[tuple[str, str], dict] = {}

    def inc(self, name: str, labels: dict | None = None, amount: float = 1.0) -> None:
        key = (name, _labels(labels))
        with self._lock:
            self._counters[key] = self._counters.get(key, 0.0) + amount

    def set_gauge(self, name: str, value: float, labels: dict | None = None) -> None:
        key = (name, _labels(labels))
        with self._lock:
            self._gauges[key] = float(value)

    def observe(self, name: str, value: float, labels: dict | None = None) -> None:
        key = (name, _labels(labels))
        with self._lock:
            bucket = self._hist.setdefault(
                key, {"count": 0, "sum": 0.0, "buckets": {b: 0 for b in _BUCKETS}}
            )
            bucket["count"] += 1
            bucket["sum"] += float(value)
            for boundary in _BUCKETS:
                if value <= boundary:
                    bucket["buckets"][boundary] += 1

    def render(self) -> str:
        from collections import defaultdict

        with self._lock:
            counters: dict[str, list] = defaultdict(list)
            gauges: dict[str, list] = defaultdict(list)
            histograms: dict[str, list] = defaultdict(list)
            for (name, labels), value in self._counters.items():
                counters[name].append((labels, value))
            for (name, labels), value in self._gauges.items():
                gauges[name].append((labels, value))
            for (name, labels), hist in self._hist.items():
                histograms[name].append((labels, hist))

            lines: list[str] = []
            for name in sorted(counters):
                lines.append(f"# TYPE {name} counter")
                for labels, value in sorted(counters[name]):
                    lines.append(f"{name}{labels} {value:g}")
            for name in sorted(gauges):
                lines.append(f"# TYPE {name} gauge")
                for labels, value in sorted(gauges[name]):
                    lines.append(f"{name}{labels} {value:g}")
            for name in sorted(histograms):
                lines.append(f"# TYPE {name} histogram")
                for labels, hist in sorted(histograms[name]):
                    prefix = labels[:-1] + "," if labels else "{"
                    for boundary, count in sorted(hist["buckets"].items()):
                        lines.append(f'{name}_bucket{prefix}le="{boundary}"}} {count}')
                    lines.append(f'{name}_bucket{prefix}le="+Inf"}} {hist["count"]}')
                    lines.append(f"{name}_sum{labels} {hist['sum']:g}")
                    lines.append(f"{name}_count{labels} {hist['count']}")
            return "\n".join(lines) + ("\n" if lines else "")
