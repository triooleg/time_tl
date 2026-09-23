"""Bounded, read-only Prometheus node_exporter metrics client."""

from __future__ import annotations

import ipaddress
import math
import re
import time
from collections.abc import Callable
from dataclasses import asdict
from typing import Any
from urllib.parse import urlsplit

import httpx

from .metrics import Metric

FetchMetrics = Callable[[str, float, int], bytes]
_SAMPLE = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(?P<labels>.*)\})?\s+"
    r"(?P<value>[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)"
    r"(?:\s+\d+)?$"
)
_LABEL = re.compile(r'(\w+)="((?:\\.|[^"\\])*)"(?:,|$)')


def _unescape_label(value: str) -> str:
    return value.replace(r"\\", "\\").replace(r"\"", '"').replace(r"\n", "\n")


def _internal_metrics_url(value: str) -> str:
    """Accept only a fixed internal node_exporter metrics URL."""
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path != "/metrics"
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("node_exporter URL must be an internal /metrics endpoint")
    host = parsed.hostname.rstrip(".").lower()
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        internal = "." not in host or host.endswith(
            (".internal", ".local", ".svc", ".svc.cluster.local")
        )
    else:
        internal = address.is_private or address.is_loopback or address.is_link_local
    if not internal:
        raise ValueError("node_exporter URL must not target a public host")
    return value


def fetch_with_httpx(url: str, timeout_seconds: float, max_bytes: int) -> bytes:
    """Fetch without redirects, proxy inheritance, credentials, or unbounded buffering."""
    with httpx.Client(follow_redirects=False, trust_env=False, timeout=timeout_seconds) as client:
        with client.stream("GET", url, headers={"Accept": "text/plain"}) as response:
            response.raise_for_status()
            declared = response.headers.get("content-length")
            if declared is not None and int(declared) > max_bytes:
                raise ValueError("node_exporter response exceeds size limit")
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError("node_exporter response exceeds size limit")
                chunks.append(chunk)
            return b"".join(chunks)


def _parse_samples(payload: bytes) -> dict[str, list[tuple[dict[str, str], float]]]:
    text = payload.decode("utf-8", errors="strict")
    result: dict[str, list[tuple[dict[str, str], float]]] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _SAMPLE.fullmatch(line)
        if match is None:
            continue
        value = float(match.group("value"))
        if not math.isfinite(value):
            continue
        labels: dict[str, str] = {}
        label_text = match.group("labels") or ""
        position = 0
        while position < len(label_text):
            label_match = _LABEL.match(label_text, position)
            if label_match is None:
                labels = {}
                break
            labels[label_match.group(1)] = _unescape_label(label_match.group(2))
            position = label_match.end()
        result.setdefault(match.group("name"), []).append((labels, value))
    return result


