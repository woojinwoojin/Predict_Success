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
from src.normalization.build_facts import INTERIM, RAW, ROOT, latest_valid_filing
from src.normalization.dart_facts import build_facts, load_filings, mismatch_kind, same_period_mismatches

NAMES = {"01009789": "코스맥스", "00763473": "코스메카코리아", "01226410": "씨앤씨인터내셔널", "00160621": "한국화장품제조", "00939331": "한국콜마"}
STATUS_KO = {"mapped": "성공", "review_needed": "확인", "no_data": "자료없음", "empty_value": "값없음", "held": "보류"}


def main(as_of: str) -> None:
    config = load_rules(ROOT / "configs" / "account_map_v1.yaml")
    rows, snaps = build_facts(RAW)
    filings = load_filings(RAW)
    by_snapshot: dict[str, list] = {}
    for r in rows:
        if r["source_column"] == "당기":
            by_snapshot.setdefault(r["snapshot_id"], []).append(r)

    results, evidence_rows = [], []
    for s in snaps:
        key = {"corp_code": s["corp_code"], "company": NAMES.get(s["corp_code"], s["corp_code"]), "fs_div": s["fs_div"], "bsns_year": int(s["bsns_year"])}
        if s["status"] == "013":
            results += [{**key, "canonical_account": n, "label": spec["label"], "status": "no_data", "value": None,
                         "note": "API 응답: 조회된 데이터 없음", "snapshot_id": s["snapshot_id"]} for n, spec in config["accounts"].items()]
            continue
        selected = latest_valid_filing(filings, s["corp_code"], int(s["bsns_year"]), as_of)
        if s["rcept_nos"] != [selected]:
            results += [{**key, "canonical_account": n, "label": spec["label"], "status": "held", "value": None,
                         "note": f"기준일 {as_of}의 선택 보고서 {selected} ≠ API 값의 보고서 {s['rcept_nos']}", "snapshot_id": s["snapshot_id"]}
                        for n, spec in config["accounts"].items()]
            continue
        for m in map_report(by_snapshot.get(s["snapshot_id"], []), config):
            results.append({**key, **{k: v for k, v in m.items() if k != "evidence"}, "rcept_no": selected, "snapshot_id": s["snapshot_id"]})
            evidence_rows += [{**key, "canonical_account": m["canonical_account"], **e} for e in m["evidence"]]

    check = pd.DataFrame(results)
    check.to_csv(INTERIM / "account_check.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(evidence_rows).to_csv(INTERIM / "account_check_evidence.csv", index=False, encoding="utf-8-sig")

    labels = [spec["label"] for spec in config["accounts"].values()]
    print(f"기준일 {as_of} — 칸 = 4개 사업연도 상태 (성공·확인·자료없음·값없음·보류)\n")
    for fs in ("CFS", "OFS"):
        sub = check[check.fs_div == fs]
        table = sub.groupby(["company", "label"])["status"].apply(
            lambda s: "/".join(f"{STATUS_KO[k]}{v}" for k, v in Counter(s).most_common())).unstack()[labels]
        print(f"[{'연결' if fs == 'CFS' else '별도'}]")
        print(table.to_string(), "\n")
    print("상태 합계:", dict(Counter(check["status"])))
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
