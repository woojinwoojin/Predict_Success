"""수기 확인표 규칙(docs/decisions.md D7) 테스트."""

import pytest

from src.normalization.manual import apply_confirmations, load_confirmations

KEY = {"corp_code": "01009789", "fs_div": "CFS"}
REVIEW = {"canonical_account": "interest_expense", "label": "이자비용", "status": "review_needed", "value": None, "note": "본문에 없음"}


def entry(**kw):
    base = {"confirmation_id": "C1", "corp_code": "01009789", "fs_div": "CFS", "canonical_account": "interest_expense",
            "period_type": "duration", "period_start": "2025-01-01", "period_end": "2025-12-31", "value": "46,717,900", "unit": "천원",
            "unit_multiplier": "1000", "rcept_no": "R2025", "source_location": "주석 29", "components": "", "status": "confirmed",
            "extraction_method": "manual", "amount_check": "확인", "scope_check": "", "component_check": "해당 없음", "method": "test", "api_relation": ""}
    return {**base, **kw}


def test_confirmed_note_value_fills_review_and_converts_unit():
    result = apply_confirmations(REVIEW, [entry()], KEY, "R2025", "2025-12-31")
    assert result["status"] == "mapped"
    assert result["value"] == "46717900000"  # 천원 × 1000 → 원
    assert result["extraction_method"] == "manual"
    assert "주석 29" in result["note"]


def test_value_from_another_report_version_is_not_used():
    result = apply_confirmations(REVIEW, [entry(rcept_no="R2025_OLD")], KEY, "R2025", "2025-12-31")
    assert result["status"] == "review_needed"
    assert "다른 보고서 버전" in result["note"]


def test_unconfirmed_or_other_period_entries_are_ignored():
    assert apply_confirmations(REVIEW, [entry(status="needs_review")], KEY, "R2025", "2025-12-31")["status"] == "review_needed"
    assert apply_confirmations(REVIEW, [entry(period_end="2024-12-31")], KEY, "R2025", "2025-12-31")["status"] == "review_needed"


def test_manual_value_that_disagrees_with_api_value_is_flagged():
    mapped = {**REVIEW, "status": "mapped", "value": "1"}
    result = apply_confirmations(mapped, [entry()], KEY, "R2025", "2025-12-31")
    assert result["status"] == "review_needed"
    assert "다름" in result["note"]


def test_api_mapping_is_labelled_as_api_rule():
    mapped = {**REVIEW, "status": "mapped", "value": "1"}
    assert apply_confirmations(mapped, [], KEY, "R2025", "2025-12-31")["extraction_method"] == "api_rule"


def write(tmp_path, rows):
    path = tmp_path / "note_confirmations.csv"
    header = list(entry().keys())
    lines = [",".join(header)] + [",".join(f'"{r[h]}"' for h in header) for r in rows]
    path.write_text("\n".join(lines), encoding="utf-8-sig")
    return path


def test_confirmed_entry_needs_a_source_location(tmp_path):
    with pytest.raises(ValueError, match="원문 위치"):
        load_confirmations(write(tmp_path, [entry(source_location="")]))


def test_only_manual_extraction_method_is_accepted(tmp_path):
    with pytest.raises(ValueError, match="manual"):
        load_confirmations(write(tmp_path, [entry(extraction_method="api_rule")]))


def test_scope_unconfirmed_note_value_is_only_a_candidate():
    with_basis = {**REVIEW, "basis": "리스 이자 포함"}
    result = apply_confirmations(with_basis, [entry(amount_check="확인", scope_check="미확인")], KEY, "R2025", "2025-12-31")
    assert result["status"] == "candidate"  # 계산용 후보값은 주되 점수 반영은 보류
    assert result["value"] == "46717900000"
    result = apply_confirmations(with_basis, [entry(amount_check="확인", scope_check="확인")], KEY, "R2025", "2025-12-31")
    assert result["status"] == "mapped"


def test_partial_components_never_make_a_normal_debt_total():
    debt = {"canonical_account": "short_term_interest_bearing_debt", "label": "단기 이자부채", "basis": "리스 포함",
            "status": "review_needed", "value": None, "note": ""}
    partial = entry(canonical_account="short_term_interest_bearing_debt", period_end="2025-12-31", value="100", unit="원", unit_multiplier="1",
                    scope_check="확인", component_check="부분")
    assert apply_confirmations(debt, [partial], KEY, "R2025", "2025-12-31")["status"] == "candidate"
    complete = {**partial, "component_check": "확인"}
    assert apply_confirmations(debt, [complete], KEY, "R2025", "2025-12-31")["status"] == "mapped"


def test_unconfirmed_amount_is_not_used():
    assert apply_confirmations(REVIEW, [entry(amount_check="미확인")], KEY, "R2025", "2025-12-31")["status"] == "review_needed"


def test_manual_total_replaces_api_total_only_when_marked_and_components_confirmed():
    api = {"canonical_account": "short_term_interest_bearing_debt", "label": "단기 이자부채", "basis": "리스 포함",
           "status": "mapped", "value": "5000", "note": ""}
    fuller = entry(canonical_account="short_term_interest_bearing_debt", value="6364", unit="원", unit_multiplier="1",
                   scope_check="확인", component_check="확인")
    assert apply_confirmations(api, [fuller], KEY, "R2025", "2025-12-31")["status"] == "review_needed"  # 사유 없이 다르면 확인 필요
    marked = {**fuller, "api_relation": "replaces_incomplete_api"}
    result = apply_confirmations(api, [marked], KEY, "R2025", "2025-12-31")
    assert (result["status"], result["value"]) == ("mapped", "6364")


@pytest.mark.parametrize("unchecked", [{"amount_check": "미확인"}, {"scope_check": "미확인"}, {"component_check": "부분"}])
def test_api_replacement_path_cannot_skip_common_checks(unchecked):
    # 검토 지적 ①: 대체 조건을 만족해도 금액·범위·구성 확인을 건너뛰면 안 된다
    api = {"canonical_account": "short_term_interest_bearing_debt", "label": "단기 이자부채", "basis": "리스 포함",
           "status": "mapped", "value": "5000", "note": ""}
    e = entry(canonical_account="short_term_interest_bearing_debt", value="6364", unit="원", unit_multiplier="1",
              scope_check="확인", component_check="확인", api_relation="replaces_incomplete_api", **{})
    e.update(unchecked)
    result = apply_confirmations(api, [e], KEY, "R2025", "2025-12-31")
    assert result["status"] != "mapped"


def test_manual_value_that_differs_from_api_candidate_is_also_checked():
    api = {"canonical_account": "short_term_interest_bearing_debt", "label": "단기 이자부채", "basis": "리스 포함",
           "status": "candidate", "value": "5000", "note": ""}
    e = entry(canonical_account="short_term_interest_bearing_debt", value="7000", unit="원", unit_multiplier="1",
              scope_check="확인", component_check="확인")
    assert apply_confirmations(api, [e], KEY, "R2025", "2025-12-31")["status"] == "review_needed"
