"""계정 매핑 규칙(docs/decisions.md D6) 테스트. 규칙 파일은 실제 configs/account_map_v1.yaml을 쓴다."""

from pathlib import Path

from src.normalization.account_map import load_rules, map_account, map_report
from src.normalization.dart_facts import is_annual_period, is_reversed_period, is_unusually_long

CONFIG = load_rules(Path(__file__).resolve().parents[1] / "configs" / "account_map_v1.yaml")
NONSTD = "-표준계정코드 미사용-"


def row(sj_div, account_id, nm, value="100", detail="-", status="parsed", i=[0]):
    i[0] += 1
    ptype = "instant" if sj_div == "BS" else "duration"
    return {"row_id": f"r{i[0]}", "sj_div": sj_div, "account_id": account_id, "account_nm": nm, "account_detail": detail,
            "raw_amount": value, "value": value if status == "parsed" else None, "value_status": status,
            "period_type": ptype, "period_start": None if ptype == "instant" else "2025-01-01", "period_end": "2025-12-31",
            "period_status": "from_filing_text", "period_is_annual": None if ptype == "instant" else True}


def account(name, rows):
    return map_account(name, CONFIG["accounts"][name], rows, CONFIG)


# ---------------------------------------------------------------- 대체 금지


def test_finance_costs_are_never_used_as_interest_expense():
    result = account("interest_expense", [row("CIS", "ifrs-full_FinanceCosts", "금융원가", "5000")])
    assert result["status"] == "review_needed"
    assert result["value"] is None
    assert "대체 금지" in result["note"]


def test_cash_flow_interest_adjustment_is_only_a_candidate():
    result = account("interest_expense", [row("CF", "dart_AdjustmentsForInterestExpenses", "이자비용", "300")])
    assert result["status"] == "review_needed"
    assert result["value"] is None


def test_current_liabilities_total_is_never_short_term_debt():
    result = account("short_term_interest_bearing_debt", [row("BS", "ifrs-full_CurrentLiabilities", "유동부채", "9000")])
    assert result["status"] == "review_needed"
    assert "구성 항목 없음" in result["note"]


def test_profit_loss_in_cash_flow_statement_is_not_net_income():
    result = account("net_income", [row("CF", "ifrs-full_ProfitLoss", "당기순이익", "50")])
    assert result["status"] == "review_needed"
    result = account("net_income", [row("CF", "ifrs-full_ProfitLoss", "당기순이익", "50"), row("CIS", "ifrs-full_ProfitLoss", "당기순이익", "60")])
    assert (result["status"], result["value"]) == ("mapped", "60")


# ---------------------------------------------------------------- 표준코드 없는 항목


def test_nonstandard_row_keeps_raw_id_and_maps_only_on_exact_name():
    r = row("BS", NONSTD, "재고자산", "70")
    result = account("inventory", [r])
    assert (result["status"], result["value"]) == ("mapped", "70")
    assert r["account_id"] == NONSTD  # 원본은 그대로
    assert account("inventory", [row("BS", NONSTD, "재고자산 등", "70")])["status"] == "review_needed"  # 비슷한 이름은 안 됨


def test_name_rule_does_not_apply_to_rows_with_a_different_standard_code():
    result = account("inventory", [row("BS", "ifrs-full_OtherCurrentAssets", "재고자산", "70")])
    assert result["status"] == "review_needed"


# ---------------------------------------------------------------- 상태


def test_multiple_candidates_are_not_auto_selected():
    result = account("revenue", [row("CIS", "ifrs-full_Revenue", "매출액", "1"), row("CIS", "ifrs-full_Revenue", "영업수익", "2")])
    assert result["status"] == "review_needed"
    assert result["value"] is None


def test_detail_rows_do_not_match_total_rules():
    result = account("revenue", [row("CIS", "ifrs-full_Revenue", "매출액", "1", detail="연결재무제표 [member]")])
    assert result["status"] == "review_needed"


def test_empty_source_value_is_reported_as_empty_not_zero():
    result = account("cash", [row("BS", "ifrs-full_CashAndCashEquivalents", "현금및현금성자산", "", status="empty_in_source")])
    assert result["status"] == "empty_value"
    assert account("cash", [row("BS", "ifrs-full_CashAndCashEquivalents", "현금및현금성자산", "0")])["value"] == "0"


# ---------------------------------------------------------------- 단기 이자부채 (구성 항목의 합)


