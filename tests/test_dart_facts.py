"""관측값 테이블 규칙(docs/decisions.md D5) 테스트."""

import io
import zipfile

import pytest

from src.normalization.selection import latest_valid_filing, select_snapshot
from src.normalization.dart_facts import (
    fiscal_periods_from_filing, flatten_snapshot, mismatch_kind, parse_amount,
    same_period_mismatches,
)

RCEPT = "20260318000694"
PERIODS = {RCEPT: {12: ("2025-01-01", "2025-12-31"), 11: ("2024-01-01", "2024-12-31"), 10: ("2023-01-01", "2023-12-31")}}
FILINGS = {RCEPT: {"link": "stored", "rcept_dt": "20260318", "report_nm": "사업보고서 (2025.12)"}}
META = {"request": {"corp_code": "01009789", "bsns_year": "2025", "reprt_code": "11011", "fs_div": "CFS"},
        "collected_at": "2026-10-07T04:06:21+00:00"}


def item(sj_div="CIS", account_id="ifrs-full_Revenue", nm="수익(매출액)", detail="-", th="100", fr="90", bf="80", rcept=RCEPT):
    return {"rcept_no": rcept, "sj_div": sj_div, "sj_nm": "", "account_id": account_id, "account_nm": nm, "account_detail": detail,
            "thstrm_nm": "제 12 기", "thstrm_amount": th, "frmtrm_nm": "제 11 기", "frmtrm_amount": fr,
            "bfefrmtrm_nm": "제 10 기", "bfefrmtrm_amount": bf, "ord": "1", "currency": "KRW"}


def flatten(items, snapshot="s1", periods=PERIODS):
    return flatten_snapshot(snapshot, {"status": "000", "list": items}, META, FILINGS, periods)


# ---------------------------------------------------------------- 값 상태


def test_zero_empty_and_unparseable_are_different():
    assert parse_amount("0") == (0, "parsed")
    assert parse_amount("-1,234") == (-1234, "parsed")
    assert parse_amount("") == (None, "empty_in_source")
    assert parse_amount("-") == (None, "empty_in_source")
    assert parse_amount("12억") == (None, "parse_failed")


# ---------------------------------------------------------------- 기간


def test_prior_year_column_maps_to_prior_period_not_report_year():
    rows = flatten([item()])
    by_col = {r["source_column"]: r for r in rows}
    assert {r["report_bsns_year"] for r in rows} == {2025}  # 보고서 사업연도는 모두 2025
    assert (by_col["당기"]["period_start"], by_col["당기"]["period_end"]) == ("2025-01-01", "2025-12-31")
    assert (by_col["전기"]["period_start"], by_col["전기"]["period_end"]) == ("2024-01-01", "2024-12-31")
    assert by_col["전기"]["period_type"] == "duration"


def test_balance_sheet_values_are_instants():
    rows = flatten([item(sj_div="BS", account_id="ifrs-full_Assets", nm="자산총계")])
    current = next(r for r in rows if r["source_column"] == "당기")
    assert current["period_type"] == "instant"
    assert current["period_start"] is None
    assert current["period_end"] == "2025-12-31"


def test_unknown_period_is_marked_not_guessed():
    rows = flatten([item()], periods={RCEPT: {12: ("2025-01-01", "2025-12-31")}})  # 제 11·10 기는 원문에서 못 찾음
    prior = next(r for r in rows if r["source_column"] == "전기")
    assert prior["period_status"] == "review_needed:period_not_found"
    assert prior["period_end"] is None


def test_equity_statement_period_type_is_left_for_review():
    rows = flatten([item(sj_div="SCE", account_id="ifrs-full_Equity", nm="기말자본")])
    assert {r["period_status"] for r in rows} == {"review_needed:period_type"}


