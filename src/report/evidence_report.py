"""기업 1개사의 증거 연결 보고서 (Markdown): python -m src.report.evidence_report [corp_code]  (docs/decisions.md D15)

새 계산을 하지 않는다. 파이프라인이 만든 결과(data/interim/)만 읽어, 지금 쌓인 결과가 사용자의 질문(docs/problem.md)에
답하는지 확인하는 용도다. 담는 것:
  1. 기준일·재무기준·규칙 버전과 '회고 추세' 설명
  2. 2022~2025 지표 표와 비교 주의 표시 (비교 대상 기간 포함)
  3. 운전자본 변동 금액·검산 상태·규모 경고
  4. 각 값에서 접수번호·원문 위치로 이어지는 근거 (근거 ID)
  5. 현재 답할 수 있는 질문과 보류된 질문, 종합점수 보류 사유
출력: data/interim/reports/{corp_code}_evidence_{기준일}.md
"""

import hashlib
import json
import sys
from decimal import Decimal

import pandas as pd
import yaml

from src.normalization.baseline import same_period_rows
from src.normalization.build_facts import INTERIM, RAW, ROOT
from src.normalization.selection import load_published_filings

MANUAL = ROOT / "data" / "manual"
RATIO = {"interest_coverage", "current_ratio_hfs", "current_ratio_reported", "cash_to_short_term_debt", "inventory_turnover"}
MARK = {"직접 비교 가능": "", "비교 주의": " △", "비교 보류 — 참고 계산": " (참고)", "비교 불가 — 계산 안 함": ""}
STATEMENT = {"BS": "재무상태표", "IS": "손익계산서", "CIS": "포괄손익계산서", "CF": "현금흐름표"}
COLUMN = {"thstrm_amount": "당기", "frmtrm_amount": "전기", "bfefrmtrm_amount": "전전기"}
ACCOUNT_KO = {"revenue": "매출", "cost_of_sales": "매출원가", "operating_income": "영업이익", "net_income": "당기순이익",
              "operating_cash_flow": "영업현금흐름", "total_assets": "총자산", "total_liabilities": "총부채", "current_assets": "유동자산(보고)",
              "current_liabilities": "유동부채(보고)", "cash": "현금및현금성자산", "inventory": "재고자산", "interest_expense": "이자비용",
              "short_term_interest_bearing_debt": "단기 이자부채", "adjusted_current_assets": "유동자산(매각예정 포함)",
              "adjusted_current_liabilities": "유동부채(매각예정 포함)"}


# 계산 입력 상태 — 매핑·계산에 쓸 수 있는지. 원문 검증 여부와 다르다 (D16)
STATUS_KO = {"mapped": "정상", "candidate": "참고 (후보값)", "review_needed": "계산 안 함 (확인 필요)", "empty_value": "계산 안 함 (값 없음)",
             "no_data": "계산 안 함 (자료 없음)", "held": "계산 안 함 (보류)", "not_applicable": "해당 없음", "없음": "없음"}


def esc(text) -> str:
    """표 칸 안의 | 는 칸 구분자로 읽히므로 이스케이프한다."""
    return str(text).replace("|", r"\|")


def read(name: str, folder=INTERIM) -> pd.DataFrame:
    return pd.read_csv(folder / name, dtype=str, keep_default_na=False, encoding="utf-8-sig")


def fmt_value(metric: str, v: str) -> str:
    if v in ("", None):
        return "—"
    x = float(v)
    return f"{x:.2f}배" if metric in RATIO else f"{x:+.1%}" if metric == "revenue_yoy_growth" else f"{x:.1%}"


def eok(v) -> str:
    return "—" if v in ("", None) or (isinstance(v, float) and pd.isna(v)) else f"{Decimal(str(v)) / Decimal(10**8):,.1f}억"


def latest_run() -> tuple[str, dict, list[str]]:
    """가장 최근 실행 기록과, 지금 data/interim 파일이 그 기록의 출력과 같은지 확인한 결과."""
    runs = sorted((INTERIM / "runs").glob("*/manifest.json"))
    if not runs:
        return "", {}, ["실행 기록 없음"]
    manifest = json.loads(runs[-1].read_text(encoding="utf-8"))
    problems = [f"{name}이 실행 기록과 다름" for name, h in manifest["outputs"].items()
                if (INTERIM / name).exists() and hashlib.sha256((INTERIM / name).read_bytes()).hexdigest() != h]
    return manifest["run_id"], manifest, problems


