from time_tl.application.alert_messages import (
    completed_alert_duration,
    duration_text,
    render_alert_report,
)


def test_each_clear_has_duration_or_explicit_missing_history():
    assert completed_alert_duration({}) == "⏱ Тривалість тривоги: немає повних даних"
    assert completed_alert_duration(
        {"duration_seconds": 1560, "duration_is_approximate": True}
    ) == "⏱ Тривалість тривоги: ≈26 хв"
    assert completed_alert_duration(
        {"duration_seconds": 3900, "duration_is_approximate": True}
    ).endswith("≈1 год 5 хв")


def test_bad_optional_statistics_cannot_break_critical_all_clear_rendering():
    for value in (None, True, -1, "1560", float("nan"), float("inf"), 10**50):
        assert "немає повних даних" in completed_alert_duration({"duration_seconds": value})


def test_short_observed_alert_is_not_rounded_up_to_a_minute():
    assert duration_text(15) == "менше 1 хв"
    assert duration_text(3600) == "1 год"


def test_report_explains_carryover_and_partial_22_to_22_statistics():
    report = {
        "period_start": "2026-11-06T20:00:00+00:00",
        "period_end": "2026-11-07T20:00:00+00:00",
        "alert_count": 2,
        "duration_seconds": 3900,
        "carried_over_count": 1,
        "ongoing_count": 1,
        "unknown_start_count": 1,
        "is_partial": True,
    }
    text = render_alert_report(report, "Кам'янське")
    assert "06.11 22:00 — 07.11 22:00" in text
    assert "Нових тривог: 2" in text
    assert "Зафіксована тривалість: ≈1 год 5 хв" in text
    assert "З попереднього періоду: 1" in text
    assert "Історія неповна" in text
    assert "Тривог без підтвердженого завершення: 1" in text
    assert "тривога ще триває" not in text
    assert "укрит" not in text


def test_complete_zero_report_does_not_invent_a_duration():
    report = {
        "period_start": "2026-11-06T20:00:00+00:00",
        "period_end": "2026-11-07T20:00:00+00:00",
        "alert_count": 0,
        "duration_seconds": 0,
        "carried_over_count": 0,
        "ongoing_count": 0,
        "is_partial": False,
    }
    text = render_alert_report(report, "Кам'янське")
    assert "Загальна тривалість: 0 хв" in text
    assert "Історія неповна" not in text
