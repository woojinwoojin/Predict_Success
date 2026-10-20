"""시장·외부환경 통계 연결 v1 (docs/decisions.md D18).

입력: 보존된 통계 원본(data/raw/stats), 통계 후보 점검표·기업 범위 사실·연결 판정(data/manual)
출력 (data/interim):
- market_stats_2021_2025.csv: 연도별 값, 계산 증감률과 공식 증감률 대조, 보고 업체수, 비교 주의
- market_summary_2021_2025.csv: 기간별 연평균 성장률 (두 기간을 함께 — 기간 선택에 민감)
- market_types_2021_2025.csv: 유형별 생산실적 (M02) — 유형 합 = 합계, 합계 = M01 확인 (D19)
- market_links.csv: 기업 × 통계 후보 연결 판정 + 근거 사실 + 작성지침 기준 적용 범위 (D20)
- reports/market_stats_{as_of}.md: 통계 추세, 기업별 범위 차이, 연결 판정
점수에는 반영하지 않는다.
"""

import hashlib
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


def pinned_raw(source: str, run_id: str | None) -> tuple[Path, dict]:
    """설정·명령행으로 지정한 수집본을 쓰고, 읽기 직전에 파일 해시를 다시 계산해 metadata와 대조한다.

    '가장 최근 폴더'를 자동으로 고르지 않는다 — 새 수집본을 쓰려면 설정의 raw_run을 바꾼다(커밋에 입력 변경이 남음).
    수집일은 공개일이 아니므로 run_id를 시점 판단에 쓰지 않는다.
    """
    root = ROOT / source
    if not run_id:
        runs = sorted(p.name for p in root.iterdir() if (p / "metadata.json").exists()) if root.exists() else []
        raise ValueError(f"{source}: raw_run 미지정 — configs/market_v1.yaml에 수집본을 고정할 것 (있는 수집본: {runs})")
    raw_dir = root / run_id
    if not (raw_dir / "metadata.json").exists():
        raise FileNotFoundError(f"지정한 수집본 없음: {raw_dir}")
    meta = json.loads((raw_dir / "metadata.json").read_text(encoding="utf-8"))
    files = meta.get("files") or {meta["file"]["name"]: meta["file"]}
    for name, info in files.items():
        actual = hashlib.sha256((raw_dir / name).read_bytes()).hexdigest()
        if actual != info["sha256"]:
            raise ValueError(f"원본 해시 불일치: {raw_dir / name} (기록 {info['sha256'][:12]}…, 실제 {actual[:12]}…)")
    return raw_dir, meta


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


def continuity_by_pair(judgments: pd.DataFrame | None) -> dict[tuple[int, int], str]:
    """Q3 판정표의 기간별 정의 연속성 (D20·D22): 인접 연도 쌍 → '상태 (판정 ID); …'. 한 구간에 여러 판정(입력 단계·집계 단계)이
    걸리면 모두 적는다. 판정이 다루지 않는 쌍은 없음."""
    out: dict[tuple[int, int], list[str]] = {}
    if judgments is None:
        return {}
    for j in judgments[judgments.question.str.startswith("Q3")].itertuples():
        years = sorted(int(y) for y in j.claim_years.split(";") if y)
        for a, b in zip(years, years[1:]):
            if b == a + 1:
                out.setdefault((a, b), []).append(f"{j.status} ({j.judgment_id})")
    return {k: "; ".join(v) for k, v in out.items()}