def test_fiscal_periods_are_read_from_filing_text(tmp_path):
    # 위: 문단으로 적힌 정상 기간. 아래: 당기·전기 열이 나란히 있는 표 — 셀 경계를 넘어 날짜가 붙으면 안 된다
    text = ("<P>연결 포괄손익계산서 제 12 기 2025.01.01 부터 2025.12.31 까지 제 11 기 2024.01.01 부터 2024.12.31 까지</P>"
            "<TR><TD>제12기 2025년 1월 1일부터</TD><TD>제11기 2024년 1월 1일부터</TD></TR>"
            "<TR><TD>2025년 12월 31일까지</TD><TD>2024년 12월 31일까지</TD></TR>")
    path = tmp_path / "original.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(f"{RCEPT}.xml", text)
    assert fiscal_periods_from_filing(path) == {12: ("2025-01-01", "2025-12-31"), 11: ("2024-01-01", "2024-12-31")}


def test_reversed_dates_are_dropped_but_long_periods_are_kept(tmp_path):
    text = ("<P>제 12 기 2025.12.31 부터 2025.01.01 까지</P>"  # 역순 — 오류
            "<P>제 11 기 2023.07.01 부터 2024.12.31 까지</P>")  # 18개월 — 결산기 변경일 수 있어 보존
    path = tmp_path / "original.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(f"{RCEPT}.xml", text)
    periods = fiscal_periods_from_filing(path)
    assert 12 not in periods
    assert periods[11] == ("2023-07-01", "2024-12-31")


def test_long_period_is_preserved_and_marked_for_review():
    rows = flatten([item()], periods={RCEPT: {12: ("2025-01-01", "2025-12-31"), 11: ("2023-07-01", "2024-12-31"), 10: ("2022-07-01", "2023-06-30")}})
    prior = next(r for r in rows if r["source_column"] == "전기")
    assert (prior["period_start"], prior["period_end"]) == ("2023-07-01", "2024-12-31")
    assert prior["period_status"] == "review_needed:unusual_length"
    assert prior["period_is_annual"] is False


# ---------------------------------------------------------------- 행 식별


def test_same_account_id_in_different_rows_is_not_merged():
    rows = flatten([
        item(sj_div="CF", account_id="-", nm="이자의 지급"),
        item(sj_div="CF", account_id="-", nm="배당금의 지급"),
        item(sj_div="CIS", account_id="ifrs-full_Revenue"),
        item(sj_div="SCE", account_id="ifrs-full_Equity", detail="자본 [구성요소]|이익잉여금 [구성요소]"),
        item(sj_div="SCE", account_id="ifrs-full_Equity", detail="자본 [구성요소]|자본금 [구성요소]"),
    ])
    assert len(rows) == 5 * 3
    assert len({r["row_id"] for r in rows}) == 15  # 스냅샷 + 행 위치 + 금액 열
    assert {r["account_detail"] for r in rows if r["sj_div"] == "SCE"} == {"자본 [구성요소]|이익잉여금 [구성요소]", "자본 [구성요소]|자본금 [구성요소]"}


def test_missing_filing_link_is_recorded():
    rows = flatten([item(rcept="20990101000001")])
    assert {r["filing_link"] for r in rows} == {"missing"}


# ---------------------------------------------------------------- 동일 기간 값 불일치


def report_2024(th, fr="80"):  # 2023 값은 2025 보고서 전전기(80)와 같게
    """2024 보고서: 당기(제 11 기)=2024, 전기(제 10 기)=2023"""
    rc = "20250319000077"
    i = item(th=th, fr=fr, bf="", rcept=rc)
    i.update({"thstrm_nm": "제 11 기", "frmtrm_nm": "제 10 기"})
    periods = {rc: {11: ("2024-01-01", "2024-12-31"), 10: ("2023-01-01", "2023-12-31")}}
    return flatten_snapshot("s0", {"status": "000", "list": [i]}, {**META, "request": {**META["request"], "bsns_year": "2024"}},
                            {rc: {"link": "stored"}}, periods)


