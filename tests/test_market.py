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
    # 산술 일치만으로는 '직접 비교 가능'이 아니다 — 판정이 없으면 연속성 미확인
    assert by.loc[2022, "comparability"] == "비교 주의" and by.loc[2023, "comparability"] == "연속성 미확인"
    assert by.loc[2023, "definition_continuity"] == "판정 없음" and by.loc[2021, "comparability"] == "기초값"
    assert by.loc[2021, "context_업체수"] == Decimal("9359")
    s = summary.set_index("span")
    assert s.loc["2022→2023", "caution"] == ""  # 주의 구간이 기간 밖
    assert "원인 미확인" in s.loc["2021→2023", "caution"]
    assert abs(s.loc["2022→2023", "cagr"] - (14510243 / 13590776 - 1)) < 1e-12


def test_continuity_judgment_flows_to_year_rows_and_spans():
    judgments = pd.DataFrame([
        {"judgment_id": "J03", "question": "Q3 연속성 — 2021→2022", "status": "미확인", "claim_years": "2021;2022"},
        {"judgment_id": "J04", "question": "Q3 연속성 — 2022→2023", "status": "미확인", "claim_years": "2022;2023"}])
    cont = run_market.continuity_by_pair(judgments)
    stats, summary = run_market.build_stats(parse_table(table_html(VALUES, GROWTH)), CONFIG, Path("run1"), META, cont)
    by = stats.set_index("year")
    # 2026-10-20 검토 반례: J04 미확인인데 2023이 '직접 비교 가능'으로 나오던 경로
    assert (by.loc[2023, "yoy_check"], by.loc[2023, "definition_continuity"], by.loc[2023, "comparability"]) == ("일치", "미확인 (J04)", "연속성 미확인")
    assert summary.set_index("span").loc["2021→2023", "definition_continuity"] == "미확인 (J03); 미확인 (J04)"
    judgments.loc[1, "status"] = "연속성 확인"
    stats, _ = run_market.build_stats(parse_table(table_html(VALUES, GROWTH)), CONFIG, Path("run1"), META, run_market.continuity_by_pair(judgments))
    assert stats.set_index("year").loc[2023, "comparability"] == "직접 비교 가능"


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


# ---------------------------------------------------------------- 유형별 생산실적 (D19)
from src.market.kosis_csv import parse_kosis_csv  # noqa: E402

TYPE_CSV = ('"유형별(1)",2021,2022,2023\n"합계",166533,135908,145102\n"기초화장용 제품류",101789,75220,79455\n'
            '"색조화장용 제품류",64744,60688,65648\n').encode("cp949")
TYPE_CONFIG = {**CONFIG, "type_statistic": {"candidate_id": "M02", "total_label": "합계", "unit": "억원"}}
TYPE_META = {"file": {"sha256": "def"}, "page_dates": {"updated": "2026-09-10"}}
M01_ROWS = pd.DataFrame({"year": [2021, 2022, 2023], "value": [Decimal("16653313"), Decimal("13590776"), Decimal("14510243")]})


def test_parse_kosis_csv_reads_cp949_rows_by_year():
    t = parse_kosis_csv(TYPE_CSV, "cp949")
    assert t["years"] == [2021, 2022, 2023] and t["rows"][0]["label"] == "합계"
    assert t["rows"][1]["values"][2022] == Decimal("75220")


def test_types_check_sum_and_total_against_m01_with_rounding_bound():
    types, summary = run_market.build_types(parse_kosis_csv(TYPE_CSV, "cp949"), TYPE_CONFIG, M01_ROWS, Path("r"), TYPE_META)
    total = types[types.type == "합계"].set_index("year")
    assert total.loc[2023, "type_sum_check"] == "반올림 차이 +1억원" and total.loc[2022, "type_sum_check"] == "일치"
    base = types[types.type == "기초화장용 제품류"].set_index("year")
    assert base.loc[2022, "change_vs_prior"] == Decimal("-26569") and base.loc[2022, "comparability"] == "비교 주의"
    assert set(summary.series) == {"합계", "기초화장용 제품류", "색조화장용 제품류"}


