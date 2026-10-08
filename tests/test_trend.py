"""2022~2025 회고 추세 규칙(docs/decisions.md D14) 테스트."""

from decimal import Decimal

import pandas as pd

from src.metrics.engine import ocf_to_avg_assets, revenue_yoy_growth
from src.metrics.run_trend import CAUTION, COMPARABLE, HOLD_REFERENCE, NOT_COMPARABLE, comparability, review_reasons, scope_reasons
from src.metrics.working_capital import ADJ_ID, CGO_ID, NI_ID, OCF_ID, WC_ID, working_capital
from src.normalization import baseline
from src.normalization.manual import apply_hfs_confirmation
from src.normalization.selection import Selection
from tests.test_metrics import getter

# ---------------------------------------------------------------- 연도 인자와 2021 기초값


def test_past_year_uses_prior_year_balances():
    get = getter({("operating_cash_flow", 2022): 10, ("total_assets", 2021): 90, ("total_assets", 2022): 110})
    r = ocf_to_avg_assets(get, 2022)
    assert r.calc_status == "normal" and r.value == 0.1
    assert r.period == "2022 (평균: 2021말·2022말)"


def test_missing_opening_balance_holds_only_that_metric_no_ending_balance_substitute():
    get = getter({("operating_cash_flow", 2022): 10, ("total_assets", 2022): 110})
    r = ocf_to_avg_assets(get, 2022)
    assert r.calc_status == "not_computed" and r.value is None


def test_revenue_yoy_growth():
    r = revenue_yoy_growth(getter({("revenue", 2021): 100, ("revenue", 2022): 125}), 2022)
    assert r.calc_status == "normal" and abs(r.value - 0.25) < 1e-12 and r.period == "2021→2022"
    assert revenue_yoy_growth(getter({("revenue", 2021): 0, ("revenue", 2022): 5}), 2022).calc_status == "not_computed"
    assert revenue_yoy_growth(getter({("revenue", 2022): 5}), 2022).calc_status == "not_computed"  # 전년 없음 → 0으로 채우지 않음


def fact(**kw):
    base = {"row_id": "r1", "snapshot_id": "S2022", "corp_code": "C", "fs_div": "CFS", "sj_div": "BS", "account_id": "ifrs-full_Assets",
            "account_nm": "자산총계", "account_detail": "-", "value": "100", "raw_amount": "100", "value_status": "parsed", "currency": "KRW",
            "source_column": "전기", "period_type": "instant", "period_start": None, "period_end": "2021-12-31",
            "period_status": "from_filing_text", "period_is_annual": None, "rcept_no": "R2022", "report_bsns_year": "2022",
            "bs_section": None, "bs_section_sum_ok": False}
    return {**base, **kw}


CONFIG = {"nonstandard_account_id": "-표준계정코드 미사용-", "statements": {"balance": ["BS"], "income": ["IS", "CIS"]},
          "accounts": {"total_assets": {"label": "총자산", "match": [{"statement": "balance", "account_id": "ifrs-full_Assets", "account_detail": "-"}]},
                       "inventory": {"label": "재고자산", "match": [{"statement": "balance", "account_id": "ifrs-full_Inventories", "account_detail": "-"}]},
                       "revenue": {"label": "매출", "match": [{"statement": "income", "account_id": "ifrs-full_Revenue", "account_detail": "-"}]}}}
PUBLISHED = {"R2022": {"rcept_dt": "20230320"}, "R2023": {"rcept_dt": "20240320"}, "R2026": {"rcept_dt": "20270320"}}


def run_baseline(monkeypatch, rows):
    monkeypatch.setattr(baseline, "select_snapshot", lambda *a: Selection("selected", "S2022", "R2022", ""))
    out = baseline.build_baseline(rows, [], {}, PUBLISHED, CONFIG, "C", "CFS", "회사", "20261008")
    return {r["canonical_account"]: r for r in out}


