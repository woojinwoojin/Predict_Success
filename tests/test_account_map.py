"""계정 매핑 규칙(docs/decisions.md D6) 테스트. 규칙 파일은 실제 configs/account_map_v1.yaml을 쓴다."""

from pathlib import Path

import pytest

from src.normalization.account_map import load_rules, map_account, map_report
from src.normalization.dart_facts import is_annual_period, is_plausible_fiscal_period

CONFIG = load_rules(Path(__file__).resolve().parents[1] / "configs" / "account_map_v1.yaml")
NONSTD = "-표준계정코드 미사용-"


def row(sj_div, account_id, nm, value="100", detail="-", status="parsed", i=[0]):
    i[0] += 1
    return {"row_id": f"r{i[0]}", "sj_div": sj_div, "account_id": account_id, "account_nm": nm, "account_detail": detail,
            "raw_amount": value, "value": value if status == "parsed" else None, "value_status": status}


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


def test_short_term_debt_sums_explicit_current_components():
    result = account("short_term_interest_bearing_debt", [
        row("BS", "ifrs-full_ShorttermBorrowings", "단기차입금", "100"),
        row("BS", NONSTD, "유동성장기부채", "30"),
        row("BS", "ifrs-full_CurrentLeaseLiabilities", "리스부채(유동)", "7"),  # 정책 미정 — 합계 제외
    ])
    assert (result["status"], result["value"]) == ("mapped", "130")
    assert "정책 미정" in result["note"]


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
    assert len(names) == 13


# ---------------------------------------------------------------- 기간 규칙 분리


def test_short_fiscal_period_is_accepted_but_marked_non_annual():
    assert is_plausible_fiscal_period("2025-07-01", "2025-12-31")  # 결산기 변경 등 짧은 기간은 정상
    assert not is_annual_period("2025-07-01", "2025-12-31")
    assert is_annual_period("2024-01-01", "2024-12-31")
    assert is_annual_period("2024-03-01", "2025-02-28")


@pytest.mark.parametrize("start, end", [("2024-01-01", "2025-12-31"), ("2025-01-01", "2024-12-31")])
def test_misattached_dates_are_rejected(start, end):
    assert not is_plausible_fiscal_period(start, end)
