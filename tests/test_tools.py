import pytest

from src.tools import score_priority


def test_structural_risk_4_floors_p0_even_with_low_impact():
    r = score_priority(behavioral_impact=1, structural_risk=4)
    assert r["priority"] == "P0"
    assert r["driver"] == "structural_risk"


def test_high_impact_local_patch_is_p1_driven_by_behavior():
    r = score_priority(behavioral_impact=4, structural_risk=2)
    assert r["priority"] == "P1"
    assert r["driver"] == "behavioral_impact"


def test_max_impact_alone_reaches_p0():
    assert score_priority(behavioral_impact=5, structural_risk=1)["priority"] == "P0"


def test_low_on_both_axes_is_p3():
    assert score_priority(behavioral_impact=2, structural_risk=2)["priority"] == "P3"


def test_out_of_range_is_rejected():
    with pytest.raises(ValueError):
        score_priority(behavioral_impact=6, structural_risk=1)
