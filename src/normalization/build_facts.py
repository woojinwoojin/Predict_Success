"""관측값 테이블(A)을 만들고 점검 결과를 출력한다: python -m src.normalization.build_facts

결과: data/interim/dart_facts.csv, data/interim/dart_snapshots.csv (다시 만들 수 있는 중간 산출물)
"""

from collections import Counter
from pathlib import Path

import pandas as pd

from src.normalization.dart_facts import NON_BODY_CORRECTIONS, build_facts, load_filings, mismatch_kind, same_period_mismatches

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "data" / "raw" / "dart"
INTERIM = ROOT / "data" / "interim"


def latest_valid_filing(filings: dict, corp_code: str, bsns_year: int, as_of: str) -> str | None:
    """기준일(YYYYMMDD) 이전에 공개된 해당 사업연도 보고서 중 최신 유효 정정본의 접수번호."""
    candidates = [
        f for f in filings.values()
        if f["link"] == "stored" and f.get("corp_code") == corp_code and f.get("bsns_year") == bsns_year
        and f["rcept_dt"] <= as_of and f.get("correction") not in NON_BODY_CORRECTIONS
    ]
    return max(candidates, key=lambda f: (f["rcept_dt"], f["rcept_no"]))["rcept_no"] if candidates else None


def main(as_of: str | None = None) -> None:
    rows, snaps = build_facts(RAW)
    INTERIM.mkdir(parents=True, exist_ok=True)
    facts = pd.DataFrame(rows)
    facts.to_csv(INTERIM / "dart_facts.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(snaps).to_csv(INTERIM / "dart_snapshots.csv", index=False, encoding="utf-8-sig")

    print(f"스냅샷 {len(snaps)}건 (상태 {dict(Counter(s['status'] for s in snaps))}), 관측값 행 {len(facts):,}개")
    print("\n[값 상태]", dict(Counter(facts["value_status"])))
    print("  숫자 0:", int((facts["value"] == "0").sum()))
    print("[기간 상태]", dict(Counter(facts["period_status"])))
    print("  sj_div별 기간 상태:", facts.groupby(["sj_div", "period_status"]).size().to_dict())
    no_std = facts["account_id"].isin(["-", "", "-표준계정코드 미사용-"]) | facts["account_id"].isna()
    items = facts[facts["source_column"] == "당기"]  # 원본 항목 수 = 당기 열 행 수
    items_no_std = no_std[facts["source_column"] == "당기"]
    print(f"[표준코드 없음] 원본 항목 {int(items_no_std.sum()):,} / {len(items):,}개 — sj_div별", items[items_no_std].groupby("sj_div").size().to_dict())
    print("[원문 연결]", dict(Counter(facts["filing_link"])))

    filings = load_filings(RAW)
    as_of = as_of or pd.Timestamp.today().strftime("%Y%m%d")
    link = Counter()
    for s in snaps:
        if s["status"] != "000":
            link["자료 없음"] += 1
            continue
        selected = latest_valid_filing(filings, s["corp_code"], int(s["bsns_year"]), as_of)
        link["최신 유효 정정본과 같음" if s["rcept_nos"] == [selected] else f"다름 (선택 {selected}, API {s['rcept_nos']})"] += 1
    print(f"[API 값 ↔ 기준일 {as_of}의 최신 유효 정정본]", dict(link))

    result = same_period_mismatches(rows)
    records = []
    for key, group in result["mismatched"].items():
        kind = mismatch_kind(group)
        for r in group:
            records.append({"corp_code": key[0], "fs_div": key[1], "sj_div": key[2], "account_key": key[3], "account_detail": key[4],
                            "period_type": key[5], "period_start": key[6], "period_end": key[7], **kind,
                            "report_bsns_year": r["report_bsns_year"], "source_column": r["source_column"], "rcept_no": r["rcept_no"],
                            "account_nm": r["account_nm"], "value": r["value"], "row_id": r["row_id"]})
    mism = pd.DataFrame(records)
    mism.to_csv(INTERIM / "dart_same_period_mismatches.csv", index=False, encoding="utf-8-sig")
    print(f"[동일 기간 값 불일치] 비교한 그룹 {result['compared_groups']:,}개 중 {len(result['mismatched'])}개"
          f" (한 응답 안에서 키가 겹쳐 비교에서 뺀 계정 {result['ambiguous_keys']}개)")
    if len(mism):
        groups = mism.drop_duplicates(["corp_code", "fs_div", "sj_div", "account_key", "account_detail", "period_end"])
        print("  유형:", groups.groupby(["kind", "sj_div"]).size().to_dict(), "| 계정명 바뀜:", int(groups["label_changed"].sum()))
        key_hits = groups[groups["account_key"].isin(KEY_ACCOUNTS)]
        print(f"  핵심 계정(매출·영업이익·순이익·영업CF·총자산) 불일치: {len(key_hits)}개")


KEY_ACCOUNTS = ["ifrs-full_Revenue", "dart_OperatingIncomeLoss", "ifrs-full_ProfitLoss",
                "ifrs-full_CashFlowsFromUsedInOperatingActivities", "ifrs-full_Assets"]


if __name__ == "__main__":
    main()
