"""2022~2025 회고 추세: python -m src.metrics.run_trend [기준일 YYYYMMDD]  (docs/decisions.md D14)

기준일 현재 확보한 공시를 사용한 회고 추세다. 모든 연도에 같은 기준일·같은 선택 규칙(D5)을 쓴다 —
각 연도 당시 알 수 있었던 정보만으로 평가한 결과가 아니다. 참고 표시만 하고 점수·커버리지에 반영하지 않는다.

입력: data/interim/account_check.csv (연도별 선택 보고서의 당기 값), 2021 기초값(2022 선택 보고서의 전기 열),
      data/manual/scope_changes.csv, data/manual/mismatch_reviews.csv, configs/trend_v1.yaml, configs/basis_v1.yaml
출력: data/interim/trend_2022_2025.csv (기업 5 × 연도 4 × 지표 10 = 200행)
      data/interim/working_capital_2022_2025.csv (기업 × 연도 운전자본 변동 표시)
      data/interim/trend_baseline_2021.csv (2021 기초값과 대조 결과)
"""

import sys
from dataclasses import dataclass
from decimal import Decimal

import pandas as pd
import yaml

from src.metrics.engine import METRIC_ACCOUNTS, TREND_METRICS, finalize
from src.metrics.run_2025 import BASIS_KO, STATUS_KO, make_getter
from src.metrics.working_capital import working_capital
from src.normalization.account_map import load_rules
from src.normalization.baseline import build_baseline
from src.normalization.build_facts import INTERIM, RAW, ROOT
from src.normalization.dart_facts import build_facts, load_filings
from src.normalization.selection import load_published_filings

MANUAL = ROOT / "data" / "manual"
COMPARABLE, CAUTION = "직접 비교 가능", "비교 주의"
HOLD_REFERENCE, NOT_COMPARABLE = "비교 보류 — 참고 계산", "비교 불가 — 계산 안 함"


@dataclass
class Reason:
    """비교 주의 사유와 그 사유가 걸리는 비교 대상 기간(인접 연도 쌍)."""
    text: str
    pairs: tuple[tuple[int, int], ...]
    span: str

    def show(self) -> str:
        return f"[{self.span}] {self.text}"


def pair_label(a: int, b: int) -> str:
    return f"{a}↔{b} 비교 주의"


def scope_reasons(scope: pd.DataFrame, corp: str, fs: str, year: int) -> list[Reason]:
    """연도 Y의 연결 범위 변화 → (Y-1, Y)와 (Y, Y+1) 비교. 연중 편입·제외는 그해 일부 기간만 반영되어 다음 해와의 비교에도 섞인다.

    보수적 처리다 — 영향이 실제로 큰지 확인했다는 뜻이 아니다 (영향 규모 미확인)."""
    if fs != "CFS":
        return []
    rows = scope[(scope.corp_code == corp) & (scope.fs_div == fs) & (scope.bsns_year == str(year)) & (scope.change_type != "변동 없음")]
    if rows.empty:
        return []
    text = (f"연결 범위 변화 {year}: " + "; ".join(f"{r.change_type} {r.entity}({r.reason})" for r in rows.itertuples())
            + " — 영향 규모 미확인 (보수적 표시)")
    return [Reason(text, ((year - 1, year), (year, year + 1)), f"{year - 1}↔{year}·{year}↔{year + 1} 비교 주의")]


def review_reasons(reviews: pd.DataFrame, causes: dict, corp: str, fs: str, metric: str, year: int) -> list[Reason]:
    """재분류·표시 범위 변경이 확인된 기간 Y → (Y, Y+1) 비교. Y와 Y+1 행에 붙인다 (trend_v1.yaml)."""
    out = []
    accounts = METRIC_ACCOUNTS[metric]
    for r in reviews[(reviews.corp_code == corp) & (reviews.fs_div == fs)].itertuples():
        account = r.canonical_account.replace("(review)", "")
        y = int(r.period_end[:4])
        if r.cause in causes and account in accounts and year in (y, y + 1):
            out.append(Reason(f"{causes[r.cause]} ({r.review_id}: {r.period_end} {r.canonical_account}, {r.cause_detail[:80]})",
                              ((y, y + 1),), pair_label(y, y + 1)))
    return out


