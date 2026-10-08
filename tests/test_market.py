"""시장 통계 연결 v1 (docs/decisions.md D18) 테스트."""

from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from src.collectors.stats import page_dates
from src.market import run_market
from src.market.index_table import parse_table


def table_html(values: dict, growth: dict) -> str:
    def row(i, label, data):
        cells = "".join(f"<td id='td_1_{n}' data-id='td_1_{n}'item-id='{y}Y' item-num={n}>{v}</td>" for n, (y, v) in enumerate(data.items(), 1))
        return f"<tr id='tr_304301_{i}'>\n<th class='tl' item-id='T0{i}'>{label}</th>\n{cells}\n</tr>"
    return ('<p id="sttsCdUnit">[단위 : 개소, %, 백만원]</p><table><tbody>'
            + row(1, "업체수", {2021: "9,359", 2022: "10,119"}) + row(2, "증감률", {2021: "4.7", 2022: "7.5"})
            + row(3, "생산금액", values) + row(4, "전년대비&nbsp;성장률", growth)
            + '</tbody></table><span class="tit bold" id="srcTitle">출처:</span> 식품의약품안전처 '
              '<span id="srcTitle">통계공표시기:</span> 작성기준 년도 익년 12월 <span>주석:</span> * 업체수는 보고 업체만 관련 KOSIS 통계보기')


VALUES = {2020: "15,161,717", 2021: "16,653,313", 2022: "13,590,776", 2023: "14,510,243"}
GROWTH = {2020: "-6.8", 2021: "9.8", 2022: "-18.4", 2023: "6.8"}
CONFIG = {"score_use": "미반영", "years": [2021, 2022, 2023],
          "base_statistic": {"candidate_id": "M01", "series": "생산금액", "context_series": ["업체수"], "label": "국내 화장품 생산금액"},
          "cagr_spans": [[2022, 2023], [2021, 2023]],
          "caution_pairs": [{"pair": [2021, 2022], "text": "2021→2022 큰 감소 — 원인 미확인"}]}
META = {"files": {"table.html": {"sha256": "abc"}}, "page_dates": {"updated": "2026-09-03"}}


def test_parse_table_reads_rows_years_unit_and_footer_without_rounding():
    t = parse_table(table_html(VALUES, GROWTH))
    assert t["unit"] == "개소, %, 백만원" and t["source"] == "식품의약품안전처" and t["release_rule"] == "작성기준 년도 익년 12월"
    rows = {r["label"]: r for r in t["rows"]}
    assert rows["생산금액"]["values"][2022] == Decimal("13590776")
    assert rows["전년대비 성장률"]["of"] == "생산금액" and rows["증감률"]["of"] == "업체수"


def test_page_dates_reads_updated_and_next_update_through_entities():
    assert page_dates("<li>최근 갱신일 : &nbsp; 2026-09-03(입력예정일 : 2027-08-30)</li>") == {"updated": "2026-09-03", "next_update": "2027-08-30"}


def test_stats_check_computed_growth_against_official_and_mark_caution_pair():
    stats, summary = run_market.build_stats(parse_table(table_html(VALUES, GROWTH)), CONFIG, Path("run1"), META)
    by = stats.set_index("year")
    assert list(by.yoy_check) == ["일치", "일치", "일치"]
    assert by.loc[2022, "comparability"] == "비교 주의" and by.loc[2023, "comparability"] == "직접 비교 가능"
    assert by.loc[2021, "context_업체수"] == Decimal("9359")
    s = summary.set_index("span")
    assert s.loc["2022→2023", "caution"] == ""  # 주의 구간이 기간 밖
    assert "원인 미확인" in s.loc["2021→2023", "caution"]
    assert abs(s.loc["2022→2023", "cagr"] - (14510243 / 13590776 - 1)) < 1e-12


def test_official_growth_mismatch_is_reported():
    stats, _ = run_market.build_stats(parse_table(table_html(VALUES, {**GROWTH, 2023: "7.0"})), CONFIG, Path("run1"), META)
    assert stats.set_index("year").loc[2023, "yoy_check"].startswith("불일치")


def test_missing_or_nonpositive_values_are_not_filled():
    assert run_market.growth(None, Decimal(5)) is None and run_market.growth(Decimal(0), Decimal(5)) is None
    assert run_market.cagr(Decimal(0), Decimal(5), 3) is None and run_market.cagr(Decimal(1), None, 3) is None


def test_links_must_point_to_recorded_facts(monkeypatch):
    tables = {
        "market_links.csv": pd.DataFrame([{"link_id": "L1", "corp_code": "C", "company": "회사", "candidate_id": "M01", "judgement": "조건부 참고",
                                           "differences": "", "evidence": "B01;B99"}]),
        "market_stat_candidates.csv": pd.DataFrame([{"candidate_id": "M01", "name": "생산금액", "status": "값 수집"}]),
        "company_scope_facts.csv": pd.DataFrame([{"fact_id": "B01", "topic": "연결 범위", "fact": "종속회사 없음", "rcept_no": "R"}]),
    }
    monkeypatch.setattr(run_market, "read_manual", lambda name: tables[name].copy())
    with pytest.raises(ValueError, match="B99"):
        run_market.build_links()
    tables["market_links.csv"].loc[0, "evidence"] = "B01"
    assert "B01 연결 범위: 종속회사 없음" in run_market.build_links().evidence_detail.iloc[0]


def test_manual_links_use_only_defined_judgements_and_are_not_scored():
    root = Path(__file__).resolve().parents[1]
    links = pd.read_csv(root / "data" / "manual" / "market_links.csv", dtype=str, keep_default_na=False, encoding="utf-8-sig")
    assert set(links.judgement) <= {"비교 가능", "조건부 참고", "연결 보류"}
    assert set(links.score_use) == {"미반영"}
