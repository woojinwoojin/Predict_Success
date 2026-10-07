"""점수 변환과 결측·해당 없음 집계 (README 7.1·7.3, docs/decisions.md D12).

계산 상태(지표 엔진)와 점수 상태(여기)를 분리한다:
  scored          점수 반영 — 확인된 기여도 K에 들어간다
  validation_only 검증용 — 비교군 n < 20 (A 방식). 점수는 추적용으로만 계산
  held            보류 — 입력이 참고 계산·계산 안 함. 참고 계산이면 참고 점수만 보여준다
  not_applicable  해당 없음 — 비교군 제외 등. 0점이 아니고 배점을 재분배하지 않는다
  view_only       조회용 — 배점 0
  not_implemented 미구현 — 시장 영역 등
"""

from bisect import bisect_right


def interpolate(x: float, points: list[list[float]]) -> float:
    """고정 구간 선형 보간. 경계점 사이는 직선, 양 끝 밖은 끝점 점수로 고정 (README 7.1 B)."""
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    if x <= xs[0]:
        return float(ys[0])
    if x >= xs[-1]:
        return float(ys[-1])
    i = bisect_right(xs, x) - 1
    x0, x1, y0, y1 = xs[i], xs[i + 1], ys[i], ys[i + 1]
    return y0 + (y1 - y0) * (x - x0) / (x1 - x0)


def raw_score(metric: dict, rule: dict) -> float | None:
    """변환 전 값 → 0~100 점수. 값이 없으면 None. CAGR 기준 상한은 score_row에서 적용한다."""
    if metric.get("value") in (None, ""):
        return None
    if rule["method"] == "B":
        s = interpolate(float(metric["value"]), rule["points"])
        # v1 규칙(보존): 3년 연속 감소면 상한 — v2에서 반례 때문에 CAGR 음수 기준으로 바뀌었다
        if rule.get("cap_if_all_decline") is not None and "3년 연속 감소" in (metric.get("notes") or ""):
            s = min(s, rule["cap_if_all_decline"])
        return s
    if rule["method"] == "A":
        p = metric.get("peer_percentile_validation")
        if p in (None, ""):
            return None
        s = float(p) if rule.get("direction") != "lower" else 100 - float(p)
        return min(s, rule["cap"]) if rule.get("cap") is not None else s
    raise ValueError(f"알 수 없는 변환 방식: {rule['method']}")


def cagr_cap(metric: dict, rule: dict, context: dict | None) -> tuple[float | None, list[str], str | None]:
    """v2 변동성 상한: 3년 CAGR이 음수면 최대 cap점. (상한 또는 None, 경고, 보류 사유)."""
    warnings = []
    if rule.get("warn_if_all_decline") and "3년 연속 감소" in (metric.get("notes") or ""):
        warnings.append("3년 연속 매출 감소")
    if rule.get("cap_if_negative_cagr") is None:
        return None, warnings, None
    cagr = (context or {}).get("revenue_cagr_3y")
    if not cagr or cagr.get("calc_status") != "정상" or cagr.get("value") in (None, ""):
        return None, warnings, "3년 CAGR 부호 미확인 — 변동성 상한 적용 여부를 정할 수 없음"
    value = float(cagr["value"])
    if abs(value) < 0.01:
        warnings.append(f"CAGR {value:+.2%} — 변동성 상한 적용 경계(0%) 근처")
    return (rule["cap_if_negative_cagr"] if value < 0 else None), warnings, None


