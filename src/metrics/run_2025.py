"""2025 지표 출력: python -m src.metrics.run_2025

입력: data/interim/account_check.csv (계정 점검표), configs/basis_v1.yaml, data/manual/scope_changes.csv
출력: data/interim/metrics_2025.csv — 기업 · 지표 · 대상 기간 · 사용 기준 · 계산값 · 입력 출처 · 계산 상태 · 점수 반영 가능 여부 · 보류 사유
"""

from decimal import Decimal

import pandas as pd
import yaml

from src.metrics.engine import OWN_METRICS, Input, finalize, peer_relative
from src.normalization.build_facts import INTERIM, ROOT

BASIS_KO = {"CFS": "연결", "OFS": "별도"}
STATUS_KO = {"normal": "정상", "reference": "참고 계산", "not_computed": "계산 안 함"}


def make_getter(check: pd.DataFrame, corp: str, fs: str):
    sub = check[(check.corp_code == corp) & (check.fs_div == fs)]

    def get(account: str, year: int) -> Input:
        row = sub[(sub.canonical_account == account) & (sub.bsns_year == str(year))]
        if row.empty:
            return Input(account, year, "missing", None, "", "점검표에 없음")
        if len(row) > 1:  # 분석 입력 키는 유일해야 한다 — 행 순서로 고르지 않는다 (D11 ②)
            raise ValueError(f"계산 입력 중복: {corp} {fs} {year} {account} ({len(row)}행)")
        r = row.iloc[0]
        value = Decimal(r["value"]) if r["value"] not in ("", None) and r["status"] in ("mapped", "candidate") else None
        annual = {"True": True, "False": False}.get(str(r.get("period_is_annual", "")))
        return Input(r["label"], year, r["status"], value, f"{r['rcept_no']} {r['extraction_method'] or ''}".strip(), r["note"] or "",
                     r.get("period_type") or None, r.get("period_start") or None, r.get("period_end") or None,
                     r.get("period_status") or None, annual)
    return get


def scope_note(scope: pd.DataFrame, corp: str, fs: str, years: range) -> str:
    rows = scope[(scope.corp_code == corp) & (scope.fs_div == fs) & (scope.bsns_year.astype(int).isin(list(years))) & (scope.change_type != "변동 없음")]
    if rows.empty:
        return ""
    return "연결 범위 변화: " + "; ".join(f"{r.bsns_year} {r.change_type} {r.entity}({r.reason})" for r in rows.itertuples()) + " — 영향 규모 미확인"


def main() -> None:
    check = pd.read_csv(INTERIM / "account_check.csv", dtype=str, keep_default_na=False)
    basis = yaml.safe_load((ROOT / "configs" / "basis_v1.yaml").read_text(encoding="utf-8"))
    scope = pd.read_csv(ROOT / "data" / "manual" / "scope_changes.csv", dtype=str, keep_default_na=False, encoding="utf-8-sig")

    own = {}
    for corp, spec in basis["companies"].items():
        get = make_getter(check, corp, spec["basis"])
        results = []
        for fn in OWN_METRICS:
            out = fn(get)
            results += out if isinstance(out, list) else [out]
        own[corp] = {r.metric: r for r in results}

    peer_spec = basis["peer_groups"]["cosmetics_odm_pilot"]
    members = [c for c, s in basis["companies"].items() if s["peer_group"] == "cosmetics_odm_pilot"]
    peer = peer_relative(own, members, peer_spec["label"] + " (파일럿)")

    rows = []
    for corp, spec in basis["companies"].items():
        for r in [finalize(x) for x in list(own[corp].values()) + peer[corp]]:
            years = range(2023, 2026) if r.metric in ("revenue_cagr_3y", "growth_volatility", "growth_gap") else range(2025, 2026)
            note = scope_note(scope, corp, spec["basis"], years) if spec["basis"] == "CFS" else ""
            rows.append({
                "company": spec["name"], "corp_code": corp, "metric": r.metric, "metric_label": r.label, "period": r.period,
                "basis": f"{BASIS_KO[spec['basis']]} — {spec['reason']}",
                "value": r.value, "calc_status": STATUS_KO[r.calc_status], "score_eligible": r.score_eligible,
                "hold_reasons": " | ".join(r.hold_reasons), "inputs": " | ".join(i.describe() for i in r.inputs),
                "peer_percentile_validation": r.peer_percentile_validation,
                "notes": " | ".join(filter(None, r.notes + [note])),
            })
    out = pd.DataFrame(rows)
    out.to_csv(INTERIM / "metrics_2025.csv", index=False, encoding="utf-8-sig")

    fmt = lambda m, v: "—" if v is None or pd.isna(v) else (f"{v:.2f}배" if m in ("interest_coverage", "current_ratio_hfs", "current_ratio_reported", "cash_to_short_term_debt", "inventory_turnover", "inventory_turnover_rel") else f"{v:+.1%}" if m.endswith("gap") else f"{v:.1%}")
    table = out.assign(show=[f"{fmt(m, v)}{'' if s == '정상' else ' ('+s+')'}" for m, v, s in zip(out.metric, out.value, out.calc_status)])
    pivot = table.pivot_table(index="metric_label", columns="company", values="show", aggfunc="first", sort=False)
    print(pivot.to_string())
    print("\n[계산 상태]", out.groupby("calc_status").size().to_dict(), "| [점수 반영]", out.score_eligible.str.split(" —").str[0].value_counts().to_dict())


if __name__ == "__main__":
    main()
