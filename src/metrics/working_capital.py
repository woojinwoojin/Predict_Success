"""운전자본 변동 표시 (docs/decisions.md D14, D15). 추세 해석용이며 점수·일회성 판정에 쓰지 않는다.

확인 항목을 나눠 기록한다 — 하나가 확인됐다고 다른 항목까지 확인된 것으로 보지 않는다.
- 소계 존재: 현금흐름표(간접법) 본문의 '영업활동으로 인한 자산·부채의 변동' 공시 소계를 그대로 쓴다.
  재무상태표 잔액 차이로 대신하지 않고, OCF에서 빼서 '정상 OCF'를 만들지 않는다
- 항등식 검산: 같은 보고서에서 당기순이익 + 비현금 조정 + 운전자본 변동 = '영업에서 창출된 현금'이 원 단위로 맞으면
  소계 부호의 **산술적 일관성**이 확인된 것이다. '영업에서 창출된 현금' 행이 없으면 이자·배당·법인세 행을 거쳐
  최종 영업현금흐름(OCF)과 대조한다 (두 대상은 다르다 — 검산 대상과 사용한 행의 이름·위치를 남긴다).
  맞지 않으면 '검증 미완료'이고 현금흐름 영향 방향을 표시하지 않는다
- 구성·완전성: 소계 1행만 사용했다. 구성 항목(재고·채권·채무 등)의 범위와 완전성은 확인하지 않았다 — 항등식 일치로도 확인되지 않는다.
  찾은 구성 항목을 더해 합계를 만들지 않는다
- 규모 경고: |운전자본 변동| / 평균 총자산 ≥ 기준(설정 파일, 탐색용 초기값). 검산 미완료면 '규모 주의 — 현금흐름 영향 방향 미확인'
- 일회성 여부: 별도 근거가 없으면 미판정. 금액이 크다는 사실만으로 일회성이라고 하지 않는다
"""

from decimal import Decimal

WC_ID = "dart_AdjustmentsForAssetsLiabilitiesOfOperatingActivities"
NI_ID = "ifrs-full_ProfitLoss"
# 현금흐름표 첫 줄의 당기순이익을 별도 코드로 공시하는 회사가 있다 (코스메카 2022). 한 행만 있을 때만 쓴다
NI_IDS = (NI_ID, "dart_ProfitLossForStatementOfCashFlows")
ADJ_ID = "ifrs-full_AdjustmentsForReconcileProfitLoss"
CGO_ID = "ifrs-full_CashFlowsFromUsedInOperations"
OCF_ID = "ifrs-full_CashFlowsFromUsedInOperatingActivities"
# 영업에서 창출된 현금 → 영업현금흐름 사이의 항목. 회사마다 부호 표기가 달라 (지급을 양수로 적는지, 음수로 적는지) 두 표기를 모두 시험한다
OUTFLOW_IDS = ("ifrs-full_InterestPaidClassifiedAsOperatingActivities", "ifrs-full_IncomeTaxesPaidRefundClassifiedAsOperatingActivities",
               "ifrs-full_IncomeTaxesPaidRefund")
INFLOW_IDS = ("ifrs-full_InterestReceivedClassifiedAsOperatingActivities", "ifrs-full_DividendsReceivedClassifiedAsOperatingActivities",
              "ifrs-full_DividendsReceivedClassifiedAsInvestingActivities")

CHECKED, UNCHECKED = "산술 일치", "검증 미완료"
COMPLETENESS = "소계 1행만 사용 — 구성 항목(재고·채권·채무 등)의 범위·완전성 미확인, 구성 항목 합계를 만들지 않음"
ONE_OFF = "미판정 — 별도 근거 없음"


def _row(cf: list[dict], account_id: str) -> dict | None:
    hits = [r for r in cf if r["account_id"] == account_id and r["account_detail"] == "-" and r["value_status"] == "parsed"]
    return hits[0] if len(hits) == 1 else None


def _where(r: dict) -> str:
    # ord는 API 응답 안의 순서다 — 원문 표의 줄 번호가 아니다
    return f"'{r['account_nm']}' ({r['account_id']}, 현금흐름표 API 응답 순서 {r.get('ord', '?')})"