def wc_reason(wc: dict, year: int) -> list[Reason]:
    """운전자본 규모 경고 → 그해 단년 값의 해석 주의. 그해가 들어간 비교 모두에 걸린다."""
    if not wc["caution_flag"]:
        return []
    return [Reason(wc["caution"], ((year - 1, year), (year, year + 1)), f"{year} 단년 값 해석 주의 — {year}가 들어간 비교")]


def comparability(calc_status: str, reasons: list) -> str:
    if calc_status == "reference":
        return HOLD_REFERENCE
    if calc_status != "normal":
        return NOT_COMPARABLE
    return CAUTION if reasons else COMPARABLE


def pair_table(trend: pd.DataFrame, reasons: dict, years: list[int]) -> pd.DataFrame:
    """비교 대상 기간별 비교 가능성: 인접 연도 쌍과 2022~2025 전체 추세. 행(연도) 상태와 따로 본다."""
    out = []
    for (corp, metric), g in trend.groupby(["corp_code", "metric"], sort=False):
        status = dict(zip(g.year, g.calc_status))
        first = g.iloc[0]
        base = {"company": first.company, "corp_code": corp, "metric": metric, "metric_label": first.metric_label}
        pair_status = []
        for a, b in zip(years, years[1:]):
            ends = (status[a], status[b])
            hits = [x for y in (a, b) for x in reasons[(corp, metric, y)] if (a, b) in x.pairs]
            texts = list(dict.fromkeys(x.text for x in hits))
            if "계산 안 함" in ends:
                s = NOT_COMPARABLE
            elif "참고 계산" in ends:
                s = HOLD_REFERENCE
            else:
                s = CAUTION if texts else COMPARABLE
            pair_status.append((f"{a}↔{b}", s))
            out.append({**base, "span": f"{a}↔{b}", "comparability": s, "reasons": " | ".join(texts)})
        n_ok = sum(1 for y in years if status[y] == "정상")
        cautions = [span for span, s in pair_status if s == CAUTION]
        if n_ok < len(years):
            s = f"전체 추세 비교 불가 — {len(years)}년 중 정상 계산 {n_ok}년"
        elif cautions:
            s = "전체 추세 주의 — 주의 구간 " + ", ".join(cautions)
        else:
            s = "전체 추세 직접 비교 가능"
        out.append({**base, "span": f"{years[0]}~{years[-1]} 전체", "comparability": s, "reasons": ""})
    return pd.DataFrame(out)


def avg_assets(get, y: int) -> Decimal | None:
    a0, a1 = get("total_assets", y - 1), get("total_assets", y)
    if a0.calc_status != "normal" or a1.calc_status != "normal":
        return None
    return (a0.value + a1.value) / 2


