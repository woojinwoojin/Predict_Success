"""파일럿 2025 분석 실행 + 실행 기록: python -m src.pipeline.run_pilot_2025 [기준일 YYYYMMDD]

관측값 → 계정 점검표 → 지표 → 점수·커버리지를 한 번에 실행하고, 결과를 실행 기록과 함께
data/interim/runs/{run_id}/에 저장한다 (D12). data/interim/의 최신 파일도 갱신한다.
실행 기록: 기준일, 기업·연도별 선택 보고서·스냅샷·응답 해시·경고, 코드 커밋, 규칙·비교군·수기 확인표 버전과 해시, 출력 해시.
"""

import hashlib
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone

import pandas as pd
import yaml

from src.collectors.dart import new_run_id
from src.metrics import run_2025
from src.normalization import build_account_check, build_facts as build_facts_cli
from src.normalization.build_facts import INTERIM, RAW, ROOT
from src.normalization.dart_facts import build_facts, load_filings
from src.normalization.selection import load_published_filings, select_snapshot
from src.scoring.score import coverage, score_row

CONFIGS = ["pilot_v1.yaml", "account_map_v1.yaml", "basis_v1.yaml", "scoring_v1.yaml"]
MANUAL = ["note_confirmations.csv", "mismatch_reviews.csv", "scope_changes.csv", "disclosure_issues.csv"]


def sha256_file(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git_state() -> dict:
    def run(*args):
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    dirty = run("status", "--porcelain", "--", "src", "configs", "data/manual")
    return {"commit": run("rev-parse", "HEAD"), "dirty": bool(dirty), "dirty_files": dirty.splitlines()[:20]}


def score_all(metrics: pd.DataFrame, scoring: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    rules, view_only = scoring["metrics"], set(scoring["view_only"]) | set(scoring.get("raw_only", []))
    rows, cover = [], []
    for company, g in metrics.groupby("company", sort=False):
        scored = []
        for m in g.to_dict("records"):
            rule = rules.get(m["metric"])
            res = score_row(m, rule, scoring, view_only)
            scored.append({**m, **res, "rule_method": rule["method"] if rule else "", "rule_direction": rule.get("direction") if rule else "",
                           "rule_points": json.dumps(rule.get("points")) if rule and rule.get("points") else "",
                           "rule_version": f"{scoring['version']} ({scoring['status']})"})
        for area, spec in scoring["areas"].items():
            if spec.get("implemented") is False:
                scored.append({"company": company, "corp_code": g.corp_code.iloc[0], "metric": f"{area}_area", "metric_label": spec["label"] + " (영역 전체)",
                               "score_status": "not_implemented", "weight": spec["weight"], "score": None, "trace_score": None, "contribution": None,
                               "score_reason": "미구현", "rule_version": f"{scoring['version']} ({scoring['status']})"})
        rows += scored
        cover.append({"company": company, "corp_code": g.corp_code.iloc[0], **coverage(scored, scoring)})
    return pd.DataFrame(rows), pd.DataFrame(cover)


def main(as_of: str) -> None:
    run_id = new_run_id()
    out = INTERIM / "runs" / run_id
    out.mkdir(parents=True)

    # 1) 관측값·점검표·지표 (기존 단계 그대로 실행 — data/interim 최신 파일 갱신)
    build_facts_cli.main(as_of)
    build_account_check.main(as_of)
    run_2025.main()

    # 2) 점수·커버리지
    scoring = yaml.safe_load((ROOT / "configs" / "scoring_v1.yaml").read_text(encoding="utf-8"))
    metrics = pd.read_csv(INTERIM / "metrics_2025.csv", dtype=str, keep_default_na=False)
    scores, cover = score_all(metrics, scoring)
    scores.to_csv(INTERIM / "scores_2025.csv", index=False, encoding="utf-8-sig")
    cover.to_csv(INTERIM / "coverage_2025.csv", index=False, encoding="utf-8-sig")

    # 3) 실행 기록: 무엇으로 이 결과를 만들었나
    rows, snaps = build_facts(RAW)
    filings, published = load_filings(RAW), load_published_filings(RAW)
    snap_by_id = {s["snapshot_id"]: s for s in snaps}
    selections = []
    for corp, fs, year in sorted({(s["corp_code"], s["fs_div"], int(s["bsns_year"])) for s in snaps}):
        sel = select_snapshot(snaps, published, filings, corp, fs, year, as_of)
        selections.append({"corp_code": corp, "fs_div": fs, "bsns_year": year, "status": sel.status, "rcept_no": sel.rcept_no,
                           "snapshot_id": sel.snapshot_id, "response_sha256": snap_by_id.get(sel.snapshot_id, {}).get("sha256"),
                           "note": sel.note, "warnings": sel.warnings})
    basis = yaml.safe_load((ROOT / "configs" / "basis_v1.yaml").read_text(encoding="utf-8"))
    outputs = {}
    for name in ["dart_facts.csv", "account_check.csv", "metrics_2025.csv", "scores_2025.csv", "coverage_2025.csv"]:
        shutil.copy2(INTERIM / name, out / name)
        outputs[name] = sha256_file(out / name)
    manifest = {
        "run_id": run_id, "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "as_of": as_of,
        "code": git_state(),
        "rules": {c: {"sha256": sha256_file(ROOT / "configs" / c),
                      "version": yaml.safe_load((ROOT / "configs" / c).read_text(encoding="utf-8")).get("version")} for c in CONFIGS},
        "scoring_status": scoring["status"],
        "peer_group": {**basis["peer_groups"]["cosmetics_odm_pilot"],
                       "members": [c for c, s in basis["companies"].items() if s["peer_group"] == "cosmetics_odm_pilot"]},
        "manual_tables": {m: {"sha256": sha256_file(ROOT / "data" / "manual" / m),
                              "rows": len(pd.read_csv(ROOT / "data" / "manual" / m, dtype=str, encoding="utf-8-sig"))} for m in MANUAL},
        "raw": {"snapshots": len(snaps), "stored_filings": sum(1 for f in filings.values() if f["link"] == "stored"),
                "published_filings": len(published)},
        "selections": selections,
        "selection_warnings": [w for s in selections for w in s["warnings"]],
        "outputs": outputs,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n[실행 기록] {out / 'manifest.json'}  (코드 {manifest['code']['commit'][:7]}{' + 미커밋 변경' if manifest['code']['dirty'] else ''}, 점수 규칙 {scoring['status']})")
    print(cover[["company", "K_confirmed_contribution", "scored_weight", "held_weight", "validation_only_weight", "not_implemented_weight",
                 "not_applicable_weight", "range_low", "range_high", "weighted_coverage", "coverage_status"]].to_string(index=False))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else pd.Timestamp.today().strftime("%Y%m%d"))