def pair_status(y: int, first: int, value_ok: bool, cautions: dict, continuity: dict) -> dict:
    """계산 가능 여부 · 정의 연속성 · 종합 비교 상태를 따로 만든다.

    공식 증감률과 산술로 일치해도(yoy_check) 정의 연속성이 확인된 것은 아니다 — '직접 비교 가능'은 연속성 확인 구간만.
    """
    pair = (y - 1, y)
    cont = continuity.get(pair, "판정 없음")
    if y == first:
        calc, comp = "기초값 — 전년 대비 계산 안 함", "기초값"
    elif not value_ok:
        calc, comp = "계산 불가 — 값 없음", "계산 불가"
    else:
        calc = "계산"
        all_confirmed = cont != "판정 없음" and all(part.startswith("연속성 확인") for part in cont.split("; "))
        comp = "비교 주의" if pair in cautions else ("직접 비교 가능" if all_confirmed else "연속성 미확인")
    return {"calc_status": calc, "definition_continuity": cont, "comparability": comp, "caution": cautions.get(pair, "")}


def span_continuity(a: int, b: int, continuity: dict) -> str:
    states = list(dict.fromkeys(part for y in range(a + 1, b + 1) for part in continuity.get((y - 1, y), "판정 없음").split("; ")))
    return "; ".join(states)


