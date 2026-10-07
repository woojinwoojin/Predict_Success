"""점수 변환·집계 규칙(docs/decisions.md D12) 테스트. 규칙 파일은 실제 configs/scoring_v1.yaml을 쓴다."""

from pathlib import Path

import pytest
import yaml

from src.scoring.score import coverage, interpolate, raw_score, score_row

SCORING = yaml.safe_load((Path(__file__).resolve().parents[1] / "configs" / "scoring_v1.yaml").read_text(encoding="utf-8"))
RULES = SCORING["metrics"]
VIEW = set(SCORING["view_only"]) | set(SCORING["raw_only"])


def metric(name, value, calc="정상", eligible="가능", notes="", pct=None):
    return {"metric": name, "value": None if value is None else str(value), "calc_status": calc, "score_eligible": eligible,
            "notes": notes, "hold_reasons": "", "peer_percentile_validation": None if pct is None else str(pct)}


# ---------------------------------------------------------------- 규칙 파일


def test_weights_add_up_to_readme_areas():
    by_area = {}
    for rule in RULES.values():
        by_area[rule["area"]] = by_area.get(rule["area"], 0) + rule["weight"]
    assert by_area == {"financial_stability": 35, "growth": 20, "operations": 20}
    assert sum(by_area.values()) + SCORING["areas"]["market"]["weight"] == SCORING["structured_total"] == 90


def test_rules_are_marked_proposed_until_reviewed():
    assert SCORING["status"] in ("proposed", "confirmed")
    if SCORING["status"] == "confirmed":
        assert SCORING["confirmed_at"] and SCORING["reviewed_by"]


def test_interval_points_are_increasing():
    for name, rule in RULES.items():
        if rule["method"] == "B":
            xs = [p[0] for p in rule["points"]]
            assert xs == sorted(xs) and len(set(xs)) == len(xs), name


# ---------------------------------------------------------------- 변환


def test_linear_interpolation_and_clamping():
    points = [[0.0, 0], [0.05, 50], [0.10, 80]]
    assert interpolate(0.025, points) == pytest.approx(25)
    assert interpolate(-1, points) == 0      # 끝점 밖은 끝점 점수
    assert interpolate(9, points) == 80


def test_lower_is_better_rule_scores_high_for_low_debt():
    assert raw_score(metric("debt_ratio", 0.1), RULES["debt_ratio"]) == 100
    assert raw_score(metric("debt_ratio", 0.5), RULES["debt_ratio"]) == pytest.approx(65)
    assert raw_score(metric("debt_ratio", 1.2), RULES["debt_ratio"]) == 0


def test_excess_liquidity_gets_no_extra_points():
    assert raw_score(metric("current_ratio_hfs", 5.0), RULES["current_ratio_hfs"]) == 90


def test_three_year_decline_caps_volatility_score():
    stable_decline = metric("growth_volatility", 0.0, notes="3년 연속 감소 — 변동성이 낮아도 '안정'으로 표현하지 않음")
    assert raw_score(stable_decline, RULES["growth_volatility"]) == 30
    assert raw_score(metric("growth_volatility", 0.0), RULES["growth_volatility"]) == 100


# ---------------------------------------------------------------- 계산 상태와 점수 상태 분리


def test_normal_calculation_is_scored():
    r = score_row(metric("ocf_to_avg_assets", 0.05), RULES["ocf_to_avg_assets"], SCORING, VIEW)
    assert (r["score_status"], r["score"], r["contribution"]) == ("scored", 50, 5.0)


def test_peer_metric_is_validation_only_even_when_computed():
    r = score_row(metric("operating_margin_gap", 0.02, eligible="검증용 — 파일럿", pct=66.7), RULES["operating_margin_gap"], SCORING, VIEW)
    assert r["score_status"] == "validation_only"
    assert r["contribution"] is None and r["trace_score"] == pytest.approx(66.7)


def test_reference_calculation_shows_trace_score_but_is_held():
    r = score_row(metric("interest_coverage", 4.19, calc="참고 계산", eligible="보류"), RULES["interest_coverage"], SCORING, VIEW)
    assert r["score_status"] == "held" and r["contribution"] is None
    assert r["trace_score"] is not None


def test_view_only_and_raw_only_metrics_have_no_weight():
    assert score_row(metric("current_ratio_reported", 1.0), None, SCORING, VIEW)["weight"] == 0
    assert score_row(metric("operating_margin", 0.1), None, SCORING, VIEW)["weight"] == 0


def test_excluded_peer_metric_is_not_applicable_not_zero():
    r = score_row(metric("roa_gap", None, calc="계산 안 함", eligible="해당 없음 — 비교군 제외"), RULES["roa_gap"], SCORING, VIEW)
    assert r["score_status"] == "not_applicable" and r["score"] is None and r["contribution"] is None


# ---------------------------------------------------------------- 결측·해당 없음 집계


def rows_for(statuses: dict):
    """{metric: status} → 집계용 행 (반영은 점수 100으로)."""
    out = []
    for name, status in statuses.items():
        w = RULES[name]["weight"]
        out.append({"metric": name, "weight": w, "score_status": status, "contribution": w if status == "scored" else None})
    out.append({"metric": "market_area", "weight": 15, "score_status": "not_implemented", "contribution": None})
    return out


def test_coverage_never_redistributes_weights():
    statuses = {n: "scored" for n in RULES}
    statuses.update({"interest_coverage": "held", "growth_gap": "validation_only", "operating_margin_gap": "validation_only",
                     "roa_gap": "validation_only", "inventory_turnover_rel": "validation_only"})
    c = coverage(rows_for(statuses), SCORING)
    assert c["K_confirmed_contribution"] == c["scored_weight"] == 90 - 15 - 7 - 27  # 반영 배점만큼만, 빠진 배점이 다른 지표로 가지 않는다
    assert c["range_high"] == pytest.approx(c["range_low"] + 15 + 7 + 27)
    assert c["composite_score"] is None


def test_not_applicable_weight_blocks_range_and_composite():
    statuses = {n: "scored" for n in RULES}
    statuses.update({k: "not_applicable" for k in ("growth_gap", "operating_margin_gap", "roa_gap", "inventory_turnover_rel")})
    c = coverage(rows_for(statuses), SCORING)
    assert c["not_applicable_weight"] == 27
    assert c["range_low"] is None and c["range_high"] is None
    assert "해당 없음" in c["composite_hold_reasons"]