class NodeExporterMetricsCollector:
    """Collect aggregate host metrics from one configured internal endpoint."""

    def __init__(
        self,
        metrics_url: str,
        *,
        fetcher: FetchMetrics = fetch_with_httpx,
        timeout_seconds: float = 2.0,
        max_response_bytes: int = 1_048_576,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.metrics_url = _internal_metrics_url(metrics_url)
        if not 0 < timeout_seconds <= 5:
            raise ValueError("node_exporter timeout must be between 0 and 5 seconds")
        if not 16_384 <= max_response_bytes <= 4_194_304:
            raise ValueError("node_exporter response limit is outside safe bounds")
        self.fetcher = fetcher
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes
        self.clock = clock
        self._cpu_sample: tuple[float, float] | None = None

    @staticmethod
    def _status(percent: float | None, attention: float, critical: float) -> str:
        if percent is None:
            return "unavailable"
        if percent >= critical:
            return "critical"
        if percent >= attention:
            return "attention"
        return "healthy"

    @staticmethod
    def _metric(value: Any, unit: str, status: str) -> dict[str, Any]:
        return asdict(Metric(value, unit, status))

    def _unavailable(self, sampled_at: int) -> dict[str, Any]:
        return {
            "cpu": self._metric(None, "%", "unavailable"),
            "ram": self._metric(None, "bytes", "unavailable"),
            "disk": self._metric(None, "bytes", "unavailable"),
            "load": self._metric(None, "load", "unavailable"),
            "network": self._metric(None, "bytes", "unavailable"),
            "uptime": self._metric(None, "seconds", "unavailable"),
            "boot_time": self._metric(None, "epoch_seconds", "unavailable"),
            "sampled_at_epoch": sampled_at,
            "source": "prometheus-node-exporter",
            "source_status": "unavailable",
        }

    def collect(self) -> dict[str, Any]:
        sampled_at = int(self.clock())
        try:
            payload = self.fetcher(self.metrics_url, self.timeout_seconds, self.max_response_bytes)
            if len(payload) > self.max_response_bytes:
                raise ValueError("node_exporter response exceeds size limit")
            result = self._build(_parse_samples(payload), sampled_at)
        except (httpx.HTTPError, OSError, UnicodeError, ValueError, KeyError, ZeroDivisionError):
            return self._unavailable(sampled_at)
        result["sampled_at_epoch"] = sampled_at
        result["source"] = "prometheus-node-exporter"
        result["source_status"] = "available"
        return result

    @staticmethod
    def _single(samples: dict[str, list[tuple[dict[str, str], float]]], name: str) -> float:
        values = samples[name]
        if len(values) != 1:
            raise ValueError(f"expected one {name} sample")
        return values[0][1]

    def _build(
        self, samples: dict[str, list[tuple[dict[str, str], float]]], sampled_at: int
    ) -> dict[str, Any]:
        cpu_rows = samples["node_cpu_seconds_total"]
        total = sum(value for _, value in cpu_rows)
        idle = sum(value for labels, value in cpu_rows if labels.get("mode") in {"idle", "iowait"})
        previous = self._cpu_sample
        new_cpu_sample = (idle, total)
        cpu_percent: float | None = None
        if previous is not None and total > previous[1] and idle >= previous[0]:
            cpu_percent = round(100 * (1 - (idle - previous[0]) / (total - previous[1])), 1)
            cpu_percent = min(100.0, max(0.0, cpu_percent))

        memory_total = self._single(samples, "node_memory_MemTotal_bytes")
        memory_available = self._single(samples, "node_memory_MemAvailable_bytes")
        if memory_total <= 0 or not 0 <= memory_available <= memory_total:
            raise ValueError("invalid memory metrics")
        memory_used = memory_total - memory_available
        memory_percent = round(100 * memory_used / memory_total, 1)

        disk_candidates = [
            (labels, value)
            for labels, value in samples["node_filesystem_size_bytes"]
            if labels.get("mountpoint") == "/"
            and labels.get("fstype") not in {"", "tmpfs", "devtmpfs", "overlay", "squashfs"}
        ]
        if len(disk_candidates) != 1:
            raise ValueError("expected one root filesystem")
        disk_labels, disk_total = disk_candidates[0]
        disk_available_rows = [
            value
            for labels, value in samples["node_filesystem_avail_bytes"]
            if labels.get("mountpoint") == "/"
            and labels.get("device") == disk_labels.get("device")
            and labels.get("fstype") == disk_labels.get("fstype")
        ]
        if len(disk_available_rows) != 1 or disk_total <= 0:
            raise ValueError("invalid root filesystem metrics")
        disk_available = disk_available_rows[0]
        if not 0 <= disk_available <= disk_total:
            raise ValueError("invalid disk availability")
        disk_used = disk_total - disk_available
        disk_percent = round(100 * disk_used / disk_total, 1)

        loads = [
            self._single(samples, "node_load1"),
            self._single(samples, "node_load5"),
            self._single(samples, "node_load15"),
        ]
        core_count = len({labels.get("cpu") for labels, _ in cpu_rows}) or 1
        load_status = (
            "critical"
            if loads[0] >= 2 * core_count
            else "attention"
            if loads[0] >= core_count
            else "healthy"
        )
        network_rx = sum(
            value
            for labels, value in samples["node_network_receive_bytes_total"]
            if labels.get("device") != "lo"
        )
        network_tx = sum(
            value
            for labels, value in samples["node_network_transmit_bytes_total"]
            if labels.get("device") != "lo"
        )
        boot_time = self._single(samples, "node_boot_time_seconds")
        uptime = sampled_at - boot_time
        if boot_time <= 0 or uptime < 0:
            raise ValueError("invalid boot time")

        self._cpu_sample = new_cpu_sample
        return {
            "cpu": self._metric(cpu_percent, "%", self._status(cpu_percent, 80, 95)),
            "ram": self._metric(
                [int(memory_used), int(memory_total), memory_percent],
                "bytes",
                self._status(memory_percent, 80, 90),
            ),
            "disk": self._metric(
                [int(disk_used), int(disk_total), disk_percent],
                "bytes",
                self._status(disk_percent, 80, 90),
            ),
            "load": self._metric([round(value, 2) for value in loads], "load", load_status),
            "network": self._metric([int(network_rx), int(network_tx)], "bytes", "healthy"),
            "uptime": self._metric(round(uptime), "seconds", "healthy"),
            "boot_time": self._metric(round(boot_time), "epoch_seconds", "healthy"),
        }