def test_types_stop_when_total_differs_from_m01_or_sum_beyond_rounding():
    m01 = M01_ROWS.assign(value=[Decimal("16653313"), Decimal("13600000"), Decimal("14510243")])
    with pytest.raises(ValueError, match="M01"):
        run_market.build_types(parse_kosis_csv(TYPE_CSV, "cp949"), TYPE_CONFIG, m01, Path("r"), TYPE_META)
    bad = TYPE_CSV.decode("cp949").replace("64744", "64700").encode("cp949")
    with pytest.raises(ValueError, match="유형 합"):
        run_market.build_types(parse_kosis_csv(bad, "cp949"), TYPE_CONFIG, M01_ROWS, Path("r"), TYPE_META)


def test_register_manual_file_moves_file_and_records_hash(tmp_path, monkeypatch):
    import json
    from src.collectors import stats
    monkeypatch.setattr(stats, "RAW", tmp_path / "raw")
    src = tmp_path / "다운로드.csv"
    src.write_bytes(TYPE_CSV)
    out = stats.register_manual_file(src, "kosis", "T1", {"encoding": "cp949"})
    meta = json.loads((out / "metadata.json").read_text(encoding="utf-8"))
    assert not src.exists() and (out / "original.csv").read_bytes() == TYPE_CSV
    assert meta["original_name"] == "다운로드.csv" and meta["file"]["sha256"] == stats.sha256(TYPE_CSV)


# ---------------------------------------------------------------- 작성지침 확인 (D20)
import hashlib  # noqa: E402


def guideline_tables(tmp_path):
    doc = tmp_path / "data" / "raw" / "g.pdf"
    doc.parent.mkdir(parents=True)
    doc.write_bytes(b"%PDF guide")
    digest = hashlib.sha256(b"%PDF guide").hexdigest()
    return {
        "stat_guideline_docs.csv": pd.DataFrame([{"doc_id": "G1", "status": "수집"}, {"doc_id": "G9", "status": "미확보"}]),
        "stat_guideline_facts.csv": pd.DataFrame([
            {"fact_id": "S1", "doc_id": "G1", "evidence_level": "명시적 확인", "raw_path": "data/raw/g.pdf", "sha256": digest, "fact_years": "2022"},
            {"fact_id": "S2", "doc_id": "G9", "evidence_level": "미확인", "raw_path": "", "sha256": "", "fact_years": ""}]),
        "stat_guideline_judgments.csv": pd.DataFrame([
            {"judgment_id": "J1", "question": "Q1 가격 기준", "status": "부분 확인", "claim_years": "2021;2022", "evidence": "S1", "open_evidence": "S2"},
            {"judgment_id": "J2", "question": "Q3 연속성", "status": "미확인", "claim_years": "2021;2022", "evidence": "", "open_evidence": "S2"}]),
    }


def run_guidelines(tables, tmp_path, monkeypatch):
    monkeypatch.setattr(run_market, "ROOT", tmp_path)
    monkeypatch.setattr(run_market, "read_manual", lambda name: tables[name].copy())
    return run_market.build_guidelines()


def test_guidelines_pass_when_raw_hash_and_evidence_match(tmp_path, monkeypatch):
    facts, judgments = run_guidelines(guideline_tables(tmp_path), tmp_path, monkeypatch)
    assert list(judgments.status) == ["부분 확인", "미확인"]


def test_full_confirmation_passes_only_for_explicitly_covered_years(tmp_path, monkeypatch):
    tables = guideline_tables(tmp_path)
    tables["stat_guideline_judgments.csv"].loc[0, ["status", "claim_years"]] = ["확인", "2022"]
    assert run_guidelines(tables, tmp_path, monkeypatch)[1].status.iloc[0] == "확인"


