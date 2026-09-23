from __future__ import annotations

import hashlib
import unittest

from time_tl.runtime.region_registry_full import (
    ALLOWED_TYPES,
    DATA_PATH,
    EXPECTED_RECORD_COUNT,
    EXPECTED_SHA256,
    EXPECTED_TOP_LEVEL_COUNT,
    WIRE_RUNTIME,
    HierarchyUnavailableError,
    assert_row_order_hierarchy_is_unambiguous,
    load_catalog,
    wire_runtime,
)


class FullRegionRegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = load_catalog()

    def test_pinned_source_hash_and_record_count(self) -> None:
        self.assertEqual(EXPECTED_SHA256, hashlib.sha256(DATA_PATH.read_bytes()).hexdigest())
        self.assertEqual(EXPECTED_RECORD_COUNT, len(self.catalog.records))

    def test_uid_is_unique_and_types_are_known(self) -> None:
        uids = [record.uid for record in self.catalog.records]
        self.assertEqual(len(uids), len(set(uids)))
        self.assertEqual(
            set(record.location_type for record in self.catalog.records), ALLOWED_TYPES
        )

    def test_catalog_contains_27_top_level_locations(self) -> None:
        self.assertEqual(EXPECTED_TOP_LEVEL_COUNT, len(self.catalog.top_level))

    def test_row_order_hierarchy_is_rejected_as_ambiguous(self) -> None:
        with self.assertRaisesRegex(
            HierarchyUnavailableError,
            r"top-level source rows 5, 6, 7, 8 precede district row 9",
        ):
            assert_row_order_hierarchy_is_unambiguous(self.catalog)

    def test_runtime_wiring_stays_fail_closed(self) -> None:
        self.assertFalse(WIRE_RUNTIME)
        with self.assertRaises(HierarchyUnavailableError):
            wire_runtime()


if __name__ == "__main__":
    unittest.main()
