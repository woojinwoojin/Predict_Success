"""지표 엔진 규칙(docs/decisions.md D9) 테스트."""

from decimal import Decimal

import pytest

from src.metrics.engine import (
    Input, current_ratios, interest_coverage, midrank_percentile, ocf_to_avg_assets, operating_margin, peer_relative,
    revenue_cagr_3y, growth_volatility,
)


def getter(values: dict, statuses: dict | None = None):
    statuses = statuses or {}

    def get(account, year):
        status = statuses.get((account, year), "mapped" if (account, year) in values else "review_needed")
        v = values.get((account, year))
        return Input(account, year, status, None if v is None else Decimal(str(v)), "test", "note")
    return get


def test_normal_inputs_give_normal_result():
    r = ocf_to_avg_assets(getter({("operating_cash_flow", 2025): 10, ("total_assets", 2024): 90, ("total_assets", 2025): 110}))
    assert (r.calc_status, r.value, r.score_eligible) == ("normal", 0.1, "가능")


def test_candidate_input_gives_reference_calculation_and_holds_score():
    get = getter({("operating_income", 2025): 400, ("interest_expense", 2025): 100}, {("interest_expense", 2025): "candidate"})
    r = interest_coverage(get)
    assert (r.calc_status, r.value, r.score_eligible) == ("reference", 4.0, "보류")
    assert any("후보값" in h for h in r.hold_reasons)


def test_unconfirmed_input_is_not_computed_with_reason():
    r = interest_coverage(getter({("operating_income", 2025): 400}))
    assert (r.calc_status, r.value) == ("not_computed", None)
    assert any("review_needed" in h for h in r.hold_reasons)


def test_zero_current_liabilities_is_never_infinite():
    get = getter({("adjusted_current_assets", 2025): 5, ("adjusted_current_liabilities", 2025): 0,
                  ("current_assets", 2025): 5, ("current_liabilities", 2025): 0})
    hfs, reported = current_ratios(get)
    assert hfs.value is None and hfs.calc_status == "not_computed"
    assert any("무한대" in h for h in hfs.hold_reasons)


def test_reported_current_ratio_is_view_only_and_hfs_ratio_is_the_scoring_one():
    get = getter({("adjusted_current_assets", 2025): 6, ("adjusted_current_liabilities", 2025): 4,
                  ("current_assets", 2025): 5, ("current_liabilities", 2025): 4})
    hfs, reported = current_ratios(get)
    assert (hfs.value, hfs.score_eligible) == (1.5, "가능")
    assert (reported.value, reported.score_eligible) == (1.25, "아니오 — 조회용")


def test_cagr_uses_2022_and_2025_and_refuses_non_positive_start():
    r = revenue_cagr_3y(getter({("revenue", 2022): 1000, ("revenue", 2025): 1331}))
    assert r.value == pytest.approx(0.10)
    r = revenue_cagr_3y(getter({("revenue", 2022): 0, ("revenue", 2025): 1331}))
    assert r.calc_status == "not_computed"


def test_three_year_decline_is_not_called_stable():
    r = growth_volatility(getter({("revenue", 2022): 1000, ("revenue", 2023): 900, ("revenue", 2024): 810, ("revenue", 2025): 729}))
    assert r.value == pytest.approx(0.0, abs=1e-12)
    assert any("안정" in n for n in r.notes)


# ---------------------------------------------------------------- 비교군


def own_margins(margins: dict):
    out = {}
    for corp, m in margins.items():
        r = operating_margin(getter({("operating_income", 2025): m, ("revenue", 2025): 100}))
        base = {"revenue_cagr_3y": r, "operating_margin": r, "roa": r, "inventory_turnover": r}
        out[corp] = base
    return out


def test_peer_median_excludes_the_company_itself_and_is_validation_only():
    own = own_margins({"a": 10, "b": 20, "c": 30, "d": 40})
    peer = peer_relative(own, ["a", "b", "c", "d"], "파일럿")
    gap = next(r for r in peer["a"] if r.metric == "operating_margin_gap")
    assert gap.value == pytest.approx(0.10 - 0.30)  # b·c·d의 중앙값 0.30
    assert gap.score_eligible.startswith("검증용")
    assert gap.peer_percentile_validation == 0.0


def test_excluded_company_gets_no_peer_score_not_zero():
    own = own_margins({"a": 10, "b": 20, "c": 30, "k": 50})
    peer = peer_relative(own, ["a", "b", "c"], "파일럿")
    for r in peer["k"]:
        assert r.value is None  # 0점이 아니다
        assert r.score_eligible.startswith("해당 없음")


def test_midrank_percentile_matches_readme_formula():
    assert midrank_percentile(2, [1, 2, 3]) == pytest.approx(100 * (1 + 0.5) / 3)
    assert midrank_percentile(2, [1, 2, 3], higher_is_better=False) == pytest.approx(100 - 50)
