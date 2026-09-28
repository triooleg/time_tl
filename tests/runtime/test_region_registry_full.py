from __future__ import annotations

import hashlib
import unittest
from dataclasses import replace

from time_tl.runtime.region_registry_full import (
    ALLOWED_TYPES,
    DATA_PATH,
    EXPECTED_PARENT_LINK_COUNT,
    EXPECTED_RECORD_COUNT,
    EXPECTED_SHA256,
    EXPECTED_TOP_LEVEL_COUNT,
    WIRE_RUNTIME,
    FullRegionCatalog,
    HierarchyUnavailableError,
    LocationRecord,
    RegistryValidationError,
    assert_row_order_hierarchy_is_unambiguous,
    expected_oblast_uid_by_region,
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

    def test_official_row_order_builds_complete_hierarchy(self) -> None:
        assert_row_order_hierarchy_is_unambiguous(self.catalog)
        self.assertEqual(EXPECTED_PARENT_LINK_COUNT, self.catalog.parent_link_count)
        self.assertEqual("8", self.catalog.by_uid["38"].parent_uid)
        self.assertIsNone(self.catalog.by_uid["29"].parent_uid)
        self.assertEqual("16", self.catalog.by_uid["1803"].parent_uid)
        self.assertEqual("1803", self.catalog.by_uid["1903"].parent_uid)

    def test_expected_oblast_mapping_covers_each_hierarchy_level(self) -> None:
        expected = expected_oblast_uid_by_region(self.catalog)

        self.assertEqual(set(self.catalog.by_uid), set(expected))
        self.assertEqual("16", expected["16"])
        self.assertEqual("16", expected["1803"])
        self.assertEqual("16", expected["1903"])
        self.assertEqual("31", expected["31"])

    def test_unpublished_iot_positions_are_not_invented(self) -> None:
        unknown = {str(uid) for uid in range(1805, 1815)}
        self.assertTrue(unknown.isdisjoint(self.catalog.by_uid))

    def test_alternate_parent_is_rejected_even_when_type_is_valid(self) -> None:
        records = tuple(
            replace(record, parent_uid="29") if record.uid == "38" else record
            for record in self.catalog.records
        )
        tampered = FullRegionCatalog(records, self.catalog.sha256)
        with self.assertRaisesRegex(
            RegistryValidationError,
            "disagrees with canonical provider row order",
        ):
            assert_row_order_hierarchy_is_unambiguous(tampered)

    def test_unexpected_consecutive_root_run_fails_closed(self) -> None:
        catalog = FullRegionCatalog(
            (
                LocationRecord("9", "Перша область", "Область", "", 1),
                LocationRecord("20", "Друга область", "Область", "", 2),
                LocationRecord("42", "Тестовий район", "Район", "", 3, "9"),
            ),
            "a" * 64,
        )

        with self.assertRaisesRegex(
            RegistryValidationError,
            "ambiguous consecutive root run",
        ):
            assert_row_order_hierarchy_is_unambiguous(catalog)

    def test_runtime_wiring_stays_fail_closed(self) -> None:
        self.assertFalse(WIRE_RUNTIME)
        with self.assertRaises(HierarchyUnavailableError):
            wire_runtime()


if __name__ == "__main__":
    unittest.main()
