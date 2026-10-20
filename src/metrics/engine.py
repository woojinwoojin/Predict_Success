"""연도별 지표 계산 (README 6.1~6.3, docs/decisions.md D9, D14). 기본 연도는 2025. 점수(0~100 변환)는 아직 하지 않는다.

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


PERIOD_OK = {"from_filing_text", "from_manual"}
# 적자·유출로 음수가 될 수 있는 흐름 계정. 그 밖의 입력(자산·부채·현금·매출·매출원가·이자비용 등)의 음수는
# 실적이 아니라 부호·계정 정의 문제일 수 있으므로 확인 전까지 계산·점수를 보류한다 (분모 0과 구분)
SIGNED_ACCOUNTS = {"operating_income", "net_income", "operating_cash_flow"}


@dataclass
class Input:
    label: str
    year: int
    status: str
    value: Decimal | None
    source: str  # 접수번호·추출 방법
    note: str = ""
    period_type: str | None = None
    period_start: str | None = None
    period_end: str | None = None
    period_status: str | None = None
    period_is_annual: bool | None = None
    account: str = ""  # 내부 계정 키 (근거 연결용)

    def period_problem(self) -> str | None:
        """값이 있는 입력의 기간 조건 (D11 ⑥): 기간 확인, 요청 연도와 일치, 흐름 값은 연간."""
        if self.status not in INPUT_TO_CALC:
            return None
        if self.period_status not in PERIOD_OK or not self.period_end:
            return f"기간 미확인 ({self.period_status})"
        if int(self.period_end[:4]) != self.year:
            return f"기간 종료일 {self.period_end}이 {self.year} 사업연도와 다름"
        if self.period_type == "duration" and self.period_is_annual is not True:
            return f"연간 기간 아님 ({self.period_start}~{self.period_end}) — 연간 비율·성장률에 쓰지 않음"
        return None

    @property
    def calc_status(self) -> str:
        if self.period_problem():
            return "not_computed"
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
        if i.period_problem():
            result.hold_reasons.append(f"{i.label} {i.year}: {i.period_problem()}")
        elif i.calc_status == "reference":
            result.hold_reasons.append(f"{i.label} {i.year}: 후보값 — {i.note[:60]}")
        elif i.calc_status == "not_computed":
            result.hold_reasons.append(f"{i.label} {i.year}: {i.status} — {i.note[:60]}")
    negative = [i for i in inputs if i.calc_status != "not_computed" and i.value is not None and i.value < 0
                and (i.account or i.label) not in SIGNED_ACCOUNTS]
    for i in negative:
        result.hold_reasons.append(f"{i.label} {i.year}: 음수 {i.value:,.0f} — 부호·계정 정의 확인 전 계산 보류")
    if negative:
        worst = result.calc_status = "not_computed"
    if worst != "normal":
        result.score_eligible = "보류"
    return result


def finalize(result: Result) -> Result:
    """상태 일관성 (D11 ⑦): 정상 계산이 아니면 점수 반영 가능으로 남지 않는다."""
    if result.calc_status != "normal" and (result.score_eligible == "가능" or result.score_eligible.startswith("검증용")):
        result.score_eligible = "보류"
    if result.value is None and result.calc_status == "normal":
        result.calc_status, result.score_eligible = "not_computed", "보류"
    return result


def ratio(n: Decimal, d: Decimal) -> float:
    return float(n / d)


def avg(a: Decimal, b: Decimal) -> Decimal:
    return (a + b) / 2


# ---------------------------------------------------------------- 6.1 재무 안정성·현금창출


def ocf_to_avg_assets(get, y: int = 2025) -> Result:
    ocf, ta0, ta1 = get("operating_cash_flow", y), get("total_assets", y - 1), get("total_assets", y)
    r = combine(Result("ocf_to_avg_assets", "OCF / 평균 총자산", f"{y} (평균: {y - 1}말·{y}말)"), [ocf, ta0, ta1])
    if r.calc_status != "not_computed":
        a = avg(ta0.value, ta1.value)
        if a <= 0:
            r.calc_status, r.score_eligible = "not_computed", "보류"
            r.hold_reasons.append("평균자산 ≤ 0")
        else:
            r.value = ratio(ocf.value, a)
    return r


def debt_ratio(get, y: int = 2025) -> Result:
    tl, ta = get("total_liabilities", y), get("total_assets", y)
    r = combine(Result("debt_ratio", "부채/자산 비율 (총부채 ÷ 총자산)", f"{y}말"), [tl, ta])
    if r.calc_status != "not_computed":
        if ta.value <= 0:
            r.calc_status, r.score_eligible = "not_computed", "보류"
            r.hold_reasons.append("자산 ≤ 0")
        else:
            r.value = ratio(tl.value, ta.value)
    return r


def interest_coverage(get, y: int = 2025) -> Result:
    oi, ie = get("operating_income", y), get("interest_expense", y)
    r = combine(Result("interest_coverage", "이자보상배율 (리스 이자 포함 기준)", str(y)), [oi, ie])
    if r.calc_status != "not_computed":
        if ie.value == 0:
            # 확인된 이자비용 0은 별도 상태 (무한대로 처리하지 않는다)
            r.calc_status, r.score_eligible = "not_computed", "보류"
            r.hold_reasons.append("이자비용 0 — 별도 상태 (점수 규칙 C에서 처리)")
        else:
            r.value = ratio(oi.value, ie.value)
    return r


def current_ratios(get, y: int = 2025) -> list[Result]:
    """점수 정의는 매각예정항목 포함 유동비율, 보고값 기준 유동비율은 조회용."""
    out = []
    for metric, label, ca_key, cl_key, eligible_if_normal in (
        ("current_ratio_hfs", "유동비율 (매각예정항목 포함)", "adjusted_current_assets", "adjusted_current_liabilities", "가능"),
        ("current_ratio_reported", "유동비율 (보고값 기준, 조회용)", "current_assets", "current_liabilities", "아니오 — 조회용"),
    ):
        ca, cl = get(ca_key, y), get(cl_key, y)
        r = combine(Result(metric, label, f"{y}말"), [ca, cl])
        if r.calc_status != "not_computed":
            if cl.value == 0:
                # 비율 계산 불가. 조건부 점수(README 7.1 C)는 아직 구현하지 않았으므로 점수도 보류
                r.calc_status, r.score_eligible = "not_computed", "보류 — 비율 계산 불가 (조건부 점수 미구현)"
                r.hold_reasons.append("유동부채 0 — 무한대 처리 금지")
            else:
                r.value = ratio(ca.value, cl.value)
        if r.calc_status == "normal":
            r.score_eligible = eligible_if_normal
        out.append(finalize(r))
    return out


def cash_to_short_term_debt(get, y: int = 2025) -> Result:
    cash, debt = get("cash", y), get("short_term_interest_bearing_debt", y)
    r = combine(Result("cash_to_short_term_debt", "현금 / 단기 이자부채 (리스 포함)", f"{y}말"), [cash, debt])
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


def revenue_yoy_growth(get, y: int = 2025) -> Result:
    """추세 비교용 (D14). 점수 정의에는 없다 — 성장 점수는 3년 CAGR·변동성이 맡는다."""
    r0, r1 = get("revenue", y - 1), get("revenue", y)
    r = combine(Result("revenue_yoy_growth", "전년 대비 매출 증가율", f"{y - 1}→{y}"), [r0, r1])
    if r.calc_status != "not_computed":
        if r0.value <= 0:
            r.calc_status, r.score_eligible = "not_computed", "보류"
            r.hold_reasons.append("전년 매출 ≤ 0 — 미사용")
        else:
            r.value = float(r1.value / r0.value) - 1
    return r


# ---------------------------------------------------------------- 6.3 운영 경쟁력 (자체 원시값)


def operating_margin(get, y: int = 2025) -> Result:
    oi, rev = get("operating_income", y), get("revenue", y)
    r = combine(Result("operating_margin", "영업이익률", str(y)), [oi, rev])
    if r.calc_status != "not_computed":
        if rev.value <= 0:
            r.calc_status, r.score_eligible = "not_computed", "보류"
            r.hold_reasons.append("매출 ≤ 0")
        else:
            r.value = ratio(oi.value, rev.value)
    return r


def roa(get, y: int = 2025) -> Result:
    ni, ta0, ta1 = get("net_income", y), get("total_assets", y - 1), get("total_assets", y)
    r = combine(Result("roa", "ROA (순이익/평균 총자산)", f"{y} (평균: {y - 1}말·{y}말)"), [ni, ta0, ta1])
    if r.calc_status != "not_computed":
        a = avg(ta0.value, ta1.value)
        if a <= 0:
            r.calc_status, r.score_eligible = "not_computed", "보류"
            r.hold_reasons.append("평균자산 ≤ 0")
        else:
            r.value = ratio(ni.value, a)
    return r


def inventory_turnover(get, y: int = 2025) -> Result:
    cogs, inv0, inv1 = get("cost_of_sales", y), get("inventory", y - 1), get("inventory", y)
    r = combine(Result("inventory_turnover", "재고회전율 (매출원가/평균 재고)", f"{y} (평균: {y - 1}말·{y}말)"), [cogs, inv0, inv1])
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

# 연도별 추세 비교에 쓰는 단년 지표 함수 (D14). current_ratios는 두 지표(매각예정 포함·보고값)를 낸다
TREND_METRICS = [ocf_to_avg_assets, debt_ratio, interest_coverage, current_ratios, cash_to_short_term_debt,
                 revenue_yoy_growth, operating_margin, roa, inventory_turnover]
# 지표별 입력 계정 — 재분류·표시 범위 변경 검토(mismatch_reviews)를 지표에 연결할 때 쓴다
METRIC_ACCOUNTS = {
    "ocf_to_avg_assets": {"operating_cash_flow", "total_assets"},
    "debt_ratio": {"total_liabilities", "total_assets"},
    "interest_coverage": {"operating_income", "interest_expense"},
    # 유동자산(보고)의 매각예정 포함 여부 차이(M03 등)는 조정값이 흡수하므로 current_assets는 넣지 않는다.
    # 조정 유동부채 = 유동부채(보고) + 매각예정부채라 유동부채 재분류(M01)는 그대로 영향을 준다
    "current_ratio_hfs": {"adjusted_current_assets", "adjusted_current_liabilities", "current_liabilities"},
    "current_ratio_reported": {"current_assets", "current_liabilities"},
    "cash_to_short_term_debt": {"cash", "short_term_interest_bearing_debt"},
    "revenue_yoy_growth": {"revenue"},
    "operating_margin": {"operating_income", "revenue"},
    "roa": {"net_income", "total_assets"},
    "inventory_turnover": {"cost_of_sales", "inventory"},
}

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