def test_same_values_across_reports_are_not_flagged():
    rows = flatten([item(fr="90")]) + report_2024(th="90")
    result = same_period_mismatches(rows)
    assert result["compared_groups"] >= 1
    assert result["mismatched"] == {}


def test_different_values_are_flagged_with_kind_not_called_restatement():
    rows = flatten([item(fr="90")]) + report_2024(th="-90")
    result = same_period_mismatches(rows)
    (key, group), = result["mismatched"].items()
    assert key[-2:] == ("2024-12-31", "KRW")  # 기간 + 통화
    assert mismatch_kind(group) == {"kind": "sign_only", "label_changed": False, "nonstandard_key": False}

    rows = flatten([item(fr="90")]) + report_2024(th="95")
    (_, group), = same_period_mismatches(rows)["mismatched"].items()
    assert mismatch_kind(group)["kind"] == "magnitude"


# ---------------------------------------------------------------- 값 선택용 보고서 버전


PUBLISHED = {
    "a": {"rcept_no": "a", "corp_code": "c", "bsns_year": 2024, "rcept_dt": "20250320", "correction": None},
    "b": {"rcept_no": "b", "corp_code": "c", "bsns_year": 2024, "rcept_dt": "20250814", "correction": "기재정정"},
    "c": {"rcept_no": "c", "corp_code": "c", "bsns_year": 2024, "rcept_dt": "20260813", "correction": "기재정정"},
    "d": {"rcept_no": "d", "corp_code": "c", "bsns_year": 2024, "rcept_dt": "20260901", "correction": "첨부정정"},
}


def test_latest_valid_filing_respects_as_of_and_skips_attachment_corrections():
    assert latest_valid_filing(PUBLISHED, "c", 2024, "20261007") == "c"  # 첨부정정(d)은 제외
    assert latest_valid_filing(PUBLISHED, "c", 2024, "20260630") == "b"  # 기준일 이후 정정(c)은 모른다
    assert latest_valid_filing(PUBLISHED, "c", 2024, "20250101") is None  # 아직 공개 전 → 보류


def snap(run, rcept, status="000"):
    return {"snapshot_id": f"c/api/fnlttSinglAcntAll/2024/11011/CFS/{run}", "corp_code": "c", "fs_div": "CFS", "bsns_year": "2024",
            "status": status, "rcept_nos": [rcept] if status == "000" else []}


def test_unstored_latest_filing_is_held_not_replaced_by_older_one():
    # 검토 지적 ③: 최신 기재정정본(c) 원문을 못 받았으면 이전 저장본(b)으로 내려가지 않는다
    filings = {"a": {"link": "stored"}, "b": {"link": "stored"}, "c": {"link": "attempt_only"}}
    sel = select_snapshot([snap("run1", "b")], PUBLISHED, filings, "c", "CFS", 2024, "20261007")
    assert (sel.status, sel.rcept_no) == ("held", "c")


@pytest.mark.parametrize("order", [0, 1])
def test_snapshot_choice_does_not_depend_on_row_order(order):
    # 검토 지적 ②: 과거 버전 스냅샷과 최신 버전 스냅샷이 섞여 있어도 선택 결과는 같아야 한다
    filings = {k: {"link": "stored"} for k in "abc"}
    snaps = [snap("20250901T000000Z", "b"), snap("20261001T000000Z", "c")]
    if order:
        snaps.reverse()
    sel = select_snapshot(snaps, PUBLISHED, filings, "c", "CFS", 2024, "20261007")
    assert (sel.status, sel.snapshot_id.rsplit("/", 1)[-1]) == ("selected", "20261001T000000Z")


def test_repeated_collection_of_same_report_uses_latest_run():
    filings = {"c": {"link": "stored"}}
    snaps = [snap("20261001T000000Z", "c"), snap("20261005T000000Z", "c")]
    sel = select_snapshot(list(reversed(snaps)), PUBLISHED, filings, "c", "CFS", 2024, "20261007")
    assert sel.snapshot_id.endswith("20261005T000000Z")
