"""Produtividade da bancada: tempo em turno, pausas, tempo ativo e paradas."""

from datetime import UTC, datetime, timedelta

from app.services import productivity as p

T = datetime(2026, 10, 27, 11, 0, tzinfo=UTC)  # 8h em Brasília
m = lambda minutes: T + timedelta(minutes=minutes)  # noqa: E731


def test_shift_time_discounts_pauses():
    events = [("shift_start", m(0)), ("pause", m(240)), ("resume", m(300)), ("shift_end", m(480))]
    worked, paused, state, since = p.shift_time(events, m(600))
    assert worked == timedelta(minutes=420) and paused == timedelta(minutes=60) and state is None


def test_open_shift_counts_until_now_and_ignores_invalid_order():
    events = [("pause", m(5)), ("shift_start", m(10)), ("resume", m(20))]  # pausa antes de começar e volta sem pausa: ignorados
    worked, paused, state, since = p.shift_time(events, m(70))
    assert worked == timedelta(minutes=60) and paused == timedelta(0) and state == "working" and since == m(10)


def test_active_time_splits_on_ten_minute_gaps():
    # 0-8 min ativo (bipagens a cada 2 min), parada de 30 min, uma bipagem sozinha conta 1 min
    moments = [m(0), m(2), m(4), m(6), m(8), m(38)]
    assert p.active_time(moments) == timedelta(minutes=9)
    assert p.active_time([]) == timedelta(0)


def test_longest_gap_only_counts_stops():
    gap = p.longest_gap([m(0), m(5), m(50), m(55)])
    assert gap["seconds"] == 45 * 60 and gap["start"] == m(5)
    assert p.longest_gap([m(0), m(5), m(9)]) is None


def test_person_metrics_cost_per_package():
    person = p.Person("u1", "Ana", "ana@x", hourly_cost=20)
    person.shift = timedelta(hours=2)
    person.activity = [m(0), m(5), m(9)]
    person.inspections = [{"at": m(i * 5), "seconds": s} for i, s in enumerate([30, 40, 50, 60])]
    out = person.metrics()
    assert out["inspections"] == 4 and out["per_hour"] == 2.0 and out["cost_per_package"] == 10.0
    assert out["median_seconds"] == 45 and round(out["utilization"], 3) == round(9 / 120, 3)


def test_productivity_template_compiles():
    from app.ui.routes import templates

    templates.env.get_template("produtividade.html")
    templates.env.get_template("lotes.html")
