"""관측값 → 내부 계산용 계정(canonical_account) 매핑과 "기업 × 필요한 계정" 점검표 (docs/decisions.md D6).

규칙은 configs/account_map_v1.yaml에 있다. 원본 account_id는 바꾸지 않는다.
상태: mapped(매핑 성공) / candidate(계산용 후보, 점수 보류) / review_needed(원문 확인 필요) / no_data(자료 없음) /
      empty_value(값 없음) / held(기준일 버전 없음, 보류) / not_applicable(해당 없음)
"""

from decimal import Decimal
from pathlib import Path

import yaml


def load_rules(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def is_nonstandard(account_id, config: dict) -> bool:
    return account_id in (config["nonstandard_account_id"], "", None) or account_id != account_id  # NaN


def rule_matches(rule: dict, row: dict, config: dict) -> bool:
    if row["sj_div"] not in config["statements"][rule["statement"]]:
        return False
    if "account_detail" in rule and row["account_detail"] != rule["account_detail"]:
        return False
    if "section" in rule:  # 소계 합으로 확인된 구간 조건 (D8)
        if rule["section"] == "unconfirmed":
            if row.get("bs_section_sum_ok"):
                return False
        elif not (row.get("bs_section") == rule["section"] and row.get("bs_section_sum_ok")):
            return False
    if "account_id" in rule:
        return row["account_id"] == rule["account_id"]
    if "nonstandard_name" in rule:  # 완전히 같은 이름만 (유사도 금지)
        return is_nonstandard(row["account_id"], config) and str(row["account_nm"]).strip() == rule["nonstandard_name"]
    raise ValueError(f"규칙에 account_id나 nonstandard_name이 없습니다: {rule}")


def matching(rows: list[dict], rules: list[dict], config: dict) -> list[tuple[dict, dict]]:
    return [(row, rule) for row in rows for rule in rules if rule_matches(rule, row, config)]


def evidence(pairs: list[tuple[dict, dict]], kind: str) -> list[dict]:
    return [{"kind": kind, "row_id": r["row_id"], "sj_div": r["sj_div"], "account_id": r["account_id"], "account_nm": r["account_nm"],
             "account_detail": r["account_detail"], "raw_amount": r["raw_amount"], "value_status": r["value_status"],
             "rule": rule.get("account_id") or f"name:{rule.get('nonstandard_name')}", "why": rule.get("why", "")}
            for r, rule in pairs]


PERIOD_FIELDS = ("period_type", "period_start", "period_end", "period_status", "period_is_annual")
PERIOD_OK = {"from_filing_text", "from_manual"}


def period_of(rows: list[dict]) -> tuple[dict, str | None]:
    """매핑에 쓴 행들의 기간. 확인되지 않았거나 행끼리 다르면 사유를 돌려준다 (D11 ⑥)."""
    if not rows:
        return {k: None for k in PERIOD_FIELDS}, None
    periods = {tuple(r.get(k) for k in PERIOD_FIELDS) for r in rows}
    period = dict(zip(PERIOD_FIELDS, next(iter(periods))))
    bad = [r for r in rows if r.get("period_status") not in PERIOD_OK]
    if bad:
        return period, "기간 미확인: " + ", ".join(f"{r['account_nm']} ({r.get('period_status')})" for r in bad)
    if len(periods) > 1:
        return period, "구성 항목의 기간이 서로 다름"
    return period, None


def map_account(name: str, spec: dict, rows: list[dict], config: dict) -> dict:
    """한 보고서(당기 열)의 행들에서 내부 계정 하나를 찾는다."""
    found = matching(rows, spec.get("match", []), config)
    reviews = matching(rows, spec.get("review", []), config)
    forbidden = matching(rows, spec.get("forbidden", []), config)
    pending = matching(rows, spec.get("policy_pending", []), config)
    base = {"canonical_account": name, "label": spec["label"], "basis": spec.get("basis", "")}
    notes = []
    if forbidden:
        notes.append("대체 금지 항목 있음: " + ", ".join(sorted({r["account_nm"] for r, _ in forbidden})))
    if pending:
        notes.append("정책 미정 항목(합계 제외): " + ", ".join(f"{r['account_nm']} {r['raw_amount']}" for r, _ in pending))
    ev = evidence(found, "match") + evidence(reviews, "review") + evidence(forbidden, "forbidden") + evidence(pending, "policy_pending")

    if spec.get("composite"):
        ids = {r["account_id"] for r, _ in found}
        overlapping = [(total, parts) for total, parts in spec.get("overlaps", []) if total in ids and ids & set(parts)]
        if overlapping:
            return {**base, "status": "review_needed", "value": None, "evidence": ev,
                    "note": "; ".join([f"합계 항목과 구성 항목이 함께 있어 중복 가능: {total} ⊃ {sorted(ids & set(parts))}" for total, parts in overlapping] + notes)}
        if reviews:
            return {**base, "status": "review_needed", "value": None, "evidence": ev,
                    "note": "; ".join(["확인 필요 후보: " + ", ".join(sorted({r["account_nm"] for r, _ in reviews}))] + notes)}
        if not found:
            return {**base, "status": "review_needed", "value": None, "evidence": ev,
                    "note": "; ".join(["구성 항목 없음 — 0으로 확정하려면 원문 확인"] + notes)}
        if any(r["value_status"] != "parsed" for r, _ in found):
            return {**base, "status": "review_needed", "value": None, "evidence": ev,
                    "note": "; ".join(["구성 항목 중 값 없음·변환 실패"] + notes)}
        period, period_problem = period_of([r for r, _ in found])
        if period_problem:
            return {**base, **period, "status": "review_needed", "value": None, "evidence": ev, "note": "; ".join([period_problem] + notes)}
        total = sum(Decimal(r["value"]) for r, _ in found)
        # 찾은 구성 항목의 합계일 뿐 완전성은 확인되지 않았다 — 묶음 행(기타유동부채 등) 안의 이자부채를 API 규칙은 모른다.
        # 완전성이 확인된 합계(수기 확인표 component_check = 확인)만 정상값이 된다 (D11 ⑤)
        status = "candidate" if spec.get("requires_completeness") else "mapped"
        return {**base, **period, "status": status, "value": str(total), "evidence": ev,
                "note": "; ".join([f"찾은 구성 항목 합계 {len(found)}개: " + ", ".join(r["account_nm"] for r, _ in found)
                                   + (" — 완전성 미확인" if status == "candidate" else "")] + notes)}

    if len(found) == 1:
        row = found[0][0]
        period, period_problem = period_of([row])
        if period_problem and row["value_status"] == "parsed":
            return {**base, **period, "status": "review_needed", "value": None, "evidence": ev, "note": "; ".join([period_problem] + notes)}
        status = {"parsed": "mapped", "empty_in_source": "empty_value"}.get(row["value_status"], "review_needed")
        return {**base, **period, "status": status, "value": row["value"], "evidence": ev, "note": "; ".join(notes)}
    if len(found) > 1:
        return {**base, "status": "review_needed", "value": None, "evidence": ev, "note": "; ".join([f"후보 {len(found)}개 — 자동 선택하지 않음"] + notes)}
    if reviews:
        return {**base, "status": "review_needed", "value": None, "evidence": ev,
                "note": "; ".join(["본문 규칙에 맞는 행 없음, 확인 필요 후보: " + ", ".join(sorted({r["account_nm"] for r, _ in reviews}))] + notes)}
    if spec.get("absent_ok"):
        return {**base, "status": "not_applicable", "value": None, "evidence": ev, "note": "; ".join(["해당 행 없음"] + notes)}
    return {**base, "status": "review_needed", "value": None, "evidence": ev, "note": "; ".join(["해당 계정 행 없음 — 원문 확인"] + notes)}


def map_report(rows: list[dict], config: dict) -> list[dict]:
    return [map_account(name, spec, rows, config) for name, spec in config["accounts"].items()]


def tag_rows(rows: list[dict], config: dict) -> dict[str, str]:
    """모든 열의 행에 대해, 매핑 규칙(match·review·policy_pending)에 걸리는 내부 계정을 표시한다 (불일치 점검용)."""
    tags = {}
    for name, spec in config["accounts"].items():
        for kind in ("match", "review", "policy_pending"):
            for row, _ in matching(rows, spec.get(kind, []), config):
                tags[row["row_id"]] = name if kind == "match" else f"{name}({kind})"
    return tags
