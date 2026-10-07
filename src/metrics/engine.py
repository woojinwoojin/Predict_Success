"""2025 지표 계산 (README 6.1~6.3, docs/decisions.md D9). 점수(0~100 변환)는 아직 하지 않는다.

입력 상태 → 계산 상태:
  mapped                → normal     (정상 계산)
  candidate             → reference  (참고 계산 — 후보 이자비용, 산술로만 판정된 매각예정 조정 등. 점수 반영 보류)
  그 밖(확인 필요 등)    → not_computed (계산하지 않음, 보류 사유)
비교군 기준 지표는 파일럿 비교군(평가기업 제외 3개사)이라 '검증용'으로만 표시한다.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from statistics import median, pstdev

STATUS_RANK = {"normal": 0, "reference": 1, "not_computed": 2}
INPUT_TO_CALC = {"mapped": "normal", "candidate": "reference"}


@dataclass
class Input:
    label: str
    year: int
    status: str
    value: Decimal | None
    source: str  # 접수번호·추출 방법
    note: str = ""

    @property
    def calc_status(self) -> str:
        return INPUT_TO_CALC.get(self.status, "not_computed")

    def describe(self) -> str:
        v = "—" if self.value is None else f"{self.value:,.0f}"
        return f"{self.label} {self.year} = {v} [{self.status}; {self.source}]"


@dataclass
class Result:
    metric: str
    label: str
    period: str
    value: float | None = None
    calc_status: str = "normal"
    score_eligible: str = "가능"
    hold_reasons: list[str] = field(default_factory=list)
    inputs: list[Input] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    peer_percentile_validation: float | None = None


def combine(result: Result, inputs: list[Input]) -> Result:
    """입력 중 가장 나쁜 상태를 따른다. 보류 사유를 모은다."""
    result.inputs = inputs
    worst = max((i.calc_status for i in inputs), key=STATUS_RANK.get, default="normal")
    result.calc_status = worst
    for i in inputs:
        if i.calc_status == "reference":
            result.hold_reasons.append(f"{i.label} {i.year}: 후보값 — {i.note[:60]}")
        elif i.calc_status == "not_computed":
            result.hold_reasons.append(f"{i.label} {i.year}: {i.status} — {i.note[:60]}")
    if worst != "normal":
        result.score_eligible = "보류"
    return result


def ratio(n: Decimal, d: Decimal) -> float:
    return float(n / d)


def avg(a: Decimal, b: Decimal) -> Decimal:
    return (a + b) / 2


# ---------------------------------------------------------------- 6.1 재무 안정성·현금창출


def ocf_to_avg_assets(get) -> Result:
    ocf, ta0, ta1 = get("operating_cash_flow", 2025), get("total_assets", 2024), get("total_assets", 2025)
    r = combine(Result("ocf_to_avg_assets", "OCF / 평균 총자산", "2025 (평균: 2024말·2025말)"), [ocf, ta0, ta1])
    if r.calc_status != "not_computed":
        a = avg(ta0.value, ta1.value)
        if a <= 0:
            r.calc_status, r.score_eligible = "not_computed", "보류"
            r.hold_reasons.append("평균자산 ≤ 0")
        else:
            r.value = ratio(ocf.value, a)
    return r


def debt_ratio(get) -> Result:
    tl, ta = get("total_liabilities", 2025), get("total_assets", 2025)
    r = combine(Result("debt_ratio", "부채/자산 비율 (총부채 ÷ 총자산)", "2025말"), [tl, ta])
    if r.calc_status != "not_computed":
        if ta.value <= 0:
            r.calc_status, r.score_eligible = "not_computed", "보류"
            r.hold_reasons.append("자산 ≤ 0")
        else:
            r.value = ratio(tl.value, ta.value)
    return r


def interest_coverage(get) -> Result:
    oi, ie = get("operating_income", 2025), get("interest_expense", 2025)
    r = combine(Result("interest_coverage", "이자보상배율 (리스 이자 포함 기준)", "2025"), [oi, ie])
    if r.calc_status != "not_computed":
        if ie.value == 0:
            # 확인된 이자비용 0은 별도 상태 (무한대로 처리하지 않는다)
            r.calc_status, r.score_eligible = "not_computed", "보류"
            r.hold_reasons.append("이자비용 0 — 별도 상태 (점수 규칙 C에서 처리)")
        else:
            r.value = ratio(oi.value, ie.value)
    return r


def current_ratios(get) -> list[Result]:
    """점수 정의는 매각예정항목 포함 유동비율, 보고값 기준 유동비율은 조회용."""
    out = []
    for metric, label, ca_key, cl_key, eligible_if_normal in (
        ("current_ratio_hfs", "유동비율 (매각예정항목 포함)", "adjusted_current_assets", "adjusted_current_liabilities", "가능"),
        ("current_ratio_reported", "유동비율 (보고값 기준, 조회용)", "current_assets", "current_liabilities", "아니오 — 조회용"),
    ):
        ca, cl = get(ca_key, 2025), get(cl_key, 2025)
        r = combine(Result(metric, label, "2025말"), [ca, cl])
        if r.calc_status != "not_computed":
            if cl.value == 0:
                r.calc_status = "not_computed"
                r.hold_reasons.append("유동부채 0 — 무한대 처리 금지, 점수 단계에서 상한 적용")
            else:
                r.value = ratio(ca.value, cl.value)
        if r.calc_status == "normal":
            r.score_eligible = eligible_if_normal
        out.append(r)
    return out


def cash_to_short_term_debt(get) -> Result:
    cash, debt = get("cash", 2025), get("short_term_interest_bearing_debt", 2025)
    r = combine(Result("cash_to_short_term_debt", "현금 / 단기 이자부채 (리스 포함)", "2025말"), [cash, debt])
    if r.calc_status != "not_computed":
        if debt.value == 0:
            r.calc_status, r.score_eligible = "not_computed", "보류"
            r.hold_reasons.append("단기 이자부채 0(확인됨) — 누락과 구분해 별도 상태로 처리")
        else:
            r.value = ratio(cash.value, debt.value)
    return r


# ---------------------------------------------------------------- 6.2 성장성과 지속성


def revenue_cagr_3y(get) -> Result:
    r0, r3 = get("revenue", 2022), get("revenue", 2025)
    r = combine(Result("revenue_cagr_3y", "3년 매출 CAGR", "2022→2025"), [r0, r3])
    if r.calc_status != "not_computed":
        if r0.value <= 0:
            r.calc_status, r.score_eligible = "not_computed", "보류"
            r.hold_reasons.append("시작 매출 ≤ 0 — 미사용")
        else:
            r.value = float(r3.value / r0.value) ** (1 / 3) - 1
    return r


def growth_volatility(get) -> Result:
    revs = [get("revenue", y) for y in (2022, 2023, 2024, 2025)]
    r = combine(Result("growth_volatility", "매출 성장률 변동성 (3개 연간 성장률의 모표준편차)", "2023~2025"), revs)
    if r.calc_status != "not_computed":
        if any(x.value <= 0 for x in revs[:-1]):
            r.calc_status, r.score_eligible = "not_computed", "보류"
            r.hold_reasons.append("기준 연도 매출 ≤ 0")
        else:
            growth = [float(revs[i + 1].value / revs[i].value) - 1 for i in range(3)]
            r.value = pstdev(growth)
            r.notes.append("연간 성장률: " + ", ".join(f"{y} {g:+.1%}" for y, g in zip((2023, 2024, 2025), growth)))
            if all(g < 0 for g in growth):
                r.notes.append("3년 연속 감소 — 변동성이 낮아도 '안정'으로 표현하지 않음")
    return r


# ---------------------------------------------------------------- 6.3 운영 경쟁력 (자체 원시값)


def operating_margin(get) -> Result:
    oi, rev = get("operating_income", 2025), get("revenue", 2025)
    r = combine(Result("operating_margin", "영업이익률", "2025"), [oi, rev])
    if r.calc_status != "not_computed":
        if rev.value <= 0:
            r.calc_status, r.score_eligible = "not_computed", "보류"
            r.hold_reasons.append("매출 ≤ 0")
        else:
            r.value = ratio(oi.value, rev.value)
    return r


def roa(get) -> Result:
    ni, ta0, ta1 = get("net_income", 2025), get("total_assets", 2024), get("total_assets", 2025)
    r = combine(Result("roa", "ROA (순이익/평균 총자산)", "2025 (평균: 2024말·2025말)"), [ni, ta0, ta1])
    if r.calc_status != "not_computed":
        a = avg(ta0.value, ta1.value)
        if a <= 0:
            r.calc_status, r.score_eligible = "not_computed", "보류"
            r.hold_reasons.append("평균자산 ≤ 0")
        else:
            r.value = ratio(ni.value, a)
    return r


def inventory_turnover(get) -> Result:
    cogs, inv0, inv1 = get("cost_of_sales", 2025), get("inventory", 2024), get("inventory", 2025)
    r = combine(Result("inventory_turnover", "재고회전율 (매출원가/평균 재고)", "2025 (평균: 2024말·2025말)"), [cogs, inv0, inv1])
    if r.calc_status != "not_computed":
        a = avg(inv0.value, inv1.value)
        if a <= 0:
            r.calc_status, r.score_eligible = "not_computed", "보류"
            r.hold_reasons.append("평균 재고 ≤ 0")
        else:
            r.value = ratio(cogs.value, a)
    return r


OWN_METRICS = [ocf_to_avg_assets, debt_ratio, interest_coverage, current_ratios, cash_to_short_term_debt,
               revenue_cagr_3y, growth_volatility, operating_margin, roa, inventory_turnover]

# 비교군 기준 지표: (이름, 표시, 바탕 지표, 방식) — 방식 diff = 값 − 중앙값, ratio = 값 ÷ 중앙값
PEER_METRICS = [
    ("growth_gap", "비교군 대비 성장 격차 (CAGR − 중앙값)", "revenue_cagr_3y", "diff", True),
    ("operating_margin_gap", "영업이익률 우위 (− 중앙값)", "operating_margin", "diff", True),
    ("roa_gap", "ROA 우위 (− 중앙값)", "roa", "diff", True),
    ("inventory_turnover_rel", "재고회전율 우위 (÷ 중앙값)", "inventory_turnover", "ratio", True),
]


def midrank_percentile(x: float, others: list[float], higher_is_better: bool = True) -> float:
    """README 7.1 A. 중간순위 백분위. 평가기업은 others에 없다."""
    below = sum(1 for o in others if o < x)
    same = sum(1 for o in others if o == x)
    p = 100 * (below + 0.5 * same) / len(others)
    return p if higher_is_better else 100 - p


def peer_relative(own: dict[str, dict[str, Result]], members: list[str], peer_label: str) -> dict[str, list[Result]]:
    """비교군 기준 지표. 평가기업은 중앙값에서 제외. 정상 계산된 값끼리만 비교한다."""
    out: dict[str, list[Result]] = {c: [] for c in own}
    for corp in own:
        for metric, label, base, how, higher in PEER_METRICS:
            r = Result(metric, label, own[corp][base].period)
            if corp not in members:
                r.calc_status, r.score_eligible = "not_computed", "해당 없음 — 비교군 제외"
                r.hold_reasons.append("화장품 ODM 비교군 제외 (0점 처리·배점 재분배 안 함)")
                out[corp].append(r)
                continue
            mine = own[corp][base]
            others = [own[o][base].value for o in members if o != corp and own[o][base].calc_status == "normal"]
            if mine.calc_status != "normal" or not others:
                r.calc_status, r.score_eligible = "not_computed", "보류"
                r.hold_reasons.append(f"바탕 지표 {mine.label} 상태 {mine.calc_status}" if mine.calc_status != "normal" else "비교 상대 없음")
                out[corp].append(r)
                continue
            m = median(others)
            if how == "ratio" and m <= 0:
                r.calc_status, r.score_eligible = "not_computed", "보류"
                r.hold_reasons.append("비교군 중앙값 ≤ 0")
            else:
                r.value = mine.value - m if how == "diff" else mine.value / m
                r.score_eligible = f"검증용 — {peer_label} (평가기업 제외 {len(others)}개사)"
                r.peer_percentile_validation = midrank_percentile(mine.value, others, higher)
                r.notes.append(f"비교군 중앙값 {m:.4f} (n={len(others)})")
            out[corp].append(r)
    return out