@pytest.mark.parametrize("change, message", [
    (lambda t: t["stat_guideline_facts.csv"].loc.__setitem__((0, "sha256"), "0" * 64), "해시 불일치"),
    (lambda t: t["stat_guideline_facts.csv"].loc.__setitem__((0, "doc_id"), "G7"), "등록부에 없는 문서"),
    # 미확보 문서만 가리키는 사실을 확인으로 쓰지 않는다
    (lambda t: t["stat_guideline_facts.csv"].loc.__setitem__((1, "evidence_level"), "간접 근거"), "보존 원본 없이"),
    (lambda t: t["stat_guideline_facts.csv"].loc.__setitem__((0, "evidence_level"), "확인"), "근거 수준"),
    # Q3에는 '확인'이 아니라 변경 확인 / 연속성 확인 / 미확인만 쓴다
    (lambda t: t["stat_guideline_judgments.csv"].loc.__setitem__((1, "status"), "확인"), "쓸 수 없는 상태"),
    (lambda t: t["stat_guideline_judgments.csv"].loc.__setitem__((0, "evidence"), "S1;S99"), "S99"),
    # 승격 검사 (2026-10-20 검토 반례): 근거가 모두 미확인인데 '연속성 확인'
    (lambda t: t["stat_guideline_judgments.csv"].loc.__setitem__((1, "status"), "연속성 확인"), "확인 근거 없음"),
    (lambda t: t["stat_guideline_judgments.csv"].loc.__setitem__((1, "evidence"), "S2"), "확인 근거에 미확인 사실"),
    # '확인'은 주장 연도마다 그해 명시 근거가 필요 — 2022 문서로 2021까지 확인하지 않는다
    (lambda t: t["stat_guideline_judgments.csv"].loc.__setitem__((0, "status"), "확인"), r"명시 확인 근거가 없는 연도 \['2021'\]"),
])
def test_guidelines_stop_on_broken_evidence(tmp_path, monkeypatch, change, message):
    tables = guideline_tables(tmp_path)
    change(tables)
    with pytest.raises(ValueError, match=message):
        run_guidelines(tables, tmp_path, monkeypatch)


def test_manual_guideline_records_keep_scores_off_and_q3_unconfirmed():
    root = Path(__file__).resolve().parents[1]
    read = lambda name: pd.read_csv(root / "data" / "manual" / name, dtype=str, keep_default_na=False, encoding="utf-8-sig")  # noqa: E731
    links, judgments = read("market_links.csv"), read("stat_guideline_judgments.csv")
    # 작성지침 확인만으로 점수를 활성화하지 않는다 (11-3 완료 조건은 정의 확인·적용 범위까지)
    assert set(links.score_use) == {"미반영"} and links.score_eligibility.str.startswith("보류").all()
    assert (links.guideline_evidence != "").all()
    q3 = judgments[judgments.question.str.startswith("Q3")]
    assert set(q3.status) <= JUDGMENT_Q3 and len(q3) == 2


JUDGMENT_Q3 = run_market.JUDGMENT_STATUS["Q3"]


@pytest.mark.skipif(not (Path(__file__).resolve().parents[1] / "data" / "raw" / "stats" / "guidelines").exists(), reason="보존 원본 없음 (data/raw는 git 제외)")
def test_manual_guideline_records_match_preserved_raw():
    facts, judgments = run_market.build_guidelines()
    assert len(facts) >= 20 and set(judgments.judgment_id) >= {"J01", "J02", "J03", "J04", "J05"}


# ---------------------------------------------------------------- 시장 원본 고정 (D21)
def write_run(root, run_id, content=b"<table/>"):
    import json
    d = root / "src_stats" / run_id
    d.mkdir(parents=True)
    (d / "table.html").write_bytes(content)
    (d / "metadata.json").write_text(json.dumps({"files": {"table.html": {"sha256": hashlib.sha256(content).hexdigest()}}}), encoding="utf-8")
    return d


def test_pinned_raw_uses_the_named_run_not_the_latest(tmp_path, monkeypatch):
    monkeypatch.setattr(run_market, "ROOT", tmp_path)
    write_run(tmp_path, "20261001T000000Z")
    write_run(tmp_path, "20261009T000000Z")
    raw_dir, _ = run_market.pinned_raw("src_stats", "20261001T000000Z")
    assert raw_dir.name == "20261001T000000Z"
    with pytest.raises(ValueError, match="raw_run 미지정"):
        run_market.pinned_raw("src_stats", None)
    with pytest.raises(FileNotFoundError):
        run_market.pinned_raw("src_stats", "20261005T000000Z")


def test_pinned_raw_stops_when_file_changed_after_collection(tmp_path, monkeypatch):
    monkeypatch.setattr(run_market, "ROOT", tmp_path)
    d = write_run(tmp_path, "20261001T000000Z")
    (d / "table.html").write_bytes(b"<table>changed</table>")
    with pytest.raises(ValueError, match="원본 해시 불일치"):
        run_market.pinned_raw("src_stats", "20261001T000000Z")


def test_market_config_pins_both_raw_runs():
    import yaml
    config = yaml.safe_load((Path(__file__).resolve().parents[1] / "configs" / "market_v1.yaml").read_text(encoding="utf-8"))
    assert config["base_statistic"]["raw_run"] and config["type_statistic"]["raw_run"]
