"""Derive the next monitor from the exact installed Weather Local source."""

from __future__ import annotations

import argparse
import hashlib
import re
from pathlib import Path

PREDECESSOR_SHA256 = "b12904511e5786249b6e4dcffba851aba457238420424bd5d6bbf2322d4ddc78"


def upgrade_monitor(raw: bytes, release_images: tuple[str, str] | None = None) -> bytes:
    if hashlib.sha256(raw).hexdigest() != PREDECESSOR_SHA256:
        raise ValueError("Monitor predecessor digest differs")
    source = raw.decode("utf-8")

    def replace(before: str, after: str) -> None:
        nonlocal source
        if source.count(before) != 1:
            raise ValueError("Monitor predecessor anchor differs")
        source = source.replace(before, after)

    if release_images is not None:
        if len(set(release_images)) != 2 or any(
            re.fullmatch(r"localhost:5000/time-tl-bot@sha256:[a-f0-9]{64}", image) is None
            for image in release_images
        ):
            raise ValueError("Exact primary and fallback digests are required")
        identity = next(
            line
            for line in source.splitlines()
            if line.startswith("APPROVED_RUNTIME_IDENTITIES = ")
        )
        profiles = tuple((15, image, image) for image in release_images)
        replace(identity, f"APPROVED_RUNTIME_IDENTITIES = frozenset({profiles!r})")

    replace(
        '    if mapping.get(row["event"]) != row["aggregate"]:',
        '    mapping["CALENDAR_REMINDER"] = "schedule"\n'
        '    if mapping.get(row["event"]) != row["aggregate"]:',
    )
    replace(
        '    if row["error"] == "final_delivery_expired":\n'
        '        return row["event"] in {"LESSON_REMINDER", "RETURN_TO_ACTIVE_LESSON", '
        '"WEATHER_DAILY", "WEATHER_EVENTS"}',
        '    if row["error"] == "mode_generation_invalidated":\n'
        '        return row["event"] in {"ALERT_STARTED", "ALL_CLEAR_CONFIRMED", '
        '"RETURN_TO_ACTIVE_LESSON"}\n'
        '    if row["error"] == "final_delivery_expired":\n'
        '        return row["event"] in {"ALERT_STARTED", "ALL_CLEAR_CONFIRMED", '
        '"LESSON_REMINDER", "RETURN_TO_ACTIVE_LESSON", "CALENDAR_REMINDER", '
        '"WEATHER_DAILY", "WEATHER_EVENTS"}',
    )
    replace(
        '        "map_schedule_or_preferences_changed", "final_map_expired_or_stale",',
        '        "map_schedule_or_preferences_changed", "final_map_expired_or_stale",\n'
        '        "final_power_map_expired_or_stale",',
    )
    replace(
        "def atomic_write(path: Path, value: Any) -> None:",
        "def atomic_write(path: Path, value: Any, *, create_only: bool = False) -> None:",
    )
    replace(
        "        os.replace(name, path)",
        "        if create_only:\n"
        "            os.link(name, path)\n"
        "        else:\n"
        "            os.replace(name, path)\n"
        "        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)\n"
        "        try:\n"
        "            os.fsync(directory)\n"
        "        finally:\n"
        "            os.close(directory)",
    )
    replace(
        '            stopped.append(role)',
        '            result = run(["docker", "inspect", "--format",\n'
        '                          "{{.State.Running}} {{.State.Restarting}}",\n'
        '                          STACK + "-" + role + "-1"], timeout=5)\n'
        '            if result.strip() == "false false":\n'
        '                stopped.append(role)',
    )
    replace(
        'def hold(state: Path) -> int:\n'
        '    try:\n'
        '        atomic_write(\n'
        '            state / "hold.json",\n'
        '            {"mode": MODE, "status": "HOLD", "at": datetime.now(UTC).isoformat()},\n'
        '        )\n'
        '    except Exception:\n'
        '        pass\n'
        '    stop_roles()',
        'def hold(state: Path, reason: str) -> int:\n'
        '    try:\n'
        '        atomic_write(\n'
        '            state / "hold.json",\n'
        '            {"mode": MODE, "status": "HOLD", "at": datetime.now(UTC).isoformat(),\n'
        '             "reason": reason}, create_only=True,\n'
        '        )\n'
        '    except Exception:\n'
        '        pass\n'
        '    stopped = stop_roles()\n'
        '    try:\n'
        '        atomic_write(state / "hold-stop.json", {\n'
        '            "at": datetime.now(UTC).isoformat(),\n'
        '            "confirmed_stopped": stopped,\n'
        '            "unconfirmed": [role for role in ROLES if role not in stopped],\n'
        '        })\n'
        '    except Exception:\n'
        '        pass',
    )
    replace(
        "    state = TRUSTED_STATE\n    try:\n        validate_config(config)",
        '    state = TRUSTED_STATE\n    reason = "CONFIG_INVALID"\n'
        "    try:\n        validate_config(config)",
    )
    replace(
        '        if mode == "watchdog":\n',
        '        if mode == "watchdog":\n            reason = "WATCHDOG_PULSE_REJECTED"\n',
    )
    replace(
        "        sample = collect(config)\n        evaluate(config, sample)",
        '        reason = "COLLECT_REJECTED"\n'
        '        sample = collect(config)\n'
        '        reason = "EVALUATION_REJECTED"\n'
        '        evaluate(config, sample)\n'
        '        reason = "PULSE_WRITE_FAILED"',
    )
    replace("        return hold(state)", "        return hold(state, reason)")
    replace(
        "            return hold(TRUSTED_STATE)",
        '            return hold(TRUSTED_STATE, "CONFIG_UNREADABLE")',
    )
    compile(source, "upgraded-cohost-monitor", "exec")
    return source.encode("utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("predecessor", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--release-images", nargs=2, metavar=("PRIMARY", "FALLBACK"))
    args = parser.parse_args()
    images = tuple(args.release_images) if args.release_images else None
    result = upgrade_monitor(args.predecessor.read_bytes(), images)
    with args.output.open("xb") as output:
        output.write(result)


if __name__ == "__main__":
    main()
