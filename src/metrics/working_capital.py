"""운전자본 변동 표시 (docs/decisions.md D14). 추세 해석용이며 점수·일회성 판정에 쓰지 않는다.

- 현금흐름표(간접법) 본문의 '영업활동으로 인한 자산·부채의 변동' 공시 소계를 그대로 쓴다.
  재무상태표 잔액 차이로 대신하지 않고, OCF에서 빼서 '정상 OCF'를 만들지 않는다
- 부호: 같은 현금흐름표의 항등식(당기순이익 + 비현금 조정 + 운전자본 변동 = 영업에서 창출된 현금 또는 영업현금흐름)이
  원 단위까지 맞을 때만 '확인'. 아니면 '부호 미확인' (금액은 표시, OCF 영향 방향은 미확인)
- 구성 항목(재고·채권·채무 등): 본문 API에 소계만 있거나 일부만 있어 범위를 확인하지 못했다 → '구성 범위 미확인'.
  찾은 구성 항목을 더해 합계를 만들지 않는다
- 주의 표시: |운전자본 변동| / 평균 총자산 ≥ 기준(설정 파일, 탐색용 초기값)이면 '운전자본 변동 규모 주의'.
  금액이 크다는 사실만으로 일회성이라고 판정하지 않는다
"""

from decimal import Decimal

WC_ID = "dart_AdjustmentsForAssetsLiabilitiesOfOperatingActivities"
NI_ID = "ifrs-full_ProfitLoss"
ADJ_ID = "ifrs-full_AdjustmentsForReconcileProfitLoss"
CGO_ID = "ifrs-full_CashFlowsFromUsedInOperations"
OCF_ID = "ifrs-full_CashFlowsFromUsedInOperatingActivities"
# 영업에서 창출된 현금 → 영업현금흐름 사이의 항목. 회사마다 부호 표기가 달라 (지급을 양수로 적는지, 음수로 적는지) 두 표기를 모두 시험한다
OUTFLOW_IDS = ("ifrs-full_InterestPaidClassifiedAsOperatingActivities", "ifrs-full_IncomeTaxesPaidRefundClassifiedAsOperatingActivities",
               "ifrs-full_IncomeTaxesPaidRefund")
INFLOW_IDS = ("ifrs-full_InterestReceivedClassifiedAsOperatingActivities", "ifrs-full_DividendsReceivedClassifiedAsOperatingActivities",
              "ifrs-full_DividendsReceivedClassifiedAsInvestingActivities")


def _one(cf: list[dict], account_id: str) -> Decimal | None:
    hits = [r for r in cf if r["account_id"] == account_id and r["account_detail"] == "-" and r["value_status"] == "parsed"]
    return Decimal(hits[0]["value"]) if len(hits) == 1 else None


def sign_check(cf: list[dict], wc: Decimal) -> tuple[str, str]:
    """현금흐름표 항등식으로 운전자본 소계의 부호를 확인한다."""
    ni, adj = _one(cf, NI_ID), _one(cf, ADJ_ID)
    if ni is None or adj is None:
        return "부호 미확인", "항등식 대조 불가 — 당기순이익 또는 비현금 조정 행 없음"
    generated = ni + adj + wc
    cgo = _one(cf, CGO_ID)
    if cgo is not None and generated == cgo:
        return "확인", f"당기순이익 {ni:,} + 비현금 조정 {adj:,} + 운전자본 변동 {wc:,} = 영업에서 창출된 현금 {cgo:,}"
    ocf = _one(cf, OCF_ID)
    outs = [v for v in (_one(cf, i) for i in OUTFLOW_IDS) if v is not None]
    ins = [v for v in (_one(cf, i) for i in INFLOW_IDS) if v is not None]
    if ocf is not None:
        # 표기 1: 지급을 양수로 적음 (빼야 함). 표기 2: 부호를 붙여 적음 (그대로 더함)
        for label, total in (("지급 양수 표기", generated - sum(outs) + sum(ins)), ("부호 포함 표기", generated + sum(outs) + sum(ins))):
            if total == ocf:
                return "확인", f"당기순이익 {ni:,} + 비현금 조정 {adj:,} + 운전자본 변동 {wc:,} ± 이자·배당·법인세({label}) = 영업현금흐름 {ocf:,}"
    return "부호 미확인", "현금흐름표 항등식이 원 단위로 맞지 않음 — 본문에 다른 영업 항목이 있을 수 있음"


def working_capital(cf: list[dict], avg_assets: Decimal | None, threshold: float) -> dict:
    """선택 보고서 당기 열의 현금흐름표 행으로 운전자본 변동 표시값을 만든다."""
    hits = [r for r in cf if r["account_id"] == WC_ID and r["account_detail"] == "-"]
    out = {"wc_amount": None, "wc_status": "", "sign_status": "", "ocf_direction": "", "sign_evidence": "",
           "components": "구성 범위 미확인 — 구성 항목(재고·채권·채무 등)은 주석 확인 전이라 표시하지 않음, 합계도 만들지 않음",
           "wc_to_avg_assets": None, "caution": "", "source": ""}
    if not hits:
        return {**out, "wc_status": "누락", "sign_status": "해당 없음",
                "caution": "확인 불가 — 현금흐름표 본문에 운전자본 변동 소계 없음 (주석 확인 필요)"}
    if len(hits) > 1:
        return {**out, "wc_status": "확인 필요", "sign_status": "해당 없음", "caution": f"운전자본 변동 소계 행 {len(hits)}개 — 자동 선택하지 않음"}
    row = hits[0]
    out["source"] = f"{row['rcept_no']} {row['account_nm']} ({row['account_id']}) 원문 '{row['raw_amount']}'"
    if row["value_status"] != "parsed":
        return {**out, "wc_status": "값 없음", "sign_status": "해당 없음", "caution": "확인 불가 — 소계 행 금액 결측"}
    if row.get("period_is_annual") is not True:
        return {**out, "wc_status": "확인 필요", "sign_status": "해당 없음", "caution": f"연간 기간 아님 ({row.get('period_start')}~{row.get('period_end')})"}
    wc = Decimal(row["value"])
    sign, evidence = sign_check(cf, wc)
    direction = ("OCF 증가 방향 (운전자본에서 현금 유입)" if wc > 0 else "OCF 감소 방향 (운전자본에 현금 투입)" if wc < 0 else "영향 없음 (0)") \
        if sign == "확인" else "미확인 (부호 미확인)"
    out.update(wc_amount=str(wc), wc_status="공시 소계", sign_status=sign, sign_evidence=evidence, ocf_direction=direction)
    if avg_assets is None or avg_assets <= 0:
        return {**out, "caution": "규모 판단 불가 — 평균 총자산 없음"}
    share = float(abs(wc) / avg_assets)
    out["wc_to_avg_assets"] = share
    out["caution"] = f"운전자본 변동 규모 주의 (|변동| / 평균 총자산 {share:.1%} ≥ {threshold:.0%})" if share >= threshold else ""
    return out
