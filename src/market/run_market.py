"""시장·외부환경 통계 연결 v1 (docs/decisions.md D18).

입력: 보존된 통계 원본(data/raw/stats), 통계 후보 점검표·기업 범위 사실·연결 판정(data/manual)
출력 (data/interim):
- market_stats_2021_2025.csv: 연도별 값, 계산 증감률과 공식 증감률 대조, 보고 업체수, 비교 주의
- market_summary_2021_2025.csv: 기간별 연평균 성장률 (두 기간을 함께 — 기간 선택에 민감)
- market_links.csv: 기업 × 통계 후보 연결 판정 + 근거 사실
- reports/market_stats_{as_of}.md: 통계 추세, 기업별 범위 차이, 연결 판정
점수에는 반영하지 않는다.
"""

import json
import sys
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pandas as pd
import yaml

from src.market.index_table import parse_table
from src.normalization.build_facts import INTERIM, ROOT

MANUAL = ROOT / "data" / "manual"


def latest_raw(source: str) -> Path:
    runs = sorted(p for p in (ROOT / source).iterdir() if (p / "metadata.json").exists())
    if not runs:
        raise FileNotFoundError(f"{source}에 수집본 없음 — python -m src.collectors.stats")
    return runs[-1]


def growth(a: Decimal | None, b: Decimal | None) -> Decimal | None:
    if a is None or b is None or a <= 0:
        return None
    return b / a - 1


def cagr(a: Decimal | None, b: Decimal | None, years: int) -> float | None:
    if a is None or b is None or a <= 0 or b <= 0 or years <= 0:
        return None
    return float(b / a) ** (1 / years) - 1


