"""수기 확인표(data/manual/note_confirmations.csv) — 주석 등 API에 없는 값을 원문 근거와 함께 보완한다 (D7).

수기 값도 정제 관측값과 같은 기준(기업·연결/별도·기간·접수번호)으로 연결하고 extraction_method = manual로 구분한다.
status가 confirmed인 행만 쓴다. 선택된 보고서(D5)와 접수번호가 다르면 쓰지 않는다.
"""

from decimal import Decimal
from pathlib import Path

import pandas as pd

REQUIRED = ["confirmation_id", "corp_code", "fs_div", "canonical_account", "period_type", "period_end", "value",
            "unit", "unit_multiplier", "rcept_no", "source_location", "status", "extraction_method",
            "amount_check", "scope_check", "method"]


def load_confirmations(path: Path) -> list[dict]:
    if not path.exists():
        return []
    table = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    missing = [c for c in REQUIRED if c not in table.columns]
    if missing:
        raise ValueError(f"수기 확인표에 열이 없습니다: {missing}")
    rows = table.to_dict("records")
    for r in rows:
        if r["extraction_method"] != "manual":
            raise ValueError(f"{r['confirmation_id']}: extraction_method는 manual이어야 합니다")
        if r["status"] == "confirmed" and not r["source_location"].strip():
            raise ValueError(f"{r['confirmation_id']}: 확인된 값에는 원문 위치가 필요합니다")
    return rows


def won(entry: dict) -> Decimal:
    """보고서 단위 그대로 적은 값 × 배수 → 원. 원래 단위와 배수는 확인표에 남아 있다."""
    return Decimal(entry["value"].replace(",", "")) * Decimal(entry["unit_multiplier"])


def apply_confirmations(result: dict, entries: list[dict], key: dict, selected_rcept: str, period_end: str | None) -> dict:
    """API 매핑 결과 하나에 수기 확인값을 적용한다."""
    candidates = [e for e in entries
                  if e["corp_code"] == key["corp_code"] and e["fs_div"] == key["fs_div"]
                  and e["canonical_account"] == result["canonical_account"] and e["period_end"] == period_end
                  and e["status"] == "confirmed"]
    if not candidates:
        return {**result, "extraction_method": "api_rule" if result["status"] == "mapped" else ""}
    usable = [e for e in candidates if e["rcept_no"] == selected_rcept]
    if not usable:
        return {**result, "extraction_method": "api_rule" if result["status"] == "mapped" else "",
                "note": "; ".join(filter(None, [result.get("note"), f"수기 확인값은 다른 보고서 버전 기준이라 쓰지 않음 ({candidates[0]['rcept_no']})"]))}
    if len(usable) > 1:
        return {**result, "status": "review_needed", "value": None, "extraction_method": "",
                "note": f"수기 확인값이 여러 개: {[e['confirmation_id'] for e in usable]}"}
    entry = usable[0]
    manual_value = won(entry)
    if result["status"] == "mapped" and Decimal(result["value"]) != manual_value:
        return {**result, "status": "review_needed", "value": None, "extraction_method": "",
                "note": f"API 값 {result['value']}와 수기 확인값 {manual_value}({entry['confirmation_id']})이 다름"}
    # 금액 확인과 범위(예: 리스 이자 포함) 확인은 따로 본다. 범위가 미확인이면 계산용 후보로만 쓰고 점수는 보류한다
    scope_ok = not result.get("basis") or entry.get("scope_check") == "확인"
    if not scope_ok:
        return {**result, "status": "candidate", "value": str(manual_value), "extraction_method": "manual",
                "note": f"수기 확인 {entry['confirmation_id']}: 금액 {entry.get('amount_check', '')}, '{result['basis']}' 범위 {entry.get('scope_check') or '미확인'} — 점수 반영 보류; {entry['source_location']}"}
    return {**result, "status": "mapped", "value": str(manual_value), "extraction_method": "manual",
            "note": "; ".join(filter(None, [f"수기 확인 {entry['confirmation_id']}: {entry['source_location']}", entry.get("components", ""), result.get("note") if result["status"] != "mapped" else ""]))}
