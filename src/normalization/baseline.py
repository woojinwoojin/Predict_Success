"""추세 비교의 2021 기초값 (docs/decisions.md D14): 2022 사업연도 선택 보고서의 전기 열.

값 선택(D5)의 기본은 '선택 보고서의 당기 값'이다. 2022년 평균 총자산·평균 재고와 전년 대비 매출 증가율에 필요한
2021 값만 예외로 2022 선택 보고서의 전기 열에서 가져온다.
- 계정 매핑 규칙은 당기와 같다 (account_map_v1)
- 실제 기간(종료일 2021, 흐름 값은 연간)·재무기준·통화(KRW)를 확인한다. 확인되지 않으면 보류
- 기준일 이전에 공개된 다른 보고서의 같은 기간 값(2023 보고서 전전기 등)과 대조한다. 다르면 확인 필요 (재작성으로 단정하지 않음)
- 0으로 채우거나 기말값 하나로 평균을 대신하지 않는다 — 값이 없으면 그 값을 쓰는 2022 지표만 보류된다
"""

from decimal import Decimal

from src.normalization.account_map import map_account
from src.normalization.selection import select_snapshot

BASELINE_YEAR = 2021
SOURCE_YEAR = 2022
SOURCE_COLUMN = "전기"
ACCOUNTS = ("total_assets", "inventory", "revenue")


def cross_check(used: dict, rows: list[dict], published: dict, as_of: str) -> tuple[str, list[str]]:
    """같은 기업·재무기준·재무제표·계정·세부구분·기간의 다른 보고서 값과 대조한다 (기준일 이전 공개분만)."""
    same = [r for r in rows
            if r["rcept_no"] != used["rcept_no"] and r["corp_code"] == used["corp_code"] and r["fs_div"] == used["fs_div"]
            and r["sj_div"] == used["sj_div"] and r["account_id"] == used["account_id"] and r["account_detail"] == used["account_detail"]
            and r["period_end"] == used["period_end"] and r["value_status"] == "parsed"
            and published.get(r["rcept_no"], {}).get("rcept_dt", "99999999") <= as_of]
    if not same:
        return "대조 없음", ["같은 기간을 담은 다른 보고서 없음"]
    found = [f"{r['report_bsns_year']} 보고서 {r['source_column']} {Decimal(r['value']):,}" for r in same]
    if all(Decimal(r["value"]) == Decimal(used["value"]) for r in same):
        return "일치", found
    return "불일치", found


def build_baseline(rows: list[dict], snaps: list[dict], filings: dict, published: dict, config: dict,
                   corp: str, fs: str, company: str, as_of: str) -> list[dict]:
    key = {"corp_code": corp, "company": company, "fs_div": fs, "bsns_year": BASELINE_YEAR, "source_column": SOURCE_COLUMN,
           "baseline_rule": f"{SOURCE_YEAR} 선택 보고서의 {SOURCE_COLUMN} 열 (추세 비교 기초값 예외, D14)"}
    sel = select_snapshot(snaps, published, filings, corp, fs, SOURCE_YEAR, as_of)
    if sel.status != "selected":
        return [{**key, "canonical_account": n, "label": config["accounts"][n]["label"], "status": sel.status, "value": None,
                 "note": f"{SOURCE_YEAR} 선택 보고서 없음: {sel.note}", "rcept_no": sel.rcept_no, "snapshot_id": sel.snapshot_id,
                 "extraction_method": "", "cross_check": ""} for n in ACCOUNTS]
    prior = [r for r in rows if r["snapshot_id"] == sel.snapshot_id and r["source_column"] == SOURCE_COLUMN]
    by_id = {r["row_id"]: r for r in prior}
    out = []
    for name in ACCOUNTS:
        m = map_account(name, config["accounts"][name], prior, config)
        base = {**key, **{k: v for k, v in m.items() if k != "evidence"}, "rcept_no": sel.rcept_no, "snapshot_id": sel.snapshot_id,
                "extraction_method": "", "cross_check": ""}
        if m["status"] != "mapped":
            out.append({**base, "value": None, "note": f"{SOURCE_COLUMN} 열 매핑 {m['status']}: {m.get('note', '')}"})
            continue
        used = [by_id[e["row_id"]] for e in m["evidence"] if e["kind"] == "match"]
        problems = []
        if len(used) != 1:
            problems.append(f"사용 행 {len(used)}개")
        u = used[0] if used else {}
        if u.get("currency") != "KRW":
            problems.append(f"통화 {u.get('currency')}")
        if u.get("fs_div") != fs:
            problems.append(f"재무기준 {u.get('fs_div')} ≠ {fs}")
        if not m.get("period_end") or int(m["period_end"][:4]) != BASELINE_YEAR:
            problems.append(f"기간 종료일 {m.get('period_end')}이 {BASELINE_YEAR} 사업연도와 다름")
        if m.get("period_type") == "duration" and m.get("period_is_annual") is not True:
            problems.append(f"연간 기간 아님 ({m.get('period_start')}~{m.get('period_end')})")
        if problems:
            out.append({**base, "status": "review_needed", "value": None, "note": "기초값 확인 실패: " + "; ".join(problems)})
            continue
        result, found = cross_check(u, rows, published, as_of)
        note = f"{u['account_nm']} ({u['account_id']}), 기간 {m.get('period_start') or ''}~{m['period_end']}, 다른 보고서 대조 {result}: " + "; ".join(found)
        if result == "불일치":
            out.append({**base, "status": "review_needed", "value": None, "cross_check": result,
                        "note": "같은 기간 다른 보고서 값과 다름 — 원인 확인 전 보류 (재작성으로 단정하지 않음). " + note})
            continue
        out.append({**base, "status": "mapped", "value": m["value"], "cross_check": result,
                    "extraction_method": f"api_rule({SOURCE_COLUMN} 열)", "note": note})
    return out
