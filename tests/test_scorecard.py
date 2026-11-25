from pathlib import Path

from scripts.kpi_scorecard import build

ROOT = Path(__file__).resolve().parents[1]


def test_every_measured_kpi_has_an_existing_source():
    result = build()
    for kid, e in result["kpis"].items():
        if e["status"] == "measured":
            src = e["source"].split(" ")[0]
            assert (ROOT / src).exists(), (kid, src)


def test_unmeasured_kpis_have_no_value():
    for kid, e in build()["kpis"].items():
        if e["status"] == "not measured":
            assert "value" not in e and "passes" not in e, kid


def test_all_36_kpis_listed():
    assert len(build()["kpis"]) == 36
