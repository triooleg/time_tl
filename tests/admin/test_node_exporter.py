from __future__ import annotations

import unittest

from time_tl.admin.node_exporter import NodeExporterMetricsCollector


def fixture(cpu_idle: int = 1000, cpu_user: int = 100) -> bytes:
    return f"""# HELP fixture fixture
node_cpu_seconds_total{{cpu=\"0\",mode=\"idle\"}} {cpu_idle}
node_cpu_seconds_total{{cpu=\"0\",mode=\"user\"}} {cpu_user}
node_cpu_seconds_total{{cpu=\"0\",mode=\"iowait\"}} 10
node_cpu_seconds_total{{cpu=\"1\",mode=\"idle\"}} {cpu_idle}
node_cpu_seconds_total{{cpu=\"1\",mode=\"user\"}} {cpu_user}
node_cpu_seconds_total{{cpu=\"1\",mode=\"iowait\"}} 10
node_memory_MemTotal_bytes 1000
node_memory_MemAvailable_bytes 250
node_filesystem_size_bytes{{device=\"/dev/sda1\",fstype=\"ext4\",mountpoint=\"/\"}} 2000
node_filesystem_avail_bytes{{device=\"/dev/sda1\",fstype=\"ext4\",mountpoint=\"/\"}} 500
node_filesystem_size_bytes{{device=\"overlay\",fstype=\"overlay\",mountpoint=\"/\"}} 99
node_filesystem_avail_bytes{{device=\"overlay\",fstype=\"overlay\",mountpoint=\"/\"}} 1
node_load1 1.5
node_load5 1.0
node_load15 0.5
node_network_receive_bytes_total{{device=\"eth0\"}} 111
node_network_receive_bytes_total{{device=\"lo\"}} 999
node_network_transmit_bytes_total{{device=\"eth0\"}} 222
node_network_transmit_bytes_total{{device=\"lo\"}} 999
node_boot_time_seconds 900
""".encode()


class NodeExporterMetricsTests(unittest.TestCase):
    def test_parses_host_metrics_and_cpu_requires_two_samples(self) -> None:
        payloads = iter((fixture(), fixture(cpu_idle=1010, cpu_user=110)))
        calls: list[tuple[str, float, int]] = []

        def fetch(url: str, timeout: float, maximum: int) -> bytes:
            calls.append((url, timeout, maximum))
            return next(payloads)

        collector = NodeExporterMetricsCollector(
            "http://node-exporter:9100/metrics", fetcher=fetch, clock=lambda: 1000
        )
        first = collector.collect()
        second = collector.collect()

        self.assertIsNone(first["cpu"]["value"])
        self.assertEqual(second["cpu"]["value"], 50.0)
        self.assertEqual(second["ram"]["value"], [750, 1000, 75.0])
        self.assertEqual(second["disk"]["value"], [1500, 2000, 75.0])
        self.assertEqual(second["load"]["value"], [1.5, 1.0, 0.5])
        self.assertEqual(second["network"]["value"], [111, 222])
        self.assertEqual(second["boot_time"]["value"], 900)
        self.assertEqual(second["uptime"]["value"], 100)
        self.assertEqual(second["source"], "prometheus-node-exporter")
        self.assertEqual(second["source_status"], "available")
        self.assertEqual(calls[0], ("http://node-exporter:9100/metrics", 2.0, 1_048_576))

    def test_failure_is_unavailable_not_zero(self) -> None:
        def fail(_url: str, _timeout: float, _maximum: int) -> bytes:
            raise OSError("offline")

        value = NodeExporterMetricsCollector(
            "http://node-exporter:9100/metrics", fetcher=fail, clock=lambda: 1000
        ).collect()
        for name in ("cpu", "ram", "disk", "load", "network", "uptime", "boot_time"):
            self.assertIsNone(value[name]["value"])
            self.assertEqual(value[name]["status"], "unavailable")
        self.assertEqual(value["source_status"], "unavailable")

    def test_partial_or_oversized_payload_fails_closed(self) -> None:
        partial = NodeExporterMetricsCollector(
            "http://node-exporter:9100/metrics",
            fetcher=lambda *_: b"node_load1 1\n",
            clock=lambda: 1000,
        ).collect()
        self.assertEqual(partial["source_status"], "unavailable")

        oversized = NodeExporterMetricsCollector(
            "http://node-exporter:9100/metrics",
            fetcher=lambda _url, _timeout, maximum: b"x" * (maximum + 1),
            max_response_bytes=16_384,
            clock=lambda: 1000,
        ).collect()
        self.assertEqual(oversized["source_status"], "unavailable")

    def test_invalid_scrape_does_not_poison_cpu_baseline(self) -> None:
        invalid = b"""node_cpu_seconds_total{cpu=\"0\",mode=\"idle\"} 1000
node_cpu_seconds_total{cpu=\"0\",mode=\"user\"} 100
"""
        payloads = iter((invalid, fixture(), fixture(cpu_idle=1010, cpu_user=110)))
        collector = NodeExporterMetricsCollector(
            "http://node-exporter:9100/metrics",
            fetcher=lambda *_: next(payloads),
            clock=lambda: 1000,
        )

        self.assertEqual(collector.collect()["source_status"], "unavailable")
        self.assertIsNone(collector.collect()["cpu"]["value"])
        self.assertEqual(collector.collect()["cpu"]["value"], 50.0)

    def test_rejects_public_or_ambiguous_endpoint(self) -> None:
        rejected = (
            "https://example.com/metrics",
            "http://user:pass@node-exporter:9100/metrics",
            "http://node-exporter:9100/other",
            "http://node-exporter:9100/metrics?target=other",
        )
        for url in rejected:
            with self.subTest(url=url), self.assertRaises(ValueError):
                NodeExporterMetricsCollector(url)


if __name__ == "__main__":
    unittest.main()
