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

import json
from decimal import Decimal

WC_ID = "dart_AdjustmentsForAssetsLiabilitiesOfOperatingActivities"
NI_ID = "ifrs-full_ProfitLoss"
# 현금흐름표 첫 줄의 당기순이익을 별도 코드로 공시하는 회사가 있다 (코스메카 2022). 한 행만 있을 때만 쓴다
NI_IDS = (NI_ID, "dart_ProfitLossForStatementOfCashFlows")
ADJ_ID = "ifrs-full_AdjustmentsForReconcileProfitLoss"
CGO_ID = "ifrs-full_CashFlowsFromUsedInOperations"
OCF_ID = "ifrs-full_CashFlowsFromUsedInOperatingActivities"
# 영업에서 창출된 현금 → 영업현금흐름 사이의 항목. 코드에 '영업활동' 구분이 있는 행만 자동으로 쓴다.
# 투자·재무활동 코드, 활동 구분 없는 코드, 표준코드 없는 행은 원문 확인표(data/manual/cf_activity_reviews.csv)에서
# 원문 표시가 '영업활동'으로 확인된 경우에만 쓰고, 코드와 원문이 다르면 충돌로 기록한다 (D16).
# 배당 수취·이자 지급 등은 회계정책에 따라 영업 또는 투자·재무활동으로 분류될 수 있다 (K-IFRS 1007)
OUTFLOW_IDS = ("ifrs-full_InterestPaidClassifiedAsOperatingActivities", "ifrs-full_IncomeTaxesPaidRefundClassifiedAsOperatingActivities")
INFLOW_IDS = ("ifrs-full_InterestReceivedClassifiedAsOperatingActivities", "ifrs-full_DividendsReceivedClassifiedAsOperatingActivities")
# 코드만으로는 영업활동이 아니거나 확인되지 않는 행 — 원문 확인 없이 검산에 넣지 않는다
NON_OPERATING_CODES = ("ifrs-full_DividendsReceivedClassifiedAsInvestingActivities", "ifrs-full_InterestReceivedClassifiedAsInvestingActivities",
                       "ifrs-full_InterestPaidClassifiedAsFinancingActivities", "ifrs-full_IncomeTaxesPaidRefund")

CHECKED, UNCHECKED = "산술 일치", "검증 미완료"
COMPLETENESS = "소계 1행만 사용 — 구성 항목(재고·채권·채무 등)의 범위·완전성 미확인, 구성 항목 합계를 만들지 않음"
ONE_OFF = "미판정 — 별도 근거 없음"


def _row(cf: list[dict], account_id: str) -> dict | None:
    hits = [r for r in cf if r["account_id"] == account_id and r["account_detail"] == "-" and r["value_status"] == "parsed"]
    return hits[0] if len(hits) == 1 else None


def _where(r: dict) -> str:
    # ord는 API 응답 안의 순서다 — 원문 표의 줄 번호가 아니다
    return f"'{r['account_nm']}' ({r['account_id']}, 현금흐름표 API 응답 순서 {r.get('ord', '?')})"


def _code_activity(account_id: str) -> str:
    for key, label in (("ClassifiedAsOperatingActivities", "영업활동"), ("ClassifiedAsInvestingActivities", "투자활동"),
                       ("ClassifiedAsFinancingActivities", "재무활동")):
        if account_id.endswith(key):
            return label
    return "표준코드 없음" if account_id.startswith("-") else "코드에 활동 구분 없음"


def _term(r: dict, sign: int, basis: str) -> dict:
    v = Decimal(r["value"])
    return {"item": r["account_nm"], "code": r["account_id"], "api_order": r.get("ord", "?"), "api_value": str(v),
            "sign": "+" if sign > 0 else "−", "basis": basis, "applied": str(v * sign)}


