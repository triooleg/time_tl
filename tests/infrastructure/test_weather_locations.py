import hashlib
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from time_tl.infrastructure.db.weather_locations import (
    ReviewedWeatherDataset,
    parse_weather_dataset,
)

CONTENT = (
    b"katottg,region_id,latitude,longitude,coverage_type\n"
    b"UA12000000000090473,synthetic-community,48.5,34.6,BASE\n"
)


def approval(content=CONTENT):
    return ReviewedWeatherDataset(
        "https://example.org/reviewed-static-catalog.csv",
        hashlib.sha256(content).hexdigest(),
        "synthetic-fixture-only",
        "test-operator",
        datetime(2026, 11, 5, tzinfo=UTC),
    )


def test_only_checksum_bound_reviewed_dataset_is_accepted():
    points = parse_weather_dataset(CONTENT, approval())
    assert len(points) == 1 and points[0].region_id == "synthetic-community"
    with pytest.raises(ValueError):
        parse_weather_dataset(CONTENT + b"\n", approval())
    with pytest.raises(ValueError):
        parse_weather_dataset(CONTENT, replace(approval(), source_url="http://example.org/data"))


@pytest.mark.parametrize("replacement", ["nan", "90", "-5", "inf"])
def test_nonfinite_or_outside_ukraine_coordinates_are_rejected(replacement):
    content = CONTENT.replace(b"48.5", replacement.encode())
    with pytest.raises(ValueError):
        parse_weather_dataset(content, approval(content))


def test_duplicate_base_and_unknown_columns_fail_atomically():
    duplicate = CONTENT + CONTENT.splitlines(keepends=True)[1]
    with pytest.raises(ValueError):
        parse_weather_dataset(duplicate, approval(duplicate))
    extra = CONTENT.replace(b"coverage_type", b"coverage_type,source")
    with pytest.raises(ValueError):
        parse_weather_dataset(extra, approval(extra))