def test_baseline_takes_prior_column_of_2022_report_and_cross_checks(monkeypatch):
    rows = [fact(), fact(row_id="r2", snapshot_id="S2023", rcept_no="R2023", report_bsns_year="2023", source_column="전전기")]
    b = run_baseline(monkeypatch, rows)["total_assets"]
    assert b["status"] == "mapped" and b["value"] == "100" and b["bsns_year"] == 2021
    assert b["source_column"] == "전기" and b["rcept_no"] == "R2022" and b["cross_check"] == "일치"


def test_baseline_mismatch_with_other_report_is_held(monkeypatch):
    rows = [fact(), fact(row_id="r2", snapshot_id="S2023", rcept_no="R2023", value="101", source_column="전전기")]
    b = run_baseline(monkeypatch, rows)["total_assets"]
    assert b["status"] == "review_needed" and b["value"] is None


def test_baseline_ignores_reports_published_after_as_of(monkeypatch):
    rows = [fact(), fact(row_id="r2", snapshot_id="S2026", rcept_no="R2026", value="999", source_column="전전기")]
    assert run_baseline(monkeypatch, rows)["total_assets"]["cross_check"] == "대조 없음"


def test_baseline_with_wrong_period_or_currency_is_held(monkeypatch):
    assert run_baseline(monkeypatch, [fact(period_end="2022-12-31")])["total_assets"]["status"] == "review_needed"
    assert run_baseline(monkeypatch, [fact(currency="USD")])["total_assets"]["status"] == "review_needed"


def test_baseline_without_value_is_not_zero_filled(monkeypatch):
    b = run_baseline(monkeypatch, [fact()])["inventory"]
    assert b["status"] == "review_needed" and b["value"] is None


# ---------------------------------------------------------------- 매각예정 금액 결측의 원문 확인


KEY = {"corp_code": "C", "fs_div": "CFS"}
MISSING = {"reported": "500", "held_for_sale": None, "included": "확인 필요", "adjusted": None,
           "evidence": "매각예정 행은 있으나 금액 결측·변환 실패: 매각예정자산 ''", "period": {}}


def hfs(**kw):
    base = {"confirmation_id": "H1", "corp_code": "C", "fs_div": "CFS", "kind": "assets", "period_end": "2023-12-31", "rcept_no": "R2023",
            "confirmed_amount": "0", "source_location": "주석 38", "evidence": "당기말 '-'", "status": "confirmed", "method": "test"}
    return {**base, **kw}


def test_confirmed_zero_resolves_missing_held_for_sale():
    adj = apply_hfs_confirmation(MISSING, [hfs()], KEY, "assets", "R2023", "2023-12-31")
    assert adj["included"] == "해당 없음" and adj["adjusted"] == "500" and adj["held_for_sale"] == "0"
    assert adj["evidence"].startswith("원문 확인 H1")


def test_hfs_confirmation_is_not_carried_to_other_report_or_year():
    assert apply_hfs_confirmation(MISSING, [hfs(rcept_no="R_OLD")], KEY, "assets", "R2023", "2023-12-31")["included"] == "확인 필요"
    assert apply_hfs_confirmation(MISSING, [hfs(period_end="2022-12-31")], KEY, "assets", "R2023", "2023-12-31")["included"] == "확인 필요"
    assert apply_hfs_confirmation(MISSING, [hfs(status="needs_review")], KEY, "assets", "R2023", "2023-12-31")["included"] == "확인 필요"


def test_nonzero_confirmed_amount_or_negative_balance_stays_open():
    assert apply_hfs_confirmation(MISSING, [hfs(confirmed_amount="7")], KEY, "assets", "R2023", "2023-12-31")["included"] == "확인 필요"
    negative = {**MISSING, "evidence": "매각예정 잔액이 음수 (-35,423,774) — 원문 확인 전 조정하지 않음"}
    assert apply_hfs_confirmation(negative, [hfs()], KEY, "assets", "R2023", "2023-12-31") == negative


# ---------------------------------------------------------------- 운전자본 변동