def score_row(metric: dict, rule: dict | None, scoring: dict, view_only: set, context: dict | None = None) -> dict:
    """지표 하나의 점수 상태·점수·기여도. context는 같은 기업의 다른 지표 (변동성 상한에 CAGR이 필요)."""
    name = metric["metric"]
    if name in view_only:
        return {"score_status": "view_only", "weight": 0, "score": None, "trace_score": None, "contribution": None,
                "score_reason": "조회용 지표 — 배점 0", "score_warnings": ""}
    if rule is None:
        return {"score_status": "view_only", "weight": 0, "score": None, "trace_score": None, "contribution": None,
                "score_reason": "점수 규칙 없음 (자체 원시값 — 비교군 지표의 바탕)", "score_warnings": ""}
    w = rule["weight"]
    eligible = metric.get("score_eligible") or ""
    calc = metric.get("calc_status")
    trace = raw_score(metric, rule) if metric.get("value") not in (None, "") else None
    cap, warnings, cap_problem = cagr_cap(metric, rule, context)
    if trace is not None and cap is not None:
        trace = min(trace, cap)
    base = {"weight": w, "trace_score": trace, "score": None, "contribution": None, "score_warnings": "; ".join(warnings)}
    if eligible.startswith("해당 없음"):
        return {**base, "score_status": "not_applicable", "score_reason": eligible}
    if calc != "정상":
        reason = "참고 계산 — 참고 점수만 표시" if calc == "참고 계산" else "계산 안 함"
        return {**base, "score_status": "held", "score_reason": f"{reason}: {metric.get('hold_reasons', '')[:120]}"}
    if rule["method"] == "A" or eligible.startswith("검증용"):
        # 비교군 크기 조건(n ≥ 20)은 지표 엔진이 '검증용'으로 표시해 넘긴다. A 방식은 그 조건을 통과해야만 반영한다
        return {**base, "score_status": "validation_only",
                "score_reason": f"비교군 n < {scoring['peer_scoring']['min_peer_size']} — 검증용 ({eligible})"}
    if not eligible.startswith("가능"):
        return {**base, "score_status": "held", "score_reason": f"점수 반영 불가: {eligible}"}
    if cap_problem:
        return {**base, "trace_score": None, "score_status": "held", "score_reason": cap_problem}
    return {**base, "score_status": "scored", "score": trace, "contribution": w * trace / 100,
            "score_reason": f"CAGR 음수 — 상한 {cap}점 적용" if cap is not None else ""}


def k_label(scoring: dict) -> str:
    if scoring["status"] != "confirmed":
        return "제안 규칙 기준 계산 기여도"
    if scoring.get("validation_status") != "validated":
        return "채택 규칙(미검증) 기준 계산 기여도"
    return "채택·검증 규칙 기준 기여도"


def coverage(rows: list[dict], scoring: dict) -> dict:
    """구조화 배점 90의 분해. 자동 재분배 없음 (README 7.3)."""
    by = lambda status: sum(r["weight"] for r in rows if r["score_status"] == status)
    k = sum(r["contribution"] for r in rows if r["score_status"] == "scored")
    scored_w, held_w, valid_w, na_w, unimpl_w = by("scored"), by("held"), by("validation_only"), by("not_applicable"), by("not_implemented")
    total = scoring["structured_total"]
    assert abs(scored_w + held_w + valid_w + na_w + unimpl_w - total) < 1e-9, "배점 분해 합이 90이 아님"
    unconfirmed = held_w + valid_w + unimpl_w
    cov = scored_w / total
    status = "충분" if cov == 1 else ("부분 평가" if cov >= 0.8 else "보류")
    return {
        "K_label": k_label(scoring), "K_contribution": round(k, 4), "K_unrounded": k, "scored_weight": scored_w,
        "unconfirmed_weight": unconfirmed, "held_weight": held_w, "validation_only_weight": valid_w, "not_implemented_weight": unimpl_w,
        "not_applicable_weight": na_w,
        # 범위 상한은 배점 기준의 느슨한 상한 (유동비율·재고회전율 상한 90 등을 반영한 정밀한 최대치가 아님)
        "range_low": round(k, 4) if not na_w else None, "range_high_loose": round(k + unconfirmed, 4) if not na_w else None,
        "weighted_coverage": round(cov, 4), "coverage_status": status,
        "composite_score": None,
        "composite_hold_reasons": "; ".join(filter(None, [
            f"점수 규칙 {scoring['status']} (미확정)" if scoring["status"] != "confirmed" else "",
            "점수 규칙 미검증 (채택만 됨)" if scoring["status"] == "confirmed" and scoring.get("validation_status") != "validated" else "",
            f"시장 영역 미구현 ({unimpl_w})" if unimpl_w else "",
            f"비교군 n < {scoring['peer_scoring']['min_peer_size']} ({valid_w})" if valid_w else "",
            f"해당 없음 배점 {na_w} — 범위·종합점수 산출 안 함" if na_w else "",
            f"가중 커버리지 {cov:.0%} → {status}" if status != "충분" else "",
            "AI 평가(10) 미제공",
        ])),
    }