def test_short_term_debt_includes_current_lease_liabilities():
    result = account("short_term_interest_bearing_debt", [
        row("BS", "ifrs-full_ShorttermBorrowings", "단기차입금", "100"),
        row("BS", NONSTD, "유동성장기부채", "30"),
        row("BS", "ifrs-full_CurrentLeaseLiabilities", "리스부채(유동)", "7"),
        row("BS", "ifrs-full_NoncurrentLeaseLiabilities", "리스부채(비유동)", "50"),  # 비유동은 넣지 않는다
    ])
    assert (result["status"], result["value"]) == ("candidate", "137")  # 찾은 구성 항목 합계 — 완전성은 수기 확인으로
    assert result["basis"] == "리스 포함"


def test_found_components_without_completeness_evidence_stay_candidate():
    # 검토 지적 ⑤: 기타유동부채 안에 리스가 있는지 모르면 정상 합계가 아니다 (한국화장품제조 사례)
    result = account("short_term_interest_bearing_debt", [
        row("BS", "ifrs-full_ShorttermBorrowings", "단기차입부채", "5000"),
        row("BS", "ifrs-full_OtherCurrentLiabilities", "기타유동부채", "4222"),
    ])
    assert (result["status"], result["value"]) == ("candidate", "5000")
    assert "완전성 미확인" in result["note"]


def test_row_with_unconfirmed_period_is_not_mapped():
    # 검토 지적 ⑥: 기간이 확인되지 않은 행은 매핑 성공으로 올리지 않는다
    r = row("CIS", "ifrs-full_Revenue", "매출액", "100")
    r["period_status"] = "review_needed:period_not_found"
    result = account("revenue", [r])
    assert result["status"] == "review_needed"
    assert "기간 미확인" in result["note"]


def test_mapped_result_carries_the_row_period():
    result = account("revenue", [row("CIS", "ifrs-full_Revenue", "매출액", "100")])
    assert (result["period_type"], result["period_start"], result["period_end"], result["period_is_annual"]) == ("duration", "2025-01-01", "2025-12-31", True)


def test_total_and_its_components_together_are_not_double_counted():
    result = account("short_term_interest_bearing_debt", [
        row("BS", "ifrs-full_CurrentLoansReceivedAndCurrentPortionOfNoncurrentLoansReceived", "유동차입금", "130"),
        row("BS", "ifrs-full_ShorttermBorrowings", "단기차입금", "100"),
    ])
    assert result["status"] == "review_needed"
    assert "중복" in result["note"]


def test_bond_code_without_current_split_is_a_review_candidate():
    result = account("short_term_interest_bearing_debt", [
        row("BS", "ifrs-full_ShorttermBorrowings", "차입금", "100"),
        row("BS", "ifrs-full_BondsIssued", "사채", "2398"),  # 한국콜마 2023: 유동 사채가 구분 없는 코드로 옴
    ])
    assert result["status"] == "review_needed"


def test_interest_paid_is_never_used_as_interest_expense():
    result = account("interest_expense", [row("CF", "ifrs-full_InterestPaidClassifiedAsOperatingActivities", "이자의 지급", "-40")])
    assert result["status"] == "review_needed"
    assert result["value"] is None
    assert "대체 금지" in result["note"]


def test_short_term_debt_is_not_summed_when_an_ambiguous_item_exists():
    result = account("short_term_interest_bearing_debt", [
        row("BS", "ifrs-full_ShorttermBorrowings", "단기차입금", "100"),
        row("BS", "ifrs-full_CurrentFinancialLiabilities", "단기금융부채", "500"),
    ])
    assert result["status"] == "review_needed"
    assert result["value"] is None


def test_map_report_covers_every_required_account():
    names = {r["canonical_account"] for r in map_report([], CONFIG)}
    assert names == set(CONFIG["accounts"])
    assert len(names) == 15  # 필요한 13개 + 매각예정자산·부채 (D8)


# ---------------------------------------------------------------- 기간 규칙 분리


def test_annual_flag_uses_report_type_and_actual_period():
    assert is_annual_period("2024-01-01", "2024-12-31", "11011")
    assert is_annual_period("2024-03-01", "2025-02-28", "11011")
    assert is_annual_period("2024-01-01", "2024-12-29", "11011")  # 52주(364일) 보고기간
    assert not is_annual_period("2025-07-01", "2025-12-31", "11011")  # 결산기 변경 등 짧은 기간 — 받아들이되 연간 아님
    assert not is_annual_period("2024-01-01", "2024-12-31", "11012")  # 반기보고서


def test_only_reversed_dates_are_errors():
    assert is_reversed_period("2025-01-01", "2024-12-31")
    assert not is_reversed_period("2023-07-01", "2024-12-31")
    assert is_unusually_long("2023-07-01", "2024-12-31")  # 버리지 않고 확인 필요로
    assert not is_unusually_long("2024-01-01", "2024-12-31")
