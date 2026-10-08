"""시장·외부환경 통계 연결 v1 (docs/decisions.md D18).

입력: 보존된 통계 원본(data/raw/stats), 통계 후보 점검표·기업 범위 사실·연결 판정(data/manual)
출력 (data/interim):
- market_stats_2021_2025.csv: 연도별 값, 계산 증감률과 공식 증감률 대조, 보고 업체수, 비교 주의
- market_summary_2021_2025.csv: 기간별 연평균 성장률 (두 기간을 함께 — 기간 선택에 민감)
- market_types_2021_2025.csv: 유형별 생산실적 (M02) — 유형 합 = 합계, 합계 = M01 확인 (D19)
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
from src.market.kosis_csv import parse_kosis_csv
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


def build_types(parsed: dict, config: dict, m01: pd.DataFrame, raw_dir: Path, meta: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    """유형별 생산실적: 연도별 값·전년 대비·합계 대비 비중. 유형 합과 합계, 합계와 M01을 대조하고 다르면 중단한다."""
    ts = config["type_statistic"]
    rows = {r["label"]: r["values"] for r in parsed["rows"]}
    total = rows.pop(ts["total_label"])
    problems, sum_check = [], {}
    # 유형마다 억원 단위로 따로 반올림되므로 유형 합과 합계는 최대 (유형 수 × 0.5 + 0.5)억원까지 다를 수 있다
    bound = Decimal(len(rows)) / 2 + Decimal("0.5")
    for y in parsed["years"]:
        parts = [v[y] for v in rows.values()]
        if None in parts or total[y] is None:
            sum_check[y] = "대조 불가 — 빈 칸"
            continue
        diff = sum(parts) - total[y]
        sum_check[y] = "일치" if diff == 0 else f"반올림 차이 {diff:+}억원"
        if abs(diff) > bound:
            problems.append(f"{y}: 유형 합 {sum(parts)} ≠ 합계 {total[y]} (허용 ±{bound})")
    m01_by_year = dict(zip(m01.year, m01.value))
    for y in config["years"]:
        if m01_by_year.get(y) is not None and total.get(y) is not None:
            expected = (Decimal(m01_by_year[y]) / 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)  # 백만원 → 억원
            if expected != total[y]:
                problems.append(f"{y}: 합계 {total[y]}억원 ≠ M01 {m01_by_year[y]}백만원 (반올림 {expected}억원)")
    if problems:
        raise ValueError("유형별 생산실적 대조 실패:\n" + "\n".join(problems))
    cautions = {tuple(c["pair"]): c["text"] for c in config.get("caution_pairs", [])}
    out, summary = [], []
    for label, values in [(ts["total_label"], total), *rows.items()]:
        for y in config["years"]:
            g = growth(values.get(y - 1), values.get(y))
            prior, cur = values.get(y - 1), values.get(y)
            out.append({"candidate_id": ts["candidate_id"], "type": label, "year": y, "value": cur, "unit": ts["unit"],
                        "yoy": None if g is None else float(g), "yoy_pair": f"{y - 1}→{y}",
                        "share_of_total": None if not total.get(y) or cur is None else float(cur / total[y]),
                        "change_vs_prior": None if cur is None or prior is None else cur - prior,
                        "comparability": "비교 주의" if (y - 1, y) in cautions else ("기초값" if y == config["years"][0] else "직접 비교 가능"),
                        "type_sum_check": sum_check.get(y, "") if label == ts["total_label"] else "",
                        "raw_run": raw_dir.name, "raw_sha256": meta["file"]["sha256"], "page_updated": meta["page_dates"]["updated"]})
        for a, b in config["cagr_spans"]:
            within = [t for (x, z), t in cautions.items() if a <= x and z <= b]
            summary.append({"candidate_id": ts["candidate_id"], "series": label, "span": f"{a}→{b}", "years": b - a,
                            "start": values.get(a), "end": values.get(b), "cagr": cagr(values.get(a), values.get(b), b - a),
                            "caution": "; ".join(within), "score_use": config["score_use"]})
    return pd.DataFrame(out), pd.DataFrame(summary)


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


def type_section(w, config: dict, types: pd.DataFrame, type_summary: pd.DataFrame) -> None:
    ts = config["type_statistic"]
    years = config["years"]
    spans = [f"{a}→{b}" for a, b in config["cagr_spans"]]
    w("## 2-1. 유형별 생산실적 (M02, 억원)")
    w("")
    w(f"원본: `{ts['raw_source']}/{types.raw_run.iloc[0]}` (sha256 `{types.raw_sha256.iloc[0][:12]}…`, 사용자가 KOSIS에서 직접 내려받음, "
      f"KOSIS 자료갱신일 {types.page_updated.iloc[0]} — 최초 공개일 아님). 대조: 합계 = M01(백만원 → 억원 반올림) 모두 일치. "
      f"유형 합 vs 합계: " + ", ".join(f"{r.year} {r.type_sum_check}" for r in types[types.type == ts["total_label"]].itertuples())
      + " (유형별로 따로 반올림한 차이)")
    w("")
    w("| 유형 | " + " | ".join(str(y) for y in years) + f" | {years[-1]} 비중 | " + " | ".join(f"연평균 {s}" for s in spans) + " |")
    w("|---|" + "---:|" * (len(years) + 1 + len(spans)))
    shown = [ts["total_label"], *ts["focus_types"]]
    for label in shown:
        t = types[types.type == label].set_index("year")
        sm = type_summary[type_summary.series == label].set_index("span")
        name = f"**{label}**" if label == ts["total_label"] else label
        w(f"| {name} | " + " | ".join(f"{int(t.loc[y, 'value']):,}" for y in years)
          + f" | {fmt(t.loc[years[-1], 'share_of_total'])} | " + " | ".join(fmt(sm.loc[s, 'cagr']) for s in spans) + " |")
    rest = [x for x in dict.fromkeys(types.type) if x not in shown]
    w("")
    w(f"그 밖의 유형 {len(rest)}개({', '.join(rest)})는 `market_types_2021_2025.csv`에 있다.")
    w("")
    first = f"{years[0]}→{years[1]}"
    d = types[(types.yoy_pair == first) & (types.type != ts["total_label"])].sort_values("change_vs_prior")
    total_change = types[(types.yoy_pair == first) & (types.type == ts["total_label"])].change_vs_prior.iloc[0]
    top = d.iloc[0]
    w(f"- {first} 합계 감소 {int(total_change):,}억원 중 {top.type} {int(top.change_vs_prior):,}억원"
      f"({float(top.change_vs_prior / total_change) * 100:.0f}%). 감소가 어느 유형에 몰렸는지만 보여 주며 원인은 추정하지 않는다")
    w("- 유형별 값도 책임판매업체 보고 생산금액(명목)이다. 기업의 제품 구성과 통계 분류가 맞을 때만 제품 범위 보완에 쓴다")
    w("")


def report(as_of: str, config: dict, stats: pd.DataFrame, summary: pd.DataFrame, links: pd.DataFrame, meta: dict, table: dict,
           types: pd.DataFrame | None = None, type_summary: pd.DataFrame | None = None) -> str:
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
    if types is not None:
        type_section(w, config, types, type_summary)
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
    ts = config["type_statistic"]
    type_dir = latest_raw(ts["raw_source"])
    type_meta = json.loads((type_dir / "metadata.json").read_text(encoding="utf-8"))
    parsed = parse_kosis_csv((type_dir / type_meta["file"]["name"]).read_bytes(), type_meta["encoding"])
    types, type_summary = build_types(parsed, config, stats, type_dir, type_meta)
    links = build_links()
    stats.to_csv(INTERIM / "market_stats_2021_2025.csv", index=False, encoding="utf-8-sig")
    pd.concat([summary.assign(series=config["base_statistic"]["series"]), type_summary], ignore_index=True).to_csv(
        INTERIM / "market_summary_2021_2025.csv", index=False, encoding="utf-8-sig")
    types.to_csv(INTERIM / "market_types_2021_2025.csv", index=False, encoding="utf-8-sig")
    links.to_csv(INTERIM / "market_links.csv", index=False, encoding="utf-8-sig")
    (INTERIM / "reports").mkdir(exist_ok=True)
    path = INTERIM / "reports" / f"market_stats_{as_of}.md"
    path.write_text(report(as_of, config, stats, summary, links, meta, table, types, type_summary), encoding="utf-8")
    print("[시장 통계]", stats[["year", "value", "yoy_check", "comparability"]].to_string(index=False))
    print("[연평균]", summary[["span", "cagr"]].to_string(index=False))
    print("[연결 판정]", links.groupby(["candidate_id", "judgement"]).size().to_dict(), "→", path)
    return {"M01": {"raw_run": raw_dir.name, "raw_sha256": meta["files"]["table.html"]["sha256"]},
            "M02": {"raw_run": type_dir.name, "raw_sha256": type_meta["file"]["sha256"]}}


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "20261008")
