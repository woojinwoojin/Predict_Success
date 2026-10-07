"""기업 × 필요한 계정 매핑 점검표: python -m src.normalization.build_account_check [기준일 YYYYMMDD]

값 선택(D5): 기준일 이전에 공개된 해당 사업연도의 최신 유효 정정본 → 그 보고서의 당기 값.
API 스냅샷이 선택한 보고서와 연결되지 않으면 최신 값으로 대체하지 않고 보류(held)한다.
결과: data/interim/account_check.csv, data/interim/account_check_evidence.csv
"""

import json
import sys
from collections import Counter
from pathlib import Path

import pandas as pd

from src.normalization.account_map import load_rules, map_report, tag_rows
from src.normalization.bs_sections import held_for_sale_adjustment
from src.normalization.build_facts import INTERIM, RAW, ROOT, latest_valid_filing
from src.normalization.dart_facts import build_facts, load_filings, mismatch_kind, same_period_mismatches
from src.normalization.manual import apply_confirmations, load_confirmations

MANUAL = ROOT / "data" / "manual"

NAMES = {"01009789": "코스맥스", "00763473": "코스메카코리아", "01226410": "씨앤씨인터내셔널", "00160621": "한국화장품제조", "00939331": "한국콜마"}
STATUS_KO = {"mapped": "성공", "candidate": "후보", "review_needed": "확인", "no_data": "자료없음", "empty_value": "값없음", "held": "보류",
             "not_applicable": "해당없음"}
# 매각예정항목 조정 (D8): 보고된 소계와 별도로 보존한다
ADJUSTED = {"adjusted_current_assets": ("assets", "유동자산(매각예정 포함)"), "adjusted_current_liabilities": ("liabilities", "유동부채(매각예정 포함)")}