def sign_check(cf: list[dict], wc_row: dict, reviews: list[dict] = ()) -> dict:
    """현금흐름표 항등식으로 운전자본 소계 부호의 산술적 일관성을 확인한다 (D15, D16).

    검산식을 항목별(API 값·적용 부호·부호 근거·적용 값)로 펼치고 합계·대상 금액·잔차를 남긴다.
    산술 일치(sign_status)와 활동 분류 확인(activity_status)은 별도 상태다.
    """
    ni_rows = [r for r in (_row(cf, i) for i in NI_IDS) if r is not None]
    ni, adj = (ni_rows[0] if len(ni_rows) == 1 else None), _row(cf, ADJ_ID)
    out = {"sign_status": UNCHECKED, "identity_target": "", "identity_rows": "", "sign_evidence": "", "identity_terms": "[]",
           "identity_total": None, "identity_target_value": None, "identity_residual": None, "activity_status": "", "activity_conflicts": ""}
    if ni is None or adj is None:
        return {**out, "sign_evidence": "검산 불가 — 당기순이익 또는 비현금 조정 행이 없거나 여러 개"}
    core = [_term(ni, 1, "간접법 출발 항목 — API 부호 그대로"), _term(adj, 1, "조정 합계 — API 부호 그대로"),
            _term(wc_row, 1, "검산 대상 소계 — API 부호 그대로")]

    def result(terms, target_row, target_label, activity, conflicts, note=""):
        total = sum(Decimal(t["applied"]) for t in terms)
        target = Decimal(target_row["value"])
        ok = total == target
        evidence = f"합계 {total:,} = {target_label} {target:,}" if ok else f"합계 {total:,} ≠ {target_label} {target:,} (잔차 {target - total:,})"
        return {"sign_status": CHECKED if ok else UNCHECKED, "identity_target": target_label,
                "identity_rows": " + ".join(_where(r) for r in [ni, adj, wc_row]) + " … = " + _where(target_row),
                "sign_evidence": evidence + (f" — {note}" if note else ""),
                "identity_terms": json.dumps(terms, ensure_ascii=False), "identity_total": str(total), "identity_target_value": str(target),
                "identity_residual": str(target - total), "activity_status": activity, "activity_conflicts": conflicts}

    cgo = _row(cf, CGO_ID)
    if cgo is not None:
        return result(core, cgo, "영업에서 창출된 현금", "해당 없음 — 사이 항목 없이 영업에서 창출된 현금과 대조", "")
    ocf = _row(cf, OCF_ID)
    if ocf is None:
        return {**out, "sign_evidence": "검산 불가 — 영업에서 창출된 현금·영업현금흐름 행 없음"}

    # 사이 항목: 코드상 영업활동 행 + 원문 확인표에서 영업활동으로 확인된 행
    between, code_only, reviewed, conflicts, excluded = [], [], [], [], []
    for ids, flow in ((OUTFLOW_IDS, "유출"), (INFLOW_IDS, "유입")):
        for r in (_row(cf, i) for i in ids):
            if r is not None:
                between.append((r, flow, "코드상 영업활동 (원문 미대조)"))
                code_only.append(r["account_nm"])
    for r in cf:
        if r["value_status"] != "parsed" or r["account_detail"] != "-":
            continue
        match = [e for e in reviews if e["rcept_no"] == r["rcept_no"] and e["account_nm"] == r["account_nm"]
                 and Decimal(e["api_value"]) == Decimal(r["value"]) and e["status"] == "confirmed"]
        code_act = _code_activity(r["account_id"])
        if match and match[0]["original_section"] == "영업활동":
            e = match[0]
            between.append((r, e["flow"], f"원문 확인 영업활동 ({e['review_id']})"))
            reviewed.append(e["review_id"])
            if code_act != "영업활동":
                conflicts.append(f"{e['review_id']} '{r['account_nm']}': API 코드 [{code_act}] ↔ 원문 표시 [영업활동]")
        elif r["account_id"] in NON_OPERATING_CODES:
            excluded.append(f"'{r['account_nm']}' ({code_act}, 원문 미확인)")
    parts = []
    if reviewed:
        parts.append("원문 확인 " + "·".join(reviewed))
    if code_only:
        parts.append(("나머지 " if reviewed else "") + "코드상 영업활동 — 원문 미대조")
    activity = ", ".join(parts) or "사이 항목 없음"
    if excluded:
        activity += f"; 제외: {', '.join(excluded)}"
    # 표기 1: 지급을 양수로 적음 (차감). 표기 2: 부호를 붙여 적음 (그대로 더함)
    attempts = []
    for label, out_sign, out_basis in (("지급 양수 표기", -1, "지급 항목 — API 값 양수 표기 → 차감"),
                                       ("부호 포함 표기", 1, "지급 항목 — API 값에 부호 포함 → 그대로")):
        terms = core + [_term(r, out_sign if flow == "유출" else 1, (out_basis if flow == "유출" else "수취 항목 → 그대로") + f"; {why}")
                        for r, flow, why in between]
        res = result(terms, ocf, f"영업현금흐름 (이자·배당·법인세 행 경유, {label})", activity, "; ".join(conflicts),
                     "원문 미확인 행 제외" if excluded else "")
        if res["sign_status"] == CHECKED:
            return res
        attempts.append(res)
    best = min(attempts, key=lambda a: abs(Decimal(a["identity_residual"])))
    return {**best, "sign_evidence": best["sign_evidence"] + " — 본문에 다른 영업 항목이 있거나 활동 분류 확인이 필요할 수 있음"}


def working_capital(cf: list[dict], avg_assets: Decimal | None, threshold: float, reviews: list[dict] = ()) -> dict:
    """선택 보고서 당기 열의 현금흐름표 행으로 운전자본 변동 표시값을 만든다."""
    hits = [r for r in cf if r["account_id"] == WC_ID and r["account_detail"] == "-"]
    out = {"wc_amount": None, "wc_status": "", "sign_status": "", "identity_target": "", "identity_rows": "", "sign_evidence": "",
           "identity_terms": "[]", "identity_total": None, "identity_target_value": None, "identity_residual": None,
           "activity_status": "", "activity_conflicts": "",
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
    check = sign_check(cf, row, reviews)
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