def sign_check(cf: list[dict], wc_row: dict) -> dict:
    """현금흐름표 항등식으로 운전자본 소계 부호의 산술적 일관성을 확인한다."""
    wc = Decimal(wc_row["value"])
    ni_rows = [r for r in (_row(cf, i) for i in NI_IDS) if r is not None]
    ni, adj = (ni_rows[0] if len(ni_rows) == 1 else None), _row(cf, ADJ_ID)
    out = {"sign_status": UNCHECKED, "identity_target": "", "identity_rows": "", "sign_evidence": ""}
    if ni is None or adj is None:
        return {**out, "sign_evidence": "검산 불가 — 당기순이익 또는 비현금 조정 행이 없거나 여러 개"}
    generated = Decimal(ni["value"]) + Decimal(adj["value"]) + wc
    used = [ni, adj, wc_row]
    cgo = _row(cf, CGO_ID)
    if cgo is not None:
        if generated == Decimal(cgo["value"]):
            return {"sign_status": CHECKED, "identity_target": "영업에서 창출된 현금", "identity_rows": " + ".join(map(_where, used)) + " = " + _where(cgo),
                    "sign_evidence": f"{Decimal(ni['value']):,} + {Decimal(adj['value']):,} + {wc:,} = {Decimal(cgo['value']):,}"}
        return {**out, "identity_target": "영업에서 창출된 현금", "sign_evidence": f"합 {generated:,} ≠ 영업에서 창출된 현금 {Decimal(cgo['value']):,}"}
    ocf = _row(cf, OCF_ID)
    if ocf is None:
        return {**out, "sign_evidence": "검산 불가 — 영업에서 창출된 현금·영업현금흐름 행 없음"}
    outs = [r for r in (_row(cf, i) for i in OUTFLOW_IDS) if r is not None]
    ins = [r for r in (_row(cf, i) for i in INFLOW_IDS) if r is not None]
    sum_out, sum_in = sum(Decimal(r["value"]) for r in outs), sum(Decimal(r["value"]) for r in ins)
    # 표기 1: 지급을 양수로 적음 (빼야 함). 표기 2: 부호를 붙여 적음 (그대로 더함)
    for label, total in (("지급 양수 표기", generated - sum_out + sum_in), ("부호 포함 표기", generated + sum_out + sum_in)):
        if total == Decimal(ocf["value"]):
            return {"sign_status": CHECKED, "identity_target": f"영업현금흐름 (영업에서 창출된 현금 행 없음 — 이자·배당·법인세 행 경유, {label})",
                    "identity_rows": " + ".join(map(_where, used)) + " ± " + ", ".join(map(_where, outs + ins)) + " = " + _where(ocf),
                    "sign_evidence": f"{total:,} = 영업현금흐름 {Decimal(ocf['value']):,}"}
    return {**out, "identity_target": "영업현금흐름 (이자·배당·법인세 행 경유)",
            "sign_evidence": "원 단위로 맞지 않음 — 본문에 다른 영업 항목이 있을 수 있음"}


def working_capital(cf: list[dict], avg_assets: Decimal | None, threshold: float) -> dict:
    """선택 보고서 당기 열의 현금흐름표 행으로 운전자본 변동 표시값을 만든다."""
    hits = [r for r in cf if r["account_id"] == WC_ID and r["account_detail"] == "-"]
    out = {"wc_amount": None, "wc_status": "", "sign_status": "", "identity_target": "", "identity_rows": "", "sign_evidence": "",
           "ocf_direction": "", "completeness": COMPLETENESS, "wc_to_avg_assets": None, "caution_flag": False, "caution": "",
           "one_off": ONE_OFF, "source": ""}
    if not hits:
        return {**out, "wc_status": "추가 확인 필요 — 본문에 소계 없음", "sign_status": "해당 없음", "completeness": "확인 불가",
                "caution": "확인 불가 — 현금흐름표 본문에 운전자본 변동 소계 없음 (주석 확인 필요)"}
    if len(hits) > 1:
        return {**out, "wc_status": "추가 확인 필요 — 소계 행 여러 개", "sign_status": "해당 없음",
                "caution": f"운전자본 변동 소계 행 {len(hits)}개 — 자동 선택하지 않음"}
    row = hits[0]
    out["source"] = f"{row['rcept_no']} 현금흐름표 {_where(row)} 원문 '{row['raw_amount']}'"
    if row["value_status"] != "parsed":
        return {**out, "wc_status": "추가 확인 필요 — 소계 금액 결측", "sign_status": "해당 없음", "caution": "확인 불가 — 소계 행 금액 결측"}
    if row.get("period_is_annual") is not True:
        return {**out, "wc_status": "추가 확인 필요 — 연간 기간 아님", "sign_status": "해당 없음",
                "caution": f"연간 기간 아님 ({row.get('period_start')}~{row.get('period_end')})"}
    wc = Decimal(row["value"])
    check = sign_check(cf, row)
    if check["sign_status"] == CHECKED:
        direction = "증가 (운전자본에서 현금 유입)" if wc > 0 else "감소 (운전자본에 현금 투입)" if wc < 0 else "영향 없음 (0)"
    else:
        direction = "미확인 — 검산 미완료"
    out.update(wc_amount=str(wc), wc_status="공시 소계 사용", ocf_direction=direction, **check)
    if avg_assets is None or avg_assets <= 0:
        return {**out, "caution": "규모 판단 불가 — 평균 총자산 없음"}
    share = float(abs(wc) / avg_assets)
    out["wc_to_avg_assets"] = share
    if share >= threshold:
        size = f"|변동| / 평균 총자산 {share:.1%} ≥ {threshold:.0%}, 탐색 기준"
        out["caution_flag"] = True
        out["caution"] = (f"운전자본 변동 규모 주의 ({size})" if check["sign_status"] == CHECKED
                          else f"규모 주의 — 현금흐름 영향 방향 미확인 ({size})")
    return out