def main(as_of: str) -> None:
    config = load_rules(ROOT / "configs" / "account_map_v1.yaml")
    rows, snaps = build_facts(RAW)
    filings = load_filings(RAW)
    by_snapshot: dict[str, list] = {}
    for r in rows:
        if r["source_column"] == "당기":
            by_snapshot.setdefault(r["snapshot_id"], []).append(r)
    # 보고서 당기의 종료일 (수기 확인값·불일치 검토를 같은 기간에 연결할 때 쓴다)
    current_end = {sid: Counter(r["period_end"] for r in rs if r["period_end"]).most_common(1)[0][0] for sid, rs in by_snapshot.items()}
    confirmations = load_confirmations(MANUAL / "note_confirmations.csv")
    reviews = pd.read_csv(MANUAL / "mismatch_reviews.csv", dtype=str, encoding="utf-8-sig") if (MANUAL / "mismatch_reviews.csv").exists() else pd.DataFrame()

    all_labels = {n: spec["label"] for n, spec in config["accounts"].items()} | {n: label for n, (_, label) in ADJUSTED.items()}
    results, evidence_rows = [], []
    for s in snaps:
        key = {"corp_code": s["corp_code"], "company": NAMES.get(s["corp_code"], s["corp_code"]), "fs_div": s["fs_div"], "bsns_year": int(s["bsns_year"])}
        if s["status"] == "013":
            results += [{**key, "canonical_account": n, "label": label, "status": "no_data", "value": None,
                         "note": "API 응답: 조회된 데이터 없음", "snapshot_id": s["snapshot_id"]} for n, label in all_labels.items()]
            continue
        selected = latest_valid_filing(filings, s["corp_code"], int(s["bsns_year"]), as_of)
        if s["rcept_nos"] != [selected]:
            results += [{**key, "canonical_account": n, "label": label, "status": "held", "value": None,
                         "note": f"기준일 {as_of}의 선택 보고서 {selected} ≠ API 값의 보고서 {s['rcept_nos']}", "snapshot_id": s["snapshot_id"]}
                        for n, label in all_labels.items()]
            continue
        end = current_end.get(s["snapshot_id"])
        for m in map_report(by_snapshot.get(s["snapshot_id"], []), config):
            m = apply_confirmations(m, confirmations, key, selected, end)
            linked = reviews[(reviews.corp_code == s["corp_code"]) & (reviews.fs_div == s["fs_div"]) & (reviews.period_end == end)
                             & (reviews.canonical_account == m["canonical_account"])] if len(reviews) else []
            review_note = "; ".join(f"불일치 검토 {r.review_id}: {r.cause}" for r in linked.itertuples()) if len(linked) else ""
            results.append({**key, **{k: v for k, v in m.items() if k != "evidence"}, "period_end": end, "mismatch_review": review_note,
                            "rcept_no": selected, "snapshot_id": s["snapshot_id"]})
            evidence_rows += [{**key, "canonical_account": m["canonical_account"], **e} for e in m["evidence"]]
        for name, (kind, label) in ADJUSTED.items():
            adj = held_for_sale_adjustment(by_snapshot.get(s["snapshot_id"], []), kind)
            status = "review_needed" if adj["included"] == "확인 필요" else "mapped"
            results.append({**key, "canonical_account": name, "label": label, "basis": "매각예정항목 포함", "status": status,
                            "value": adj["adjusted"], "reported_value": adj["reported"], "held_for_sale": adj["held_for_sale"],
                            "held_for_sale_included": adj["included"], "note": adj["evidence"] or f"매각예정 {adj['included']}",
                            "extraction_method": "api_rule+adjustment" if adj["included"] == "미포함" else ("api_rule" if status == "mapped" else ""),
                            "period_end": end, "mismatch_review": "", "rcept_no": selected, "snapshot_id": s["snapshot_id"]})

    check = pd.DataFrame(results)
    check.to_csv(INTERIM / "account_check.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(evidence_rows).to_csv(INTERIM / "account_check_evidence.csv", index=False, encoding="utf-8-sig")

    labels = list(all_labels.values())
    print(f"기준일 {as_of} — 칸 = 4개 사업연도 상태 (성공·확인·자료없음·값없음·보류)\n")
    for fs in ("CFS", "OFS"):
        sub = check[check.fs_div == fs]
        table = sub.groupby(["company", "label"])["status"].apply(
            lambda s: "/".join(f"{STATUS_KO[k]}{v}" for k, v in Counter(s).most_common())).unstack()[labels]
        print(f"[{'연결' if fs == 'CFS' else '별도'}]")
        print(table.to_string(), "\n")
    print("상태 합계:", dict(Counter(check["status"])), "| 수기 확인 사용:", int((check.get("extraction_method") == "manual").sum()))
    hfs = check[check["held_for_sale_included"].fillna("해당 없음") != "해당 없음"] if "held_for_sale_included" in check else check.iloc[0:0]
    for r in hfs.itertuples():
        print(f"  [매각예정 조정] {r.company} {r.fs_div} {r.bsns_year} {r.label}: 보고 {r.reported_value} / 매각예정 {r.held_for_sale} / {r.held_for_sale_included} → {r.value} ({r.note})")
    for r in check[check.status == "candidate"].itertuples():
        print(f"  [후보값·점수 보류] {r.company} {r.fs_div} {r.bsns_year} {r.label}: {r.value} — {r.note[:80]}")
    flagged = check[check["mismatch_review"].fillna("") != ""] if "mismatch_review" in check else check.iloc[0:0]
    for r in flagged.itertuples():
        print(f"  [불일치 검토 연결] {r.company} {r.fs_div} {r.bsns_year} {r.label}: {r.mismatch_review}")
    print("\n[확인 필요 사유]")
    rv = check[check.status == "review_needed"]
    for (label, note), g in rv.groupby(["label", "note"]):
        print(f"  {label}: {note}  ← {', '.join(sorted({f'{c} {f}' for c, f in zip(g.company, g.fs_div)}))} ({len(g)}건)")

    # 필요한 계정에 해당하는 동일 기간 값 불일치부터
    tags = tag_rows(rows, config)
    result = same_period_mismatches(rows)
    hits = []
    for key, group in result["mismatched"].items():
        names = {tags.get(r["row_id"]) for r in group} - {None}
        if names:
            hits.append((sorted(names), key, group))
    print(f"\n[필요한 계정의 동일 기간 값 불일치] {len(hits)}그룹 / 전체 {len(result['mismatched'])}그룹")
    for names, key, group in hits:
        kind = mismatch_kind(group)
        vals = "; ".join(f"{r['report_bsns_year']}보고서 {r['source_column']} {Decimal_str(r['value'])} [{r['account_nm']}]" for r in group)
        print(f"  {','.join(names)} | {NAMES.get(key[0])} {key[1]} {key[2]} {key[3]} {key[7]} | {kind['kind']}{' 계정명바뀜' if kind['label_changed'] else ''}{' 비표준키' if kind['nonstandard_key'] else ''} | {vals}")


def Decimal_str(v) -> str:
    return f"{int(float(v)):,}"


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else pd.Timestamp.today().strftime("%Y%m%d"))
