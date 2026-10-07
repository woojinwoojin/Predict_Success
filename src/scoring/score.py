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
    """변환 전 값 → 0~100 점수. 값이 없으면 None."""
    if metric.get("value") in (None, ""):
        return None
    if rule["method"] == "B":
        s = interpolate(float(metric["value"]), rule["points"])
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


def score_row(metric: dict, rule: dict | None, scoring: dict, view_only: set) -> dict:
    """지표 하나의 점수 상태·점수·기여도."""
    name = metric["metric"]
    if name in view_only:
        return {"score_status": "view_only", "weight": 0, "score": None, "trace_score": None, "contribution": None,
                "score_reason": "조회용 지표 — 배점 0"}
    if rule is None:
        return {"score_status": "view_only", "weight": 0, "score": None, "trace_score": None, "contribution": None,
                "score_reason": "점수 규칙 없음 (자체 원시값 — 비교군 지표의 바탕)"}
    w = rule["weight"]
    eligible = metric.get("score_eligible") or ""
    calc = metric.get("calc_status")
    trace = raw_score(metric, rule) if metric.get("value") not in (None, "") else None
    base = {"weight": w, "trace_score": trace, "score": None, "contribution": None}
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
    return {**base, "score_status": "scored", "score": trace, "contribution": w * trace / 100, "score_reason": ""}


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
        "K_confirmed_contribution": round(k, 4), "scored_weight": scored_w,
        "unconfirmed_weight": unconfirmed, "held_weight": held_w, "validation_only_weight": valid_w, "not_implemented_weight": unimpl_w,
        "not_applicable_weight": na_w,
        "range_low": round(k, 4) if not na_w else None, "range_high": round(k + unconfirmed, 4) if not na_w else None,
        "weighted_coverage": round(cov, 4), "coverage_status": status,
        "composite_score": None,
        "composite_hold_reasons": "; ".join(filter(None, [
            f"점수 규칙 {scoring['status']} (미확정)" if scoring["status"] != "confirmed" else "",
            f"시장 영역 미구현 ({unimpl_w})" if unimpl_w else "",
            f"비교군 n < {scoring['peer_scoring']['min_peer_size']} ({valid_w})" if valid_w else "",
            f"해당 없음 배점 {na_w} — 범위·종합점수 산출 안 함" if na_w else "",
            f"가중 커버리지 {cov:.0%} → {status}" if status != "충분" else "",
            "AI 평가(10) 미제공",
        ])),
    }