def pct1(x) -> Decimal:
    return (Decimal(str(x)) * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def build_stats(table: dict, config: dict, raw_dir: Path, meta: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    base = config["base_statistic"]
    rows = {r["label"]: r for r in table["rows"] if "of" not in r}
    growth_rows = {r["of"]: r for r in table["rows"] if "of" in r}
    series = rows[base["series"]]["values"]
    official = growth_rows.get(base["series"], {}).get("values", {})
    context = {name: rows[name]["values"] for name in base["context_series"] if name in rows}
    cautions = {tuple(c["pair"]): c["text"] for c in config.get("caution_pairs", [])}
    out = []
    for y in config["years"]:
        g = growth(series.get(y - 1), series.get(y))
        off = official.get(y)
        check = "" if g is None or off is None else ("일치" if pct1(g) == off else f"불일치 (계산 {pct1(g)}%)")
        out.append({"candidate_id": base["candidate_id"], "label": base["label"], "year": y,
                    "value": series.get(y), "unit": "백만원", "yoy": None if g is None else float(g),
                    "yoy_pair": f"{y - 1}→{y}", "official_yoy_pct": off, "yoy_check": check,
                    **{f"context_{k}": v.get(y) for k, v in context.items()},
                    "comparability": "비교 주의" if (y - 1, y) in cautions else ("기초값" if y == config["years"][0] else "직접 비교 가능"),
                    "caution": cautions.get((y - 1, y), ""),
                    "raw_run": raw_dir.name, "raw_sha256": meta["files"]["table.html"]["sha256"],
                    "page_updated": meta["page_dates"].get("updated"), "source": table["source"], "release_rule": table["release_rule"]})
    stats = pd.DataFrame(out)
    summary = []
    for a, b in config["cagr_spans"]:
        within = [t for (p, q), t in cautions.items() if a <= p and q <= b]
        summary.append({"candidate_id": base["candidate_id"], "span": f"{a}→{b}", "years": b - a,
                        "start": series.get(a), "end": series.get(b), "cagr": cagr(series.get(a), series.get(b), b - a),
                        "caution": "; ".join(within) if within else "", "score_use": config["score_use"]})
    return stats, pd.DataFrame(summary)


def read_manual(name: str) -> pd.DataFrame:
    return pd.read_csv(MANUAL / name, dtype=str, keep_default_na=False, encoding="utf-8-sig")


def build_links() -> pd.DataFrame:
    links, cands = read_manual("market_links.csv"), read_manual("market_stat_candidates.csv")
    facts = read_manual("company_scope_facts.csv").set_index("fact_id")
    links = links.merge(cands[["candidate_id", "name", "status"]].rename(columns={"name": "candidate", "status": "candidate_status"}),
                        on="candidate_id", how="left")
    missing = sorted({e for ev in links.evidence for e in ev.split(";") if e and e not in facts.index})
    if missing:
        raise ValueError(f"연결 판정이 없는 근거를 가리킴: {missing}")
    links["evidence_detail"] = [" | ".join(f"{e} {facts.loc[e, 'topic']}: {facts.loc[e, 'fact']} (접수 {facts.loc[e, 'rcept_no']})"
                                           for e in ev.split(";") if e) for ev in links.evidence]
    return links


def fmt(v, digits=1) -> str:
    return "" if v is None or (isinstance(v, float) and pd.isna(v)) else f"{v * 100:.{digits}f}%"


def report(as_of: str, config: dict, stats: pd.DataFrame, summary: pd.DataFrame, links: pd.DataFrame, meta: dict, table: dict) -> str:
    cands = read_manual("market_stat_candidates.csv")
    facts = read_manual("company_scope_facts.csv")
    L = []
    w = L.append
    w(f"# 시장·외부환경 통계 연결 — {config['title']}")
    w("")
    w(f"> 질문: {config['question']}")
    w(">")
    w(f"> 점수: **{config['score_use']}**. 이 문서의 값은 국내 생산금액의 변화이며 글로벌 수요나 ODM 매출 성장률을 직접 뜻하지 않는다.")
    w("")
    w(f"| 항목 | 값 |\n|---|---|\n| 기준일 | {as_of} |\n| 원본 | `{config['base_statistic']['raw_source']}/{stats.raw_run.iloc[0]}` "
      f"(table.html sha256 `{stats.raw_sha256.iloc[0][:12]}…`, 수집 {meta['collected_at']}) |\n"
      f"| 통계 | {table['source']} — {cands.set_index('candidate_id').loc['M01', 'provider']} |\n"
      f"| 페이지 갱신일 | {meta['page_dates'].get('updated')} — 수집본의 갱신일이며 각 연도 값의 최초 공개일이 아님 |\n"
      f"| 표의 통계공표시기 | {table['release_rule']} |")
    w("")
    w("## 1. 통계 후보 점검")
    w("")
    w("| ID | 후보 | 답할 질문 | 지역 | 측정 | 금액·단위 | 기간·확보 | 공개 시점 | 상태 | V1 용도 |")
    w("|---|---|---|---|---|---|---|---|---|---|")
    for c in cands.itertuples():
        w(f"| {c.candidate_id} | {c.name} | {c.question} | {c.region or '—'} | {c.measure or '—'} | {c.amount_or_volume or '—'} {('· ' + c.currency_unit) if c.currency_unit else ''} | "
          f"{c.years_needed or '—'} / 확보 {c.years_collected} | {c.page_updated or '—'}; {c.first_release or '최초 공개일 —'} | {c.status} | {c.v1_use} |")
    w("")
    m01 = cands.set_index("candidate_id").loc["M01"]
    w(f"**M01 정의·범위:** {m01.reporter}. 포함·제외: {m01.include_exclude}. 가격 평가 기준: {m01.valuation_basis}. 개정 이력: {m01.revision_history}.")
    w("")
    w("## 2. 국내 화장품 생산금액 추세")
    w("")
    ctx = [c for c in stats.columns if c.startswith("context_")]
    w("| 연도 | 생산금액 (백만원) | 전년 대비 | 공식 증감률 | 대조 | " + " | ".join(c.replace("context_", "보고 ") for c in ctx) + " | 비교 |")
    w("|---|---:|---:|---:|---|" + "---:|" * len(ctx) + "---|")
    for r in stats.itertuples():
        extra = " | ".join(f"{int(getattr(r, c)):,}" if getattr(r, c) is not None else "" for c in ctx)
        w(f"| {r.year} | {int(r.value):,} | {fmt(r.yoy) if r.yoy is not None and not pd.isna(r.yoy) else '—'} | "
          f"{'' if r.official_yoy_pct is None else str(r.official_yoy_pct) + '%'} | {r.yoy_check or '—'} | {extra} | {r.comparability}{(' — ' + r.caution) if r.caution else ''} |")
    w("")
    w("| 기간 | 연평균 성장률 | 주의 |")
    w("|---|---:|---|")
    for s in summary.itertuples():
        w(f"| {s.span} ({s.years}년) | {fmt(s.cagr)} | {s.caution or '—'} |")
    w("")
    w("- 연평균 성장률은 시작 연도에 민감하다. 2021에서 2022로 크게 줄었기 때문에 2022 시작은 높게, 2021 시작은 낮게 나온다. 어느 쪽도 '시장 성장률'로 단정하지 않는다")
    w("- 보고 업체수가 해마다 늘었다. 합계는 그해 생산실적을 보고한 책임판매업체의 집계이므로, 증감에는 집계 대상의 변화도 섞여 있을 수 있다(확인 안 함)")
    w("- 명목 금액이다. 가격 평가 기준을 작성지침에서 확인하기 전에는 실질 성장으로 해석하지 않는다")
    w("")
    w("## 3. 기업별 범위 차이와 연결 판정")
    w("")
    w("판정: " + " / ".join(f"**{k}** — {v}" for k, v in config["judgements"].items()))
    w("")
    for cid in links.candidate_id.unique():
        sub = links[links.candidate_id == cid]
        w(f"### {cid} {sub.candidate.iloc[0]}")
        w("")
        if (sub.judgement == "연결 보류").all():
            w("| 기업 | 판정 | 사유 | 근거 |")
            w("|---|---|---|---|")
            for r in sub.itertuples():
                w(f"| {r.company} | {r.judgement} | {r.differences} | {r.evidence.replace(';', ', ')} |")
        else:
            w("| 기업 | 판정 | 용도 | 지역 | 제품 | 측정 대상 | 기간 | 차이 | 근거 |")
            w("|---|---|---|---|---|---|---|---|---|")
            for r in sub.itertuples():
                w(f"| {r.company} | **{r.judgement}** | {r.use} | {r.region_match} | {r.product_match} | {r.measure_match} | {r.period_match} | "
                  f"{r.differences} | {r.evidence.replace(';', ', ')} |")
        w("")
    w("어느 기업도 '비교 가능'으로 판정하지 않았다. 통계(책임판매업체가 보고한 생산금액)와 기업 매출의 측정 대상·평가 기준이 같다고 확인되지 않았으므로 "
      "시장점유율이나 '시장 대비 성장 격차'를 계산하지 않는다.")
    w("")
    w("### 근거 사실 (사업보고서 원문)")
    w("")
    w("| ID | 기업 | 주제 | 사실 | 값 | 접수번호 | 원문 위치 | 상태 |")
    w("|---|---|---|---|---|---|---|---|")
    for f in facts.itertuples():
        w(f"| {f.fact_id} | {f.company} | {f.topic} | {f.fact} | {f.values} | {f.rcept_no} | {f.source_location or '—'} | {f.status} |")
    w("")
    w("## 4. 하지 않은 것")
    w("")
    w("- 국내 생산금액과 수출액을 합쳐 시장 규모를 만들지 않음 (서로 다른 흐름)")
    w("- 통계 이름을 'ODM 시장 규모'·'소비자 시장 규모'로 바꾸지 않음")
    w("- 갱신일을 각 연도 값의 최초 공개일로 쓰지 않음 — 당시 이용 가능 정보만 쓴 분석은 과거 통계본 확보 후")
    w("- '조건부 참고' 통계를 점수에 넣지 않음. README 6.4 지표(대응 산업 매출 추세 등)는 아직 미구현")
    return "\n".join(L) + "\n"


def main(as_of: str) -> dict:
    config = yaml.safe_load((ROOT / "configs" / "market_v1.yaml").read_text(encoding="utf-8"))
    raw_dir = latest_raw(config["base_statistic"]["raw_source"])
    meta = json.loads((raw_dir / "metadata.json").read_text(encoding="utf-8"))
    table = parse_table((raw_dir / "table.html").read_text(encoding="utf-8"))
    stats, summary = build_stats(table, config, raw_dir, meta)
    bad = stats[stats.yoy_check.str.startswith("불일치")]
    if not bad.empty:
        raise ValueError(f"계산 증감률이 공식 표와 다름:\n{bad[['year', 'yoy_check']]}")
    links = build_links()
    stats.to_csv(INTERIM / "market_stats_2021_2025.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(INTERIM / "market_summary_2021_2025.csv", index=False, encoding="utf-8-sig")
    links.to_csv(INTERIM / "market_links.csv", index=False, encoding="utf-8-sig")
    (INTERIM / "reports").mkdir(exist_ok=True)
    path = INTERIM / "reports" / f"market_stats_{as_of}.md"
    path.write_text(report(as_of, config, stats, summary, links, meta, table), encoding="utf-8")
    print("[시장 통계]", stats[["year", "value", "yoy_check", "comparability"]].to_string(index=False))
    print("[연평균]", summary[["span", "cagr"]].to_string(index=False))
    print("[연결 판정]", links.groupby(["candidate_id", "judgement"]).size().to_dict(), "→", path)
    return {"raw_run": raw_dir.name, "raw_sha256": meta["files"]["table.html"]["sha256"]}


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "20261008")