def build_stats(table: dict, config: dict, raw_dir: Path, meta: dict, continuity: dict | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
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
        check = ("대조 불가 — 계산값 없음" if g is None else "대조 불가 — 공식 증감률 없음" if off is None
                 else "일치" if pct1(g) == off else f"불일치 (계산 {pct1(g)}%)")
        out.append({"candidate_id": base["candidate_id"], "label": base["label"], "year": y,
                    "value": series.get(y), "unit": "백만원", "yoy": None if g is None else float(g),
                    "yoy_pair": f"{y - 1}→{y}", "official_yoy_pct": off, "yoy_check": check,
                    **{f"context_{k}": v.get(y) for k, v in context.items()},
                    **pair_status(y, config["years"][0], g is not None, cautions, continuity or {}),
                    "raw_run": raw_dir.name, "raw_sha256": meta["files"]["table.html"]["sha256"],
                    "page_updated": meta["page_dates"].get("updated"), "source": table["source"], "release_rule": table["release_rule"]})
    stats = pd.DataFrame(out)
    summary = []
    for a, b in config["cagr_spans"]:
        within = [t for (p, q), t in cautions.items() if a <= p and q <= b]
        summary.append({"candidate_id": base["candidate_id"], "span": f"{a}→{b}", "years": b - a,
                        "start": series.get(a), "end": series.get(b), "cagr": cagr(series.get(a), series.get(b), b - a),
                        "role": config.get("cagr_span_roles", {}).get(f"{a}→{b}", ""),
                        "definition_continuity": span_continuity(a, b, continuity or {}),
                        "caution": "; ".join(within) if within else "", "score_use": config["score_use"]})
    return stats, pd.DataFrame(summary)


def build_types(parsed: dict, config: dict, m01: pd.DataFrame, raw_dir: Path, meta: dict,
                continuity: dict | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
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
                        **pair_status(y, config["years"][0], g is not None, cautions, continuity or {}),
                        "type_sum_check": sum_check.get(y, "") if label == ts["total_label"] else "",
                        "raw_run": raw_dir.name, "raw_sha256": meta["file"]["sha256"], "page_updated": meta["page_dates"]["updated"]})
        for a, b in config["cagr_spans"]:
            within = [t for (x, z), t in cautions.items() if a <= x and z <= b]
            summary.append({"candidate_id": ts["candidate_id"], "series": label, "span": f"{a}→{b}", "years": b - a,
                            "start": values.get(a), "end": values.get(b), "cagr": cagr(values.get(a), values.get(b), b - a),
                            "role": config.get("cagr_span_roles", {}).get(f"{a}→{b}", ""),
                            "definition_continuity": span_continuity(a, b, continuity or {}),
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
    if "guideline_evidence" in links:  # 작성지침 사실(S)·판정(J) (D20)
        known = set(read_manual("stat_guideline_facts.csv").fact_id) | set(read_manual("stat_guideline_judgments.csv").judgment_id)
        missing = sorted({e for ev in links.guideline_evidence for e in ev.split(";") if e and e not in known})
        if missing:
            raise ValueError(f"연결 판정이 없는 작성지침 근거를 가리킴: {missing}")
    links["evidence_detail"] = [" | ".join(f"{e} {facts.loc[e, 'topic']}: {facts.loc[e, 'fact']} (접수 {facts.loc[e, 'rcept_no']})"
                                           for e in ev.split(";") if e) for ev in links.evidence]
    return links


EVIDENCE_LEVELS = {"명시적 확인", "간접 근거", "미확인"}
# Q1·Q2는 정의 확인, Q3는 연속성 — '변경 언급을 찾지 못함'과 '연속성 확인'을 구분한다 (D20)
JUDGMENT_STATUS = {"Q1": {"확인", "부분 확인", "미확인"}, "Q2": {"확인", "부분 확인", "미확인"},
                   "Q3": {"변경 확인", "연속성 확인", "미확인"}, "공통": {"확인", "미확인"}}
FULL_STATUS = {"확인", "변경 확인", "연속성 확인"}  # 주장 연도 전부에 명시 확인 근거가 필요한 상태


def build_guidelines() -> tuple[pd.DataFrame, pd.DataFrame]:
    """작성지침 사실·판정(D20)을 읽고 근거 연결을 확인한다. 하나라도 어긋나면 중단.

    - 사실의 문서는 등록부에 있어야 하고, 보존된 원본의 sha256이 기록과 같아야 한다
    - 미확보 문서만 가리키는 사실은 '미확인'이어야 한다 (찾지 못함을 확인으로 쓰지 않음)
    - 판정의 근거는 기록된 사실이어야 하고, 상태는 질문별로 정한 값만 쓴다
    - 확인 근거(evidence)와 미해결 쟁점 근거(open_evidence)를 나눈다. 확인 근거에 미확인 사실을 넣지 않고,
      '확인'·'변경 확인'·'연속성 확인'은 claim_years의 모든 연도에 그해 명시 확인 사실(fact_years)이 있어야 한다
    """
    docs = read_manual("stat_guideline_docs.csv").set_index("doc_id")
    facts, judgments = read_manual("stat_guideline_facts.csv"), read_manual("stat_guideline_judgments.csv")
    errors = []
    for f in facts.itertuples():
        ids = f.doc_id.split(";")
        if unknown := [d for d in ids if d not in docs.index]:
            errors.append(f"{f.fact_id}: 등록부에 없는 문서 {unknown}")
            continue
        if f.evidence_level not in EVIDENCE_LEVELS:
            errors.append(f"{f.fact_id}: 근거 수준 '{f.evidence_level}'")
        kept = [d for d in ids if docs.loc[d, "status"] in ("수집", "기존 보존")]
        if not kept and f.evidence_level != "미확인":
            errors.append(f"{f.fact_id}: 보존 원본 없이 '{f.evidence_level}'")
        paths, hashes = [p for p in f.raw_path.split(";") if p], [h for h in f.sha256.split(";") if h]
        if len(paths) != len(kept) or len(hashes) != len(kept):
            errors.append(f"{f.fact_id}: 원본 경로·해시 수가 보존 문서 수({len(kept)})와 다름")
            continue
        for path, digest in zip(paths, hashes):
            file = ROOT / path
            if not file.exists():
                errors.append(f"{f.fact_id}: 원본 없음 {path}")
            elif hashlib.sha256(file.read_bytes()).hexdigest() != digest:
                errors.append(f"{f.fact_id}: 원본 해시 불일치 {path}")
    by_id = facts.set_index("fact_id")
    for j in judgments.itertuples():
        q = j.question.split()[0]
        if j.status not in JUDGMENT_STATUS.get(q, set()):
            errors.append(f"{j.judgment_id}: '{j.question}'에 쓸 수 없는 상태 '{j.status}'")
        support, open_ = [e for e in j.evidence.split(";") if e], [e for e in j.open_evidence.split(";") if e]
        if missing := [e for e in support + open_ if e not in by_id.index]:
            errors.append(f"{j.judgment_id}: 기록되지 않은 사실 {missing}")
            continue
        if "superseded_by" in by_id and (old := [e for e in support + open_ if by_id.loc[e, "superseded_by"]]):
            errors.append(f"{j.judgment_id}: 대체된 사실을 근거로 씀 {old} — superseded_by의 사실로 바꿀 것")
        # 승격 검사: '근거가 연결됐으니 확인'을 막는다. 미확인 사실은 미해결 쟁점 근거(open_evidence)로만
        if weak := [e for e in support if by_id.loc[e, "evidence_level"] == "미확인"]:
            errors.append(f"{j.judgment_id}: 확인 근거에 미확인 사실 {weak} — open_evidence로 옮길 것")
        if j.status != "미확인" and not support:
            errors.append(f"{j.judgment_id}: '{j.status}'인데 확인 근거 없음")
        if j.status in FULL_STATUS:
            # 주장하는 연도마다 그해 문서로 명시 확인된 사실이 있어야 한다 (현재본 공지·다른 연도 문서는 간접)
            explicit = {y for e in support if by_id.loc[e, "evidence_level"] == "명시적 확인"
                        for y in by_id.loc[e, "fact_years"].split(";") if y}
            claim = [y for y in j.claim_years.split(";") if y]
            if not claim:
                errors.append(f"{j.judgment_id}: '{j.status}'인데 claim_years 없음")
            elif uncovered := [y for y in claim if y not in explicit]:
                errors.append(f"{j.judgment_id}: '{j.status}'인데 명시 확인 근거가 없는 연도 {uncovered}")
    if errors:
        raise ValueError("작성지침 기록 확인 실패:\n" + "\n".join(errors))
    return facts, judgments


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
    w("- 정의 연속성은 M01과 같다(같은 보고 자료, J05): 연도별 상태는 `market_types_2021_2025.csv`의 definition_continuity·comparability")
    w("")


def guideline_section(w, facts: pd.DataFrame, judgments: pd.DataFrame) -> None:
    w("## 1-1. 작성지침 확인 — 가격·보고 범위·연속성 (D20)")
    w("")
    w("통계 정의를 확인하는 것과 점수에 쓸 자격을 얻는 것은 다른 단계다. 이 절은 정의 확인과 기업별 적용 범위까지이며, 점수는 바꾸지 않는다.")
    w("")
    w("| ID | 질문 | 적용 연도 | 상태 | 확인한 것 | 남은 불확실성 | 결과 | 확인 근거 | 미해결 쟁점 근거 |")
    w("|---|---|---|---|---|---|---|---|---|")
    for j in judgments.itertuples():
        w(f"| {j.judgment_id} | {j.question} | {j.applies_years} | **{j.status}** | {j.confirmed} | {j.remaining_uncertainty} | {j.consequence} | "
          f"{j.evidence.replace(';', ', ') or '—'} | {j.open_evidence.replace(';', ', ') or '—'} |")
    w("")
    w("| ID | 질문 | 주제 | 적용 연도 | 근거 수준 | 문서 (판·위치) | 해석 |")
    w("|---|---|---|---|---|---|---|")
    for f in facts.itertuples():
        w(f"| {f.fact_id} | {f.question} | {f.topic} | {f.applies_years} | {f.evidence_level} | {f.doc_id.replace(';', ', ')} ({f.doc_version}; {f.location}) | {f.interpretation} |")
    w("")
    w("- 근거 수준: 명시적 확인(해당 연도 문서에 적혀 있음) / 간접 근거(다른 연도 문서·게시판 현재본·구조에서 추론) / 미확인(문서 미확보 또는 찾지 못함). 원문 인용·원본 경로·해시는 `data/manual/stat_guideline_facts.csv`")
    w("- 게시판 공지는 같은 주소에서 본문이 갱신된다(G04: 등록일 2021-01-06인 글의 본문이 2025년도 기준). 공지 현재본을 과거 연도의 지침으로 쓰지 않는다")
    w("")


def report(as_of: str, config: dict, stats: pd.DataFrame, summary: pd.DataFrame, links: pd.DataFrame, meta: dict, table: dict,
           types: pd.DataFrame | None = None, type_summary: pd.DataFrame | None = None,
           guidelines: tuple[pd.DataFrame, pd.DataFrame] | None = None) -> str:
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
    if guidelines is not None:
        guideline_section(w, *guidelines)
    w("## 2. 국내 화장품 생산금액 추세")
    w("")
    ctx = [c for c in stats.columns if c.startswith("context_")]
    w("| 연도 | 생산금액 (백만원) | 전년 대비 | 공식 증감률 | 대조 | " + " | ".join(c.replace("context_", "보고 ") for c in ctx) + " | 정의 연속성 (Q3) | 비교 |")
    w("|---|---:|---:|---:|---|" + "---:|" * len(ctx) + "---|---|")
    for r in stats.itertuples():
        extra = " | ".join(f"{int(getattr(r, c)):,}" if getattr(r, c) is not None else "" for c in ctx)
        w(f"| {r.year} | {int(r.value):,} | {fmt(r.yoy) if r.yoy is not None and not pd.isna(r.yoy) else '—'} | "
          f"{'' if r.official_yoy_pct is None else str(r.official_yoy_pct) + '%'} | {r.yoy_check or '—'} | {extra} | {r.definition_continuity} | {r.comparability}{(' — ' + r.caution) if r.caution else ''} |")
    w("")
    roles = config.get("cagr_span_roles", {})
    w("| 기간 | 용도 | 연평균 성장률 | 정의 연속성 (Q3) | 주의 |")
    w("|---|---|---:|---|---|")
    for s in summary.itertuples():
        w(f"| {s.span} ({s.years}년) | {roles.get(s.span, '—')} | {fmt(s.cagr)} | {s.definition_continuity} | {s.caution or '—'} |")
    w("")
    w("- '대조'는 계산 증감률과 공식 증감률의 산술 일치일 뿐이다. 정의·범위가 연속인지는 '정의 연속성'(작성지침 판정)이 따로 말하며, "
      "'직접 비교 가능'은 연속성이 확인된 구간에만 붙는다")
    w("")
    w("- 연평균 성장률은 시작 연도에 민감하다. 2021에서 2022로 크게 줄었기 때문에 2022 시작은 높게, 2021 시작은 낮게 나온다. 유리한 숫자를 고르는 문제가 아니다: "
      "2022→2025는 재무 회고 추세와 맞춘 기본 비교 기간(연속성 미확인), 2021→2025는 시작점 민감도 확인용. 어느 쪽도 '시장 성장률'로 단정하지 않는다")
    w("- 보고 업체수가 해마다 늘었다. 합계는 그해 생산실적을 보고한 책임판매업체의 집계이므로, 증감에는 집계 대상의 변화도 섞여 있을 수 있다(확인 안 함)")
    w("- 명목 금액이다 (단가 × 수량, 물가 조정 없음 — J01). 가격 효과와 수량 효과를 나누지 않으며 실질 성장으로 해석하지 않는다")
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
        if "application_scope" in sub:
            w("")
            w("| 기업 | 통계가 설명할 수 있는 범위 | 간접 지표 역할 | 점수 자격 | 작성지침 근거 |")
            w("|---|---|---|---|---|")
            for r in sub.itertuples():
                w(f"| {r.company} | {r.application_scope} | {r.proxy_role} | {r.score_eligibility} | {r.guideline_evidence.replace(';', ', ')} |")
        w("")
    w("어느 기업도 '비교 가능'으로 판정하지 않았다. 통계는 책임판매업자가 보고한 국내 제조 화장품 금액이고, 기업 값은 연결 매출이다. "
      "보고 주체·범위가 다르다는 것은 2022·2024년분 문서로 확인했다(J02). 가격 기준은 '부분 확인'이다(J01): 부가세 포함 출하가 기준이라는 설명은 "
      "2017년분 작성요령과 수집 시점의 공지 현재본에 있을 뿐, 분석 대상 연도(2021~2025)의 작성요령에는 그 문구가 없다. "
      "국내 고객사의 ODM 물량은 고객사 실적에 들어가므로 통계는 ODM 수요의 직접 측정이 아니라 고객 측 생산 활동의 간접 지표 후보일 뿐이다. "
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


def main(as_of: str, runs: dict | None = None) -> dict:
    """runs: {"M01": run_id, "M02": run_id} — 설정의 raw_run 대신 쓸 수집본 (과거 실행 재현용)."""
    config = yaml.safe_load((ROOT / "configs" / "market_v1.yaml").read_text(encoding="utf-8"))
    runs = runs or {}
    base = config["base_statistic"]
    raw_dir, meta = pinned_raw(base["raw_source"], runs.get(base["candidate_id"]) or base.get("raw_run"))
    table = parse_table((raw_dir / "table.html").read_text(encoding="utf-8"))
    guidelines = build_guidelines()
    continuity = continuity_by_pair(guidelines[1])
    stats, summary = build_stats(table, config, raw_dir, meta, continuity)
    bad = stats[stats.yoy_check.str.startswith("불일치")]
    if not bad.empty:
        raise ValueError(f"계산 증감률이 공식 표와 다름:\n{bad[['year', 'yoy_check']]}")
    ts = config["type_statistic"]
    type_dir, type_meta = pinned_raw(ts["raw_source"], runs.get(ts["candidate_id"]) or ts.get("raw_run"))
    parsed = parse_kosis_csv((type_dir / type_meta["file"]["name"]).read_bytes(), type_meta["encoding"])
    types, type_summary = build_types(parsed, config, stats, type_dir, type_meta, continuity)
    links = build_links()
    stats.to_csv(INTERIM / "market_stats_2021_2025.csv", index=False, encoding="utf-8-sig")
    pd.concat([summary.assign(series=config["base_statistic"]["series"]), type_summary], ignore_index=True).to_csv(
        INTERIM / "market_summary_2021_2025.csv", index=False, encoding="utf-8-sig")
    types.to_csv(INTERIM / "market_types_2021_2025.csv", index=False, encoding="utf-8-sig")
    links.to_csv(INTERIM / "market_links.csv", index=False, encoding="utf-8-sig")
    (INTERIM / "reports").mkdir(exist_ok=True)
    path = INTERIM / "reports" / f"market_stats_{as_of}.md"
    path.write_text(report(as_of, config, stats, summary, links, meta, table, types, type_summary, guidelines), encoding="utf-8")
    print("[시장 통계]", stats[["year", "value", "yoy_check", "definition_continuity", "comparability"]].to_string(index=False))
    print("[연평균]", summary[["span", "cagr"]].to_string(index=False))
    print("[연결 판정]", links.groupby(["candidate_id", "judgement"]).size().to_dict(), "→", path)
    print("[작성지침]", dict(zip(guidelines[1].judgment_id + " " + guidelines[1].question, guidelines[1].status)))
    return {"M01": {"raw_run": raw_dir.name, "raw_sha256": meta["files"]["table.html"]["sha256"]},
            "M02": {"raw_run": type_dir.name, "raw_sha256": type_meta["file"]["sha256"]}}


if __name__ == "__main__":
    # python -m src.market.run_market 20261020 [M01=<run_id>] [M02=<run_id>]
    args = sys.argv[1:]
    main(args[0] if args and "=" not in args[0] else "20261008", dict(a.split("=", 1) for a in args if "=" in a))