def main(as_of: str, rows: list[dict] | None = None, snaps: list[dict] | None = None) -> pd.DataFrame:
    cfg = yaml.safe_load((ROOT / "configs" / "trend_v1.yaml").read_text(encoding="utf-8"))
    basis = yaml.safe_load((ROOT / "configs" / "basis_v1.yaml").read_text(encoding="utf-8"))
    account_cfg = load_rules(ROOT / "configs" / "account_map_v1.yaml")
    check = pd.read_csv(INTERIM / "account_check.csv", dtype=str, keep_default_na=False)
    scope = pd.read_csv(MANUAL / "scope_changes.csv", dtype=str, keep_default_na=False, encoding="utf-8-sig")
    reviews = pd.read_csv(MANUAL / "mismatch_reviews.csv", dtype=str, keep_default_na=False, encoding="utf-8-sig")
    if rows is None:
        rows, snaps = build_facts(RAW)
    filings, published = load_filings(RAW), load_published_filings(RAW)

    # 2021 기초값 (예외 규칙 — baseline.py)
    baseline = [b for corp, spec in basis["companies"].items()
                for b in build_baseline(rows, snaps, filings, published, account_cfg, corp, spec["basis"], spec["name"], as_of)]
    baseline_df = pd.DataFrame(baseline).fillna("").astype(str)
    baseline_df.to_csv(INTERIM / "trend_baseline_2021.csv", index=False, encoding="utf-8-sig")
    inputs_table = pd.concat([check, baseline_df], ignore_index=True).fillna("")

    cf_by_snapshot: dict[str, list] = {}
    for r in rows:
        if r["sj_div"] == "CF" and r["source_column"] == "당기":
            cf_by_snapshot.setdefault(r["snapshot_id"], []).append(r)

    out, wc_rows, all_reasons = [], [], {}
    threshold = float(cfg["working_capital"]["caution_threshold"])
    for corp, spec in basis["companies"].items():
        fs = spec["basis"]
        get = make_getter(inputs_table, corp, fs)
        for y in cfg["years"]:
            # 운전자본 변동 (선택 보고서 당기 열의 현금흐름표)
            snap = check[(check.corp_code == corp) & (check.fs_div == fs) & (check.bsns_year == str(y))].snapshot_id
            snapshot_id = snap.iloc[0] if len(snap) and snap.iloc[0] else None
            wc = working_capital(cf_by_snapshot.get(snapshot_id, []), avg_assets(get, y), threshold)
            scope_y = scope_reasons(scope, corp, fs, y)
            wc_rows.append({"title": cfg["title"], "as_of": as_of, "company": spec["name"], "corp_code": corp, "year": y,
                            "basis": BASIS_KO[fs], **wc, "scope_change": " | ".join(x.text for x in scope_y) or ("변동 없음" if fs == "CFS" else "해당 없음 — 별도 기준"),
                            "use": "해석용 표시 — 점수 감점·일회성 판정에 쓰지 않음"})

            results = []
            for fn in TREND_METRICS:
                res = fn(get, y)
                results += res if isinstance(res, list) else [res]
            by_metric = {r.metric: finalize(r) for r in results}
            for metric in cfg["metrics"]:
                r = by_metric[metric]
                reasons = scope_y + review_reasons(reviews, cfg["comparability_causes"], corp, fs, metric, y)
                notes = list(r.notes)
                if metric == "ocf_to_avg_assets":
                    reasons += wc_reason(wc, y)
                    notes.append(f"운전자본 변동: {wc['wc_status']}"
                                 + (f" {Decimal(wc['wc_amount']):,}원, 항등식 {wc['sign_status']}, 현금흐름 영향 방향 {wc['ocf_direction']}" if wc["wc_amount"] else "")
                                 + (f" — {wc['caution']}" if wc["caution"] else ""))
                all_reasons[(corp, metric, y)] = reasons
                if any(i.year == cfg["baseline"]["year"] for i in r.inputs):
                    notes.append(f"{cfg['baseline']['year']} 기초값: {cfg['baseline']['source']} (예외 규칙, 다른 보고서 대조는 trend_baseline_2021.csv)")
                out.append({
                    "title": cfg["title"], "as_of": as_of, "company": spec["name"], "corp_code": corp, "year": y,
                    "metric": metric, "metric_label": r.label, "period": r.period, "basis": f"{BASIS_KO[fs]} — {spec['reason']}",
                    "value": r.value, "calc_status": STATUS_KO[r.calc_status], "comparability": comparability(r.calc_status, reasons),
                    "caution_spans": " / ".join(dict.fromkeys(x.span for x in reasons)),
                    "comparability_reasons": " | ".join(x.show() for x in reasons), "hold_reasons": " | ".join(r.hold_reasons),
                    "inputs": " | ".join(i.describe() for i in r.inputs),
                    "input_keys": ";".join(f"{i.account}:{i.year}" for i in r.inputs), "score_use": cfg["score_use"], "notes": " | ".join(notes),
                })
    trend = pd.DataFrame(out)
    expected = len(basis["companies"]) * len(cfg["years"]) * len(cfg["metrics"])
    if len(trend) != expected or trend.duplicated(["corp_code", "year", "metric"]).any():
        raise ValueError(f"추세 표 행 수 {len(trend)} ≠ {expected} 또는 키 중복")
    trend.to_csv(INTERIM / "trend_2022_2025.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(wc_rows).to_csv(INTERIM / "working_capital_2022_2025.csv", index=False, encoding="utf-8-sig")
    pairs = pair_table(trend, all_reasons, cfg["years"])
    pairs.to_csv(INTERIM / "trend_pairs_2022_2025.csv", index=False, encoding="utf-8-sig")

    report(trend, pd.DataFrame(wc_rows), baseline_df, cfg, pairs)
    compare_2025(trend)
    return trend


def compare_2025(trend: pd.DataFrame) -> None:
    """추세 표의 2025 값·계산 상태가 기존 2025 지표(metrics_2025.csv)와 같은지 대조한다. 다르면 중단."""
    m25 = pd.read_csv(INTERIM / "metrics_2025.csv", dtype=str, keep_default_na=False)
    t25 = trend[trend.year == 2025]
    merged = t25.merge(m25, on=["corp_code", "metric"], suffixes=("", "_2025"))
    diffs = []
    for r in merged.itertuples():
        a = None if r.value is None or pd.isna(r.value) else float(r.value)
        b = float(r.value_2025) if r.value_2025 else None
        same_value = (a is None and b is None) or (a is not None and b is not None and abs(a - b) <= 1e-12 * max(1.0, abs(b)))
        if not same_value or r.calc_status != r.calc_status_2025:
            diffs.append(f"{r.company} {r.metric}: 추세 {a} {r.calc_status} / 2025 {b} {r.calc_status_2025}")
    print(f"\n[2025 대조] 공통 지표 {len(merged)}개 (전년 대비 매출 증가율은 2025 결과에 없음) — 값·계산 상태 차이 {len(diffs)}개")
    if diffs:
        raise ValueError("추세 표의 2025 결과가 기존 2025 지표와 다름: " + "; ".join(diffs[:10]))


def report(trend: pd.DataFrame, wc: pd.DataFrame, baseline: pd.DataFrame, cfg: dict, pairs: pd.DataFrame) -> None:
    ratio_metrics = {"interest_coverage", "current_ratio_hfs", "current_ratio_reported", "cash_to_short_term_debt", "inventory_turnover"}
    mark = {COMPARABLE: "", CAUTION: "△", HOLD_REFERENCE: "(참고)", NOT_COMPARABLE: "×"}

    def show(r):
        if r.value is None or pd.isna(r.value):
            return "— " + mark[r.comparability]
        v = f"{r.value:.2f}배" if r.metric in ratio_metrics else f"{r.value:+.1%}" if r.metric == "revenue_yoy_growth" else f"{r.value:.1%}"
        return v + mark[r.comparability]

    print(f"\n== {cfg['title']} (기준일 {trend.as_of.iloc[0]}) — {cfg['score_use']}")
    print("표시: △ 비교 주의, (참고) 참고 계산, × 계산 안 함\n")
    t = trend.assign(show=[show(r) for r in trend.itertuples()])
    for company, g in t.groupby("company", sort=False):
        print(f"[{company}]")
        print(g.pivot_table(index="metric_label", columns="year", values="show", aggfunc="first", sort=False).to_string(), "\n")
    print("[계산 상태]", trend.groupby("calc_status").size().to_dict(), "| [비교 가능성]", trend.groupby("comparability").size().to_dict())
    whole = pairs[pairs.span.str.endswith("전체")]
    print("[비교 대상 기간] 인접 연도 쌍", pairs[~pairs.span.str.endswith("전체")].groupby("comparability").size().to_dict(),
          "| 2022~2025 전체", whole.comparability.str.split(" — ").str[0].value_counts().to_dict())
    print("[2021 기초값]", baseline.groupby("status").size().to_dict(), "| 다른 보고서 대조", baseline.groupby("cross_check").size().to_dict())
    print("\n[운전자본 변동] (공시 소계, 구성·완전성 미확인, 일회성 미판정, 탐색용 기준 |변동|/평균 총자산 ≥", f"{cfg['working_capital']['caution_threshold']:.0%})")
    for r in wc.itertuples():
        amt = f"{Decimal(r.wc_amount):,.0f}" if r.wc_amount else "—"
        share = f"{r.wc_to_avg_assets:.1%}" if r.wc_to_avg_assets is not None and not pd.isna(r.wc_to_avg_assets) else "—"
        print(f"  {r.company} {r.year}: {r.wc_status} {amt} | 검산 {r.sign_status}{' → ' + r.identity_target if r.identity_target else ''} | 방향 {r.ocf_direction or '—'} | {share} {r.caution}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else pd.Timestamp.today().strftime("%Y%m%d"))