NO_DIRECT = "현재 보고서 원문 대조 미실시"
EMPTY = {"value": "", "status": "없음", "report": "", "where": "", "method": "", "link": "", "verify": "미대조", "where_basis": ""}


class Evidence:
    """근거 레지스트리: 계정·연도마다 근거 ID 하나 (접수번호 → 원문 위치)."""

    def __init__(self, corp: str, fs: str, published: dict, as_of: str):
        self.corp, self.fs, self.published, self.as_of = corp, fs, published, as_of
        facts = read("dart_facts.csv")
        self.facts = facts[(facts.corp_code == corp) & (facts.fs_div == fs) & (facts.value_status == "parsed")]
        self.fact_by_id = self.facts.set_index("row_id")
        self.fact_records = self.facts.to_dict("records")
        check = read("account_check.csv")
        self.check = check[(check.corp_code == corp) & (check.fs_div == fs)]
        ev = read("account_check_evidence.csv")
        self.ev = ev[(ev.corp_code == corp) & (ev.fs_div == fs)]
        base = read("trend_baseline_2021.csv")
        self.base = base[(base.corp_code == corp) & (base.fs_div == fs)]
        notes = read("note_confirmations.csv", MANUAL)
        self.notes = notes[(notes.corp_code == corp) & (notes.fs_div == fs) & (notes.status == "confirmed")]
        self.ids: dict[tuple[str, int], str] = {}
        self.rows: list[dict] = []

    def section(self, sj_div: str) -> str:
        prefix = "연결 " if self.fs == "CFS" else ""
        part = "2. 연결재무제표" if self.fs == "CFS" else "4. 재무제표"
        return f"III. 재무에 관한 사항 > {part} > {prefix}{STATEMENT.get(sj_div, sj_div)}"

    def report_name(self, rcept: str) -> str:
        f = self.published.get(rcept, {})
        return f"{f.get('report_nm', '사업보고서')} (접수 {rcept}, 공시일 {f.get('rcept_dt', '?')})"

    def ref(self, account: str, year: int) -> str:
        key = (account, year)
        if key in self.ids:
            return self.ids[key]
        eid = f"E{len(self.ids) + 1:02d}"
        self.ids[key] = eid
        self.rows.append({"id": eid, "account": ACCOUNT_KO.get(account, account), "year": year, **self._locate(account, year)})
        return eid

    def _locate(self, account: str, year: int) -> dict:
        if year == 2021:
            b = self.base[self.base.canonical_account == account]
            if b.empty:
                return {**EMPTY}
            b = b.iloc[0]
            verify = {"일치": "API 간 대조 — 다른 보고서와 일치", "불일치": "API 간 대조 — 불일치"}.get(
                b.cross_check, f"미대조 — API 간 대조 불가: {b.note.split(': ', 1)[-1] if '대조 없음' in b.note else '대조 결과 없음'} · {NO_DIRECT}")
            return {"value": b.value, "status": b.status, "report": self.report_name(b.rcept_no),
                    "where": f"{self.section('CIS' if account == 'revenue' else 'BS')} > 전기 열. {b.note}",
                    "method": f"{b.extraction_method} — 2021 기초값 예외 (2022 보고서 전기 열)",
                    "verify": verify, "where_basis": "API 정보로 구성",
                    "link": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={b.rcept_no}"}
        c = self.check[(self.check.canonical_account == account) & (self.check.bsns_year == str(year))]
        if c.empty:
            return {**EMPTY}
        c = c.iloc[0]
        rcept = c.rcept_no
        out = {"value": c.value, "status": c.status, "report": self.report_name(rcept), "method": c.extraction_method or "—",
               "verify": f"미대조 — 계산용 값 없음 · {NO_DIRECT}" if c.status not in ("mapped", "candidate") else "미대조", "where_basis": "API 정보로 구성",
               "link": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept}"}
        manual = self.notes[(self.notes.canonical_account == account) & (self.notes.rcept_no == rcept)
                            & (self.notes.period_end.str[:4] == str(year))]
        if "manual" in (c.extraction_method or "") and not manual.empty:
            m = manual.iloc[0]
            return {**out, "verify": f"직접 대조 — 수기 확인 {m.confirmation_id}", "where_basis": "원문에서 확인",
                    "where": f"수기 확인 {m.confirmation_id}: {m.source_location} (금액 {m.amount_check}, 범위 {m.scope_check or '—'}, 구성 {m.component_check or '—'})"}
        if account.startswith("adjusted_"):
            hfs_direct = c.extraction_method == "api_rule+manual"
            base_account = "current_assets" if account == "adjusted_current_assets" else "current_liabilities"
            base_verify = self._cross_check(self.ev[(self.ev.canonical_account == base_account) & (self.ev.bsns_year == str(year))
                                                    & (self.ev.kind == "match")])
            return {**out, "where": f"보고 소계 + 매각예정 조정: {c.held_for_sale_included or ''} — {c.note}",
                    "verify": (f"매각예정 0은 직접 대조({c.note.split(':')[0].replace('원문 확인 ', '')}), 소계는 " if hfs_direct else "소계: ") + base_verify,
                    "where_basis": "매각예정은 원문에서 확인, 소계는 API 정보로 구성" if hfs_direct else "API 정보로 구성"}
        ev = self.ev[(self.ev.canonical_account == account) & (self.ev.bsns_year == str(year)) & (self.ev.kind == "match")]
        if ev.empty:
            return {**out, "where": c.note}
        parts = []
        for r in ev.itertuples():
            _, line, col = r.row_id.rsplit("#", 2)
            parts.append(f"'{r.account_nm}' {COLUMN.get(col, col)} 열 (금액 '{r.raw_amount}', API 응답 행 번호 {line})")
        where = f"{self.section(ev.iloc[0].sj_div)} > " + " + ".join(parts)
        if c.status != "mapped":
            where += f" — {c.note}"
        return {**out, "where": where, "verify": self._cross_check(ev), "where_basis": "API 정보로 구성"}

    def _cross_check(self, ev: pd.DataFrame) -> str:
        """사용한 API 행을 기준일 이전에 공개된 다른 보고서의 같은 기간 대응 행(다음 해 보고서의 전기 열 등)과 대조한다.

        원문을 직접 본 것이 아니라 API 응답끼리의 대조다. 대조할 수 없을 때는 사유를 둘로 나눈다 (D17):
        API 간 대조 불가 사유(동일 기간의 다른 보고서 없음 / 대응 행 없음)와, 현재 보고서 원문 대조 미실시.
        """
        if ev.empty:
            return f"미대조 — 사용 행 없음 · {NO_DIRECT}"
        results, bases, missing = [], set(), []
        for r in ev.itertuples():
            if r.row_id not in self.fact_by_id.index:
                return f"미대조 — 사용 행 중 수치로 읽힌 행이 아닌 것이 있음 · {NO_DIRECT}"
            f = self.fact_by_id.loc[r.row_id]
            same = same_period_rows({**f.to_dict(), "row_id": r.row_id}, self.fact_records, self.published, self.as_of)
            if not same:
                other = any(x["rcept_no"] != f.rcept_no and x["sj_div"] == f.sj_div and x["period_end"] == f.period_end
                            and self.published.get(x["rcept_no"], {}).get("rcept_dt", "99999999") <= self.as_of for x in self.fact_records)
                missing.append("다른 보고서에 대응 행 없음 (계정 코드·이름 모두 다름)" if other else "동일 기간의 다른 보고서 없음")
                results.append(None)
            else:
                results.append(all(Decimal(x["value"]) == Decimal(f.value) for x, _ in same))
                bases.update(b for _, b in same if b != "코드·계정명 같음")
        note = f" (표기 차이: {'; '.join(sorted(bases))})" if bases else ""
        if any(x is False for x in results):
            return "API 간 대조 — 불일치 (다른 보고서 값과 다름)" + note
        if results and all(x is True for x in results):
            return "API 간 대조 — 다음 보고서 비교 열과 일치" + note
        if all(x is None for x in results):
            return f"미대조 — API 간 대조 불가: {'; '.join(dict.fromkeys(missing))} · {NO_DIRECT}"
        return f"API 간 대조 — 일부 행만 대조 (나머지: {'; '.join(dict.fromkeys(missing))})" + note


def market_answer(corp: str, as_of: str) -> str:
    """질문 ①: 시장 통계 연결 결과(D18)를 그대로 옮긴다. 결과 파일이 없으면 보류."""
    if not (INTERIM / "market_links.csv").exists():
        return "| ① 시장이 숫자로 성장하는가 | **보류** | 시장 통계 연결 결과 없음 (src.market.run_market) |"
    links = read("market_links.csv")
    links = links[(links.corp_code == corp) & (links.candidate_id == "M01")]
    summary = read("market_summary_2021_2025.csv")
    summary = summary[summary.series == summary.series.iloc[0]] if "series" in summary else summary  # M01 (합계) 행만
    cagr = "; ".join(f"{r.span} 연평균 {float(r.cagr) * 100:.1f}%"
                     + (f" ({r.role.split(' — ')[0]}, 정의 연속성 {r.definition_continuity})" if getattr(r, "role", "") else "")
                     for r in summary.itertuples() if r.cagr)
    judge = f"{links.judgement.iloc[0]} — {links.differences.iloc[0]}" if not links.empty else "연결 판정 없음"
    return (f"| ① 시장이 숫자로 성장하는가 | **부분 답변** | 국내 화장품 생산금액(책임판매업체 보고, 명목)의 회고 추세만 제시: {cagr} "
            f"(시작 연도에 민감, 2021→2022 -18.4%는 작성 기준 변경 여부 미확인 — 비교 주의). 이 회사와의 연결: **{esc(judge)}**. "
            f"글로벌 수요·ODM 시장 성장률이 아니며 점수 미반영 — `market_stats_{as_of}.md` |")


def cash_note(raw: dict, wc: pd.DataFrame, years: list[int]) -> str:
    """최근 두 해의 영업이익·영업현금흐름 방향과 운전자본 소계를 기존 결과로만 서술한다 (원인을 확정하지 않음)."""
    a, b = years[-2], years[-1]
    oi = [raw.get(("operating_income", y)) for y in (a, b)]
    ocf = [raw.get(("operating_cash_flow", y)) for y in (a, b)]
    if None in oi or None in ocf:
        return ""
    word = lambda x0, x1: "증가" if Decimal(x1) > Decimal(x0) else "감소" if Decimal(x1) < Decimal(x0) else "변화 없음"
    w_rows = wc.set_index("year")
    wc_text = "; ".join(f"{y} {eok(w_rows.loc[str(y)].wc_amount)} (검산 {w_rows.loc[str(y)].sign_status})"
                        for y in (a, b) if str(y) in w_rows.index and w_rows.loc[str(y)].wc_amount)
    unchecked = [str(y) for y in (a, b) if str(y) not in w_rows.index or w_rows.loc[str(y)].sign_status != "산술 일치"]
    text = (f"{a}→{b}년 영업이익은 {eok(oi[0])}에서 {eok(oi[1])}으로 {word(*oi)}했지만, 영업현금흐름은 {eok(ocf[0])}에서 {eok(ocf[1])}으로 {word(*ocf)}했다"
            if word(*oi) != word(*ocf) else
            f"{a}→{b}년 영업이익은 {eok(oi[0])}에서 {eok(oi[1])}으로, 영업현금흐름은 {eok(ocf[0])}에서 {eok(ocf[1])}으로 함께 {word(*oi)}했다")
    text += f". 운전자본 변동 소계: {wc_text}"
    if unchecked:
        text += f". {'·'.join(unchecked)}년 검산이 미완료라 차이의 원인을 운전자본으로 확정하지 않는다"
    else:
        text += (". 두 해 모두 소계 부호의 산술적 일관성은 확인됐지만, 구성·일회성 여부는 확인하지 않았으므로 "
                 "영업현금흐름 변화의 원인을 운전자본 하나로 확정하지 않는다")
    return esc(text + ".")


def span_note(pairs: pd.DataFrame, metrics: list[str]) -> str:
    """지표별 2022~2025 전체 비교 가능성 요약 (보고서 5절)."""
    p = pairs[pairs.metric.isin(metrics) & pairs.span.str.endswith("전체")]
    return " ".join(f"{r.metric_label}: {r.comparability}." for r in p.itertuples())


def status_note(trend: pd.DataFrame, metrics: list[str], years: list[int]) -> str:
    """과거 연도 중 정상 계산이 아닌 칸과 사유 (보고서 5절)."""
    t = trend[trend.metric.isin(metrics) & trend.year.isin([str(y) for y in years]) & (trend.calc_status != "정상")]
    if t.empty:
        return f"{years[0]}~{years[-1]} 모두 정상 계산."
    by = {}
    for r in t.itertuples():
        by.setdefault((r.metric_label, r.calc_status, r.hold_reasons.split(" — ")[0][:60]), []).append(r.year)
    return esc(" ".join(f"{label} {'·'.join(ys)}: {status} ({reason})." for (label, status, reason), ys in by.items())
               .replace("review_needed", "확인 필요"))


def main(corp: str = "00763473") -> str:
    basis = yaml.safe_load((ROOT / "configs" / "basis_v1.yaml").read_text(encoding="utf-8"))
    trend_cfg = yaml.safe_load((ROOT / "configs" / "trend_v1.yaml").read_text(encoding="utf-8"))
    scoring = yaml.safe_load((ROOT / "configs" / "scoring_v2.yaml").read_text(encoding="utf-8"))
    spec = basis["companies"][corp]
    name, fs = spec["name"], spec["basis"]
    trend = read("trend_2022_2025.csv")
    trend = trend[trend.corp_code == corp]
    pairs = read("trend_pairs_2022_2025.csv")
    pairs = pairs[pairs.corp_code == corp]
    wc = read("working_capital_2022_2025.csv")
    wc = wc[wc.corp_code == corp]
    scores = read("scores_2025.csv")
    scores = scores[scores.corp_code == corp]
    cover = read("coverage_2025.csv")
    cover = cover[cover.corp_code == corp].iloc[0]
    scope = read("scope_changes.csv", MANUAL)
    scope = scope[(scope.corp_code == corp) & (scope.fs_div == fs)]
    check = read("account_check.csv")
    check = check[(check.corp_code == corp) & (check.fs_div == fs)]
    as_of = trend.as_of.iloc[0]
    run_id, manifest, run_problems = latest_run()
    ev = Evidence(corp, fs, load_published_filings(RAW), as_of)
    years = [int(y) for y in trend_cfg["years"]]

    L = []
    w = L.append
    w(f"# {name} 증거 연결 보고서 — {trend_cfg['title']}")
    w("")
    w("> 이 보고서는 기존 파이프라인 결과를 모은 것이다. 지표별 점수와 기여도는 채택된 미검증 규칙에 따른 계산 결과이며, "
      "**종합점수와 성공확률은 제공하지 않는다** (5절).")
    w("")
    # ------------------------------------------------------------ 1
    w("## 1. 기준과 범위")
    w("")
    w("| 항목 | 내용 |")
    w("|---|---|")
    w(f"| 분석 기준일 | {as_of[:4]}-{as_of[4:6]}-{as_of[6:]} — 이 날짜까지 공개된 사업보고서 중 연도별 최신 유효 정정본의 당기 값 (D5) |")
    w(f"| 재무 기준 | {esc(spec['reason'])} (`basis_v1`) |")
    w(f"| 회고 추세 | {trend_cfg['title']}. 모든 연도에 같은 기준일·같은 선택 규칙을 썼다 — **각 연도 당시 알 수 있었던 정보만으로 평가한 결과가 아니다** |")
    w(f"| 추세의 쓰임 | {trend_cfg['score_use']} |")
    rules = manifest.get("rules", {})
    w("| 규칙 버전 | " + ", ".join(f"`{k}` {v.get('version')}" for k, v in rules.items())
      + f" — 점수 규칙 {scoring['version']}는 채택({scoring['status']}), 검증 {scoring.get('validation_status')} |")
    w(f"| 실행 기록 | `{run_id}` (코드 {manifest.get('code', {}).get('commit', '?')[:7]}"
      + (", 미커밋 변경 있음" if manifest.get("code", {}).get("dirty") else "") + ")"
      + (" — **주의: " + "; ".join(run_problems) + "**" if run_problems else " — 결과 파일 해시 일치") + " |")
    w("")
    # ------------------------------------------------------------ 2
    w("## 2. 2022~2025 지표")
    w("")
    w("표시: △ 비교 주의 (비교 대상 기간은 아래 표), (참고) 참고 계산 — 비교 보류, — 계산 안 함. 각 칸의 `E..`는 4절 근거 ID.")
    w("")
    w("| 지표 | " + " | ".join(str(y) for y in years) + " |")
    w("|---|" + "---|" * len(years))
    hold_lines = []
    for metric in trend_cfg["metrics"]:
        g = trend[trend.metric == metric].set_index("year")
        cells = []
        for y in years:
            r = g.loc[str(y)]
            refs = ",".join(ev.ref(k.split(":")[0], int(k.split(":")[1])) for k in r.input_keys.split(";") if k)
            cells.append(f"{fmt_value(metric, r.value)}{MARK[r.comparability]} <sub>{refs}</sub>")
            if r.calc_status != "정상":
                hold_lines.append(f"- {r.metric_label} {y}: **{r.calc_status}** — {r.hold_reasons or '—'}")
        w(f"| {g.iloc[0].metric_label} | " + " | ".join(cells) + " |")
    w("")
    w("**비교 대상 기간별 비교 가능성** (연도 칸의 계산 상태와 따로 본다)")
    w("")
    spans = list(dict.fromkeys(pairs.span))
    w("| 지표 | " + " | ".join(spans) + " |")
    w("|---|" + "---|" * len(spans))
    for metric in trend_cfg["metrics"]:
        p = pairs[pairs.metric == metric].set_index("span")
        w(f"| {p.iloc[0].metric_label} | " + " | ".join(p.loc[s].comparability for s in spans) + " |")
    w("")
    w("**비교 주의 사유** — 연결 범위 변화는 보수적 표시다. 영향이 실제로 큰지 확인했다는 뜻이 아니다 (영향 규모 미확인).")
    w("")
    reasons = {}
    for r in trend.itertuples():
        for part in filter(None, r.comparability_reasons.split(" | ")):
            reasons.setdefault(part, set()).add(r.metric_label)
    for text, metrics in reasons.items():
        w(f"- {text} → {len(metrics)}개 지표")
    w("")
    w("**연결 범위 변화 원문 기록** (`data/manual/scope_changes.csv`, 사업보고서 '연결대상회사의 변동내용' 그대로)")
    w("")
    w("| ID | 연도 | 구분 | 대상 | 사유 | 근거 상태 | 접수번호 |")
    w("|---|---|---|---|---|---|---|")
    for r in scope.sort_values("bsns_year").itertuples():
        w(f"| {r.scope_change_id} | {r.bsns_year} | {r.change_type} | {esc(r.entity)} | {esc(r.reason)} | {r.evidence_status} · {r.impact} | {r.rcept_no} |")
    flagged = scope[scope.change_type == "확인 필요"]
    for r in flagged.itertuples():
        w("")
        w(f"> **{r.scope_change_id} ({r.bsns_year}) — 숫자는 계산됐지만 비교 해석에는 확인이 필요한 사례.** {r.reason}. "
          f"그래서 {r.bsns_year}년 지표는 정상 계산이어도 {int(r.bsns_year) - 1}↔{r.bsns_year}·{r.bsns_year}↔{int(r.bsns_year) + 1} 비교에 주의가 붙는다. "
          "확인 방법: 같은 보고서의 연결 주석(종속기업 현황)에서 세 회사의 연결 포함 여부와 설립 시점을 대조.")
    if hold_lines:
        w("")
        w("**계산 안 함·참고 계산 사유**")
        w("")
        L.extend(hold_lines)
    w("")
    # ------------------------------------------------------------ 3
    w("## 3. 운전자본 변동")
    w("")
    w(f"출처: {trend_cfg['working_capital']['source']}. 규모 경고 기준 |변동| / 평균 총자산 ≥ "
      f"{trend_cfg['working_capital']['caution_threshold']:.0%} — **탐색용 초기 기준**(회계기준 값 아님), 점수·일회성 판정에 쓰지 않는다.")
    w("")
    w("| 연도 | 금액 | 소계 존재 | 항등식 검산 | 활동 분류 확인 | 현금흐름 영향 방향 | 구성·완전성 | 규모 경고 | 일회성 여부 |")
    w("|---|---:|---|---|---|---|---|---|---|")
    for r in wc.itertuples():
        share = f"{float(r.wc_to_avg_assets):.1%}" if r.wc_to_avg_assets else "—"
        caution = r.caution if r.caution_flag == "True" else f"기준 미만 ({share})" if r.wc_amount else r.caution
        w(f"| {r.year} | {eok(r.wc_amount)} | {esc(r.wc_status)} | {esc(r.sign_status)} | {esc(r.activity_status or '—')} | "
          f"{esc(r.ocf_direction or '—')} | {'소계 1행만 사용 — 범위·완전성 미확인' if r.wc_amount else esc(r.completeness)} | {esc(caution)} | {r.one_off} |")
    w("")
    w("'산술 일치'와 '활동 분류 확인'은 별도 상태다. 산술 일치는 아래 검산식이 원 단위로 맞는다는 뜻이고, 활동 분류 확인은 검산에 넣은 "
      "이자·배당·법인세 행이 원문에서 실제로 영업활동 구간에 있는지다. 코드에 '영업활동' 구분이 없는 행은 원문 확인표"
      "(`data/manual/cf_activity_reviews.csv`)에서 영업활동으로 확인된 경우에만 넣는다.")
    conflicts = [c for c in wc.activity_conflicts if c]
    if conflicts:
        w("")
        w("**API 코드와 원문 표시가 다른 행** — 원문 표시를 따르고 충돌을 기록한다 (배당·이자의 활동 분류는 회사의 회계정책에 따라 다를 수 있다):")
        w("")
        for c in conflicts:
            for part in c.split("; "):
                w(f"- {part}")
    w("")
    w("**검산식 전개** (같은 보고서 현금흐름표, API 응답 순서는 원문 표의 줄 번호가 아님)")
    for r in wc.itertuples():
        terms = json.loads(r.identity_terms) if r.identity_terms else []
        if not terms:
            if r.wc_amount:
                w("")
                w(f"*{r.year}* — {r.sign_status}: {r.sign_evidence}")
            continue
        w("")
        w(f"*{r.year}* — 대상: {r.identity_target}")
        w("")
        w("| 항목 | 코드 (API 응답 순서) | API 값 | 적용 부호 | 부호 근거 | 적용 값 |")
        w("|---|---|---:|:---:|---|---:|")
        for t in terms:
            w(f"| {esc(t['item'])} | {t['code']} ({t['api_order']}) | {Decimal(t['api_value']):,} | {t['sign']} | {esc(t['basis'])} | {Decimal(t['applied']):,} |")
        w(f"| **합계** | | | | | **{Decimal(r.identity_total):,}** |")
        w(f"| **대상: {esc(r.identity_target.split(' (')[0])}** | | | | | **{Decimal(r.identity_target_value):,}** |")
        w(f"| **잔차 (대상 − 합계)** | | | | {r.sign_status} | **{Decimal(r.identity_residual):,}** |")
    w("")
    w("항등식이 맞으면 **운전자본 소계 부호의 산술적 일관성**이 확인된 것이다. '영업에서 창출된 현금'과 최종 영업현금흐름(OCF)은 다르며, "
      "그 사이에 이자·법인세 지급 등이 있다 — 그래서 검산 대상을 함께 적었다. 계정 범위·구성의 완전성·일회성 여부는 항등식으로 확인되지 않는다.")
    w("")
    # ------------------------------------------------------------ 4
    w("## 4. 근거 (접수번호 → 원문 위치)")
    w("")
    w("세 상태를 구분한다. **계산 입력 상태**는 매핑·계산에 쓸 수 있는지(정상·참고·계산 안 함)이고, 원문 검증이 끝났다는 뜻이 아니다. "
      "**원문 대조**는 직접 대조(수기 확인표로 원문을 읽음) / API 간 대조(다른 보고서의 같은 기간 대응 행 API 값과 비교 — 원문을 직접 본 것은 아님. "
      "계정명·코드 표기가 보고서마다 바뀐 경우 코드나 계정명 중 하나가 같으면 대응 행으로 보고 표기 차이를 적음) / 미대조. "
      "미대조 사유는 둘로 나눈다: API 간 대조 불가 사유(동일 기간의 다른 보고서 없음 — 2025 값은 다음 해 보고서가 아직 없음)와 "
      "현재 보고서 원문 대조 미실시(같은 해 보고서 원문과 직접 대조하는 것은 가능하지만 아직 하지 않음). "
      "**위치 근거**는 원문에서 확인 / API 정보로 구성(재무제표 구분·계정명·열로 만든 경로).")
    w("")
    w("원문 위치는 API 행(재무제표 구분·계정명·열)에 대응하는 사업보고서의 표다. API 응답 행 번호·순서는 수집한 응답 안의 위치이며 원문 표의 줄 번호가 아니다. 수기 확인값은 확인표의 원문 위치를 그대로 옮겼다. "
      "링크는 DART 공시 뷰어.")
    w("")
    w("| ID | 계정 | 연도 | 값 | 계산 입력 상태 | 원문 대조 | 위치 근거 | 보고서 | 원문 위치 | 추출 방법 |")
    w("|---|---|---|---:|---|---|---|---|---|---|")
    for e in ev.rows:
        w(f"| {e['id']} | {e['account']} | {e['year']} | {eok(e['value'])} | {STATUS_KO.get(e['status'], e['status'])} | "
          f"{esc(e['verify'])} | {e['where_basis']} | "
          f"[{esc(e['report'])}]({e['link']}) | {esc(e['where'])} | {esc(e['method'])} |")
    w("")
    # ------------------------------------------------------------ 5
    w("## 5. 지금 답할 수 있는 질문과 보류된 질문 (docs/problem.md)")
    w("")
    raw = {(a, int(y)): v for a, y, v, s in zip(check.canonical_account, check.bsns_year, check.value, check.status) if s == "mapped"}
    w("| 질문 | 상태 | 지금의 답 또는 보류 사유 |")
    w("|---|---|---|")
    w(market_answer(corp, as_of))
    gap = scores[scores.metric.isin(["growth_gap", "operating_margin_gap", "roa_gap", "inventory_turnover_rel"])]
    w("| ② 비슷한 경쟁사보다 성장성·운영 효율이 좋은가 | **부분 — 검증용** | 원시 지표(성장률·영업이익률·ROA·재고회전율)는 위 표에 있다. "
      f"비교군 기준 지표 {len(gap)}개는 파일럿 비교군(평가기업 제외 3개사, n < 20)이라 계산 검증용으로만 표시하고 점수에 넣지 않는다 |")
    flows = " / ".join(f"{y}: 매출 {eok(raw.get(('revenue', y)))}, 영업이익 {eok(raw.get(('operating_income', y)))}, "
                       f"영업현금흐름 {eok(raw.get(('operating_cash_flow', y)))}" for y in years)
    w(f"| ③ 4년간 매출·이익 변화와 이익의 현금 전환 | **변화는 제시 가능 — 원인 설명과 일부 기간 비교는 부분 답변** | {flows}. "
      + cash_note(raw, wc, years) + " "
      + span_note(pairs, ["revenue_yoy_growth", "operating_margin", "ocf_to_avg_assets"]) + " |")
    ic = trend[trend.metric == "interest_coverage"].set_index("year")
    w(f"| ④ 단기 지급의무·이자 부담 여력 | **2025 재무비율 설명 가능 — 실제 지급 여력 판단은 부분 답변** | 2025: 이자보상배율 {fmt_value('interest_coverage', ic.loc['2025'].value)}, "
      f"유동비율 {fmt_value('current_ratio_hfs', trend[(trend.metric == 'current_ratio_hfs') & (trend.year == '2025')].value.iloc[0])}, "
      f"현금/단기 이자부채 {fmt_value('cash_to_short_term_debt', trend[(trend.metric == 'cash_to_short_term_debt') & (trend.year == '2025')].value.iloc[0])}. "
      + "만기 일정·현금 사용 제한·약정(커버넌트)·미사용 한도는 확인하지 않아, 이 비율만으로 실제 지급 가능 여부를 판단하지 않는다. "
      + status_note(trend, ["interest_coverage", "current_ratio_hfs", "cash_to_short_term_debt"], years[:-1]) + " |")
    w("| ⑤ 정책·규제·기술 변화와 사업 위험 | **보류** | PESTEL·위험 요인 근거 카드(3주차) 미구현 |")
    w("")
    w("**공통 신뢰 질문**")
    w("")
    w(f"1. 언제 공개된 어떤 자료인가 — 1절(기준일·규칙)과 4절(근거 {len(ev.rows)}개, 접수번호·공시일·원문 위치).")
    w("2. 자료가 충분한가 — 2절의 계산 상태와 비교 대상 기간별 비교 가능성, 3절의 검산 상태. 계산 상태와 비교 가능성은 따로 표시했다.")
    w(f"3. 무엇이 결과를 움직였나 — 2025 {cover.K_label} **{float(cover.K_contribution):.2f}** (반영 배점 {cover.scored_weight}/90):")
    w("")
    w("| 지표 (2025) | 배점 | 점수 | 기여 | 상태 |")
    w("|---|---:|---:|---:|---|")
    status_ko = {"scored": "반영", "validation_only": "검증용", "view_only": "조회용", "not_implemented": "미구현", "held": "보류",
                 "not_applicable": "해당 없음"}
    for r in scores.itertuples():
        score = f"{float(r.score):.1f}" if r.score else "—"
        contrib = f"{float(r.contribution):.2f}" if r.contribution else "—"
        w(f"| {r.metric_label} | {r.weight or 0} | {score} | {contrib} | {status_ko.get(r.score_status, r.score_status)} |")
    w("")
    w(f"**종합점수: 보류** — {cover.composite_hold_reasons}. "
      f"K는 반영된 배점({cover.scored_weight})만의 기여도라 다른 기업의 K나 100점 만점 점수와 비교하지 않는다. "
      f"구조화 기여도 범위 — 배점 90 기준, AI 10 제외: {float(cover.range_low):.1f} ~ {float(cover.range_high_loose):.1f} "
      "(확인된 기여도 ~ 미확인 배점을 모두 만점으로 둔 느슨한 상한. 신뢰구간이나 성공확률 범위가 아니다).")
    w("")
    text = "\n".join(L) + "\n"
    out_dir = INTERIM / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{corp}_evidence_{as_of}.md"
    path.write_text(text, encoding="utf-8")
    print(f"[증거 연결 보고서] {path} — 근거 {len(ev.rows)}개, 지표 {len(trend)}칸" + (f", 주의: {run_problems}" if run_problems else ""))
    return str(path)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "00763473")
