"""Read-only process-namespace OS metrics for local/simulation use."""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Metric:
    value: float | int | list[float] | None
    unit: str
    status: str


class HostMetricsCollector:
    """Collect OS metrics visible inside the current process namespace."""

    def __init__(self, data_path: str | Path):
        self.data_path = Path(data_path)
        self._cpu_sample: tuple[int, int] | None = None

    def collect(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "cpu": self._cpu(),
            "ram": self._ram(),
            "disk": self._disk(),
            "load": self._load(),
            "network": self._network(),
            "uptime": self._uptime(),
            "sampled_at_epoch": int(time.time()),
            "source": "process-namespace-os-interfaces",
        }
        return {
            key: asdict(value) if isinstance(value, Metric) else value
            for key, value in result.items()
        }

    @staticmethod
    def _status(percent: float | None, attention: float, critical: float) -> str:
        if percent is None:
            return "unavailable"
        if percent >= critical:
            return "critical"
        if percent >= attention:
            return "attention"
        return "healthy"

    def _cpu(self) -> Metric:
        try:
            fields = Path("/proc/stat").read_text(encoding="ascii").splitlines()[0].split()[1:]
            values = [int(value) for value in fields]
            idle, total = values[3] + values[4], sum(values)
            previous, self._cpu_sample = self._cpu_sample, (idle, total)
            if previous is None or total <= previous[1]:
                return Metric(None, "%", "unavailable")
            used = 100 * (1 - (idle - previous[0]) / (total - previous[1]))
            return Metric(round(used, 1), "%", self._status(used, 80, 95))
        except (OSError, ValueError, IndexError):
            return Metric(None, "%", "unavailable")

    @staticmethod
    def _ram() -> Metric:
        try:
            info = {}
            for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
                key, raw = line.split(":", 1)
                info[key] = int(raw.strip().split()[0]) * 1024
            total, available = info["MemTotal"], info["MemAvailable"]
            used, percent = total - available, 100 * (total - available) / total
            return Metric(
                [used, total, round(percent, 1)],
                "bytes",
                HostMetricsCollector._status(percent, 80, 90),
            )
        except (OSError, ValueError, KeyError):
            return Metric(None, "bytes", "unavailable")

    def _disk(self) -> Metric:
        try:
            usage = shutil.disk_usage(self.data_path)
            percent = 100 * usage.used / usage.total
            return Metric(
                [usage.used, usage.total, round(percent, 1)], "bytes", self._status(percent, 80, 90)
            )
        except OSError:
            return Metric(None, "bytes", "unavailable")

    @staticmethod
    def _load() -> Metric:
        try:
            getloadavg = getattr(os, "getloadavg", None)
            if getloadavg is None:
                return Metric(None, "load", "unavailable")
            values = [round(value, 2) for value in getloadavg()]
            cores = os.cpu_count() or 1
            status = (
                "critical"
                if values[0] >= 2 * cores
                else "attention"
                if values[0] >= cores
                else "healthy"
            )
            return Metric(values, "load", status)
        except (AttributeError, OSError):
            return Metric(None, "load", "unavailable")

    @staticmethod
    def _network() -> Metric:
        try:
            received = sent = 0
            for line in Path("/proc/net/dev").read_text(encoding="ascii").splitlines()[2:]:
                _, raw = line.split(":", 1)
                parts = raw.split()
                received += int(parts[0])
                sent += int(parts[8])
            return Metric([received, sent], "bytes", "healthy")
        except (OSError, ValueError, IndexError):
            return Metric(None, "bytes", "unavailable")

    @staticmethod
    def _uptime() -> Metric:
        try:
            return Metric(
                round(float(Path("/proc/uptime").read_text(encoding="ascii").split()[0])),
                "seconds",
                "healthy",
            )
        except (OSError, ValueError, IndexError):
            return Metric(None, "seconds", "unavailable")