def cf(account_id, value, **kw):
    return {"account_id": account_id, "account_detail": "-", "value": str(value), "value_status": "parsed", "raw_amount": str(value),
            "account_nm": account_id, "rcept_no": "R", "period_is_annual": True, **kw}


def test_working_capital_uses_reported_subtotal_and_confirms_sign_by_identity():
    rows = [cf(NI_ID, 50), cf(ADJ_ID, 30), cf(WC_ID, -20), cf(CGO_ID, 60)]
    wc = working_capital(rows, Decimal(1000), 0.05)
    assert wc["wc_amount"] == "-20" and wc["sign_status"] == "확인" and wc["ocf_direction"].startswith("OCF 감소")
    assert wc["caution"] == ""  # 2% < 5%
    assert "구성 범위 미확인" in wc["components"]


def test_identity_with_operating_cash_flow_and_paid_items():
    rows = [cf(NI_ID, 50), cf(ADJ_ID, 30), cf(WC_ID, 20), cf(OCF_ID, 92),
            cf("ifrs-full_IncomeTaxesPaidRefund", 10), cf("ifrs-full_InterestReceivedClassifiedAsOperatingActivities", 2)]
    assert working_capital(rows, Decimal(1000), 0.05)["sign_status"] == "확인"


def test_sign_unconfirmed_when_identity_fails_and_direction_not_stated():
    rows = [cf(NI_ID, 50), cf(ADJ_ID, 30), cf(WC_ID, -100), cf(CGO_ID, 999)]
    wc = working_capital(rows, Decimal(1000), 0.05)
    assert wc["sign_status"] == "부호 미확인" and wc["ocf_direction"].startswith("미확인")
    assert wc["caution"].startswith("운전자본 변동 규모 주의")  # 규모는 절대값으로 판단 (10%)


def test_missing_subtotal_is_not_rebuilt_from_balance_sheet():
    wc = working_capital([cf(NI_ID, 50)], Decimal(1000), 0.05)
    assert wc["wc_status"] == "누락" and wc["wc_amount"] is None and wc["caution"].startswith("확인 불가")


# ---------------------------------------------------------------- 비교 가능성


def test_normal_calculation_and_direct_comparability_are_separate():
    assert comparability("normal", []) == COMPARABLE
    assert comparability("normal", ["연결 범위 변화"]) == CAUTION
    assert comparability("reference", []) == HOLD_REFERENCE
    assert comparability("not_computed", []) == NOT_COMPARABLE


def test_reclassification_flags_year_and_following_year_for_affected_metrics():
    reviews = pd.DataFrame([{"review_id": "M01", "corp_code": "C", "fs_div": "CFS", "canonical_account": "current_liabilities",
                             "period_end": "2023-12-31", "cause": "재분류", "cause_detail": "기준서 개정"}])
    causes = {"재분류": "분류 기준 변경 — 단순 증감 해석 주의"}
    for y, expected in ((2022, 0), (2023, 1), (2024, 1), (2025, 0)):
        assert len(review_reasons(reviews, causes, "C", "CFS", "current_ratio_reported", y)) == expected
    assert review_reasons(reviews, causes, "C", "CFS", "operating_margin", 2023) == []
    assert review_reasons(reviews, causes, "C", "OFS", "current_ratio_reported", 2023) == []


def test_scope_changes_flag_consolidated_years_only():
    scope = pd.DataFrame([{"corp_code": "C", "fs_div": "CFS", "bsns_year": "2023", "change_type": "신규연결", "entity": "A", "reason": "설립"},
                          {"corp_code": "C", "fs_div": "CFS", "bsns_year": "2024", "change_type": "변동 없음", "entity": "-", "reason": "-"}])
    assert "영향 규모 미확인" in scope_reasons(scope, "C", "CFS", 2023)[0]
    assert scope_reasons(scope, "C", "CFS", 2024) == []
    assert scope_reasons(scope, "C", "OFS", 2023) == []
