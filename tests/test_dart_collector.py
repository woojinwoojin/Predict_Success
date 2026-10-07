"""원본 저장 규칙(docs/decisions.md D4) 테스트. 실제 API 대신 가짜 응답을 쓴다."""

import io
import json
import zipfile

import pytest
import requests

from src.collectors.dart import (
    DartClient, DartError, RawStore, fetch_filing, fetch_financials, is_valid_zip, list_annual_reports,
    parse_annual_report_name,
)

KEY = "testkey0123456789abcdef"


class FakeResponse:
    def __init__(self, content: bytes, status_code: int = 200):
        self.content = content
        self.status_code = status_code

    def json(self):
        return json.loads(self.content)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} for url: https://x/api?crtfc_key={KEY}")


class FakeSession:
    """endpoint 이름 → 응답 목록 (호출 순서대로 꺼낸다). 요청 기록도 남긴다."""

    def __init__(self, responses: dict[str, list]):
        self.responses = responses
        self.calls = []

    def get(self, url, params, timeout):
        endpoint = url.rsplit("/", 1)[1]
        self.calls.append((endpoint, params))
        item = self.responses[endpoint].pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("20260318000694.xml", "<DOCUMENT>본문</DOCUMENT>")
    return buf.getvalue()


def as_json(obj) -> FakeResponse:
    return FakeResponse(json.dumps(obj, ensure_ascii=False).encode("utf-8"))


def client(responses) -> DartClient:
    return DartClient(KEY, session=FakeSession(responses), pause=0)


def no_key_anywhere(root):
    for path in root.rglob("*"):
        assert KEY not in str(path)
        if path.is_file():
            assert KEY.encode() not in path.read_bytes()


FILING = {"rcept_no": "20260318000694", "corp_name": "코스맥스", "report_nm": "사업보고서 (2025.12)",
          "rcept_dt": "20260318", "bsns_year": 2025, "correction": None}


# ---------------------------------------------------------------- 보고서 이름


@pytest.mark.parametrize("name, year, correction", [
    ("사업보고서 (2025.12)", 2025, None),
    ("[기재정정]사업보고서 (2024.12)", 2024, "기재정정"),  # 2026년에 접수돼도 보고대상연도는 2024
    ("[첨부정정]사업보고서 (2025.12)", 2025, "첨부정정"),
])
def test_parses_target_year_and_correction(name, year, correction):
    parsed = parse_annual_report_name(name)
    assert parsed["bsns_year"] == year
    assert parsed["correction"] == correction


@pytest.mark.parametrize("name", ["반기보고서 (2025.06)", "분기보고서 (2025.09)", "감사보고서제출"])
def test_non_annual_reports_are_ignored(name):
    assert parse_annual_report_name(name) is None


# ---------------------------------------------------------------- ZIP 확인


def test_zip_validation_rejects_error_responses():
    assert is_valid_zip(make_zip())
    assert not is_valid_zip('<?xml version="1.0"?><result><status>014</status><message>파일이 존재하지 않습니다</message></result>'.encode())
    assert not is_valid_zip('{"status":"020","message":"요청 제한을 초과하였습니다."}'.encode())
    assert not is_valid_zip(make_zip()[:30])  # 중간에 끊긴 ZIP


# ---------------------------------------------------------------- 원문 저장


def test_filing_is_stored_with_published_and_collected_dates(tmp_path):
    store = RawStore(tmp_path)
    result = fetch_filing(client({"document.xml": [FakeResponse(make_zip())]}), store, "01009789", FILING, "run1")

    assert result["result"] == "stored"
    directory = tmp_path / "01009789" / "filings" / "20260318000694"
    assert (directory / "original.zip").read_bytes() == make_zip()
    meta = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
    assert meta["rcept_dt"] == "20260318"  # 공개일
    assert meta["collected_at"].startswith("20")  # 수집일은 따로
    assert meta["bsns_year"] == 2025
    assert len(meta["sha256"]) == 64
    no_key_anywhere(tmp_path)


def test_error_response_is_not_saved_as_filing(tmp_path):
    store = RawStore(tmp_path)
    error = FakeResponse('{"status":"020","message":"요청 제한을 초과하였습니다."}'.encode())
    result = fetch_filing(client({"document.xml": [error]}), store, "01009789", FILING, "run1")

    assert result["result"] == "invalid_zip"
    directory = tmp_path / "01009789" / "filings" / "20260318000694"
    assert not (directory / "original.zip").exists()
    # 받지 못한 사실은 시도 기록으로 남긴다
    meta = json.loads((directory / "attempts" / "run1" / "metadata.json").read_text(encoding="utf-8"))
    assert meta["result"] == "invalid_zip"
    assert (directory / "attempts" / "run1" / "response.bin").read_bytes() == error.content


def test_failed_attempt_does_not_block_a_later_download(tmp_path):
    store = RawStore(tmp_path)
    error = FakeResponse('{"status":"020","message":"요청 제한을 초과하였습니다."}'.encode())
    fetch_filing(client({"document.xml": [error]}), store, "01009789", FILING, "run1")
    result = fetch_filing(client({"document.xml": [FakeResponse(make_zip())]}), store, "01009789", FILING, "run2")

    assert result["result"] == "stored"
    assert (tmp_path / "01009789" / "filings" / "20260318000694" / "original.zip").exists()


def test_stored_filing_is_never_downloaded_or_overwritten_again(tmp_path):
    store = RawStore(tmp_path)
    fetch_filing(client({"document.xml": [FakeResponse(make_zip())]}), store, "01009789", FILING, "run1")
    original = (tmp_path / "01009789" / "filings" / "20260318000694" / "original.zip").read_bytes()

    second = client({"document.xml": []})  # 호출되면 pop에서 실패한다
    result = fetch_filing(second, store, "01009789", FILING, "run2")

    assert result["result"] == "already_stored"
    assert second.session.calls == []
    assert (tmp_path / "01009789" / "filings" / "20260318000694" / "original.zip").read_bytes() == original


# ---------------------------------------------------------------- 재무 API 응답


def test_no_data_response_is_recorded(tmp_path):
    store = RawStore(tmp_path)
    body = {"status": "013", "message": "조회된 데이타가 없습니다."}
    result = fetch_financials(client({"fnlttSinglAcntAll.json": [as_json(body)]}), store, "00160621", 2025, "11011", "CFS", "run1")

    assert result["status"] == "013"
    directory = tmp_path / "00160621" / "api" / "fnlttSinglAcntAll" / "2025" / "11011" / "CFS" / "run1"
    meta = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
    assert meta["status"] == "013"
    assert meta["request"] == {"corp_code": "00160621", "bsns_year": "2025", "reprt_code": "11011", "fs_div": "CFS"}
    no_key_anywhere(tmp_path)


def test_requery_goes_to_new_run_and_keeps_old_response(tmp_path):
    store = RawStore(tmp_path)
    first = {"status": "000", "list": [{"rcept_no": "20250320001367", "account_id": "ifrs-full_Revenue", "thstrm_amount": "1"}]}
    second = {"status": "000", "list": [{"rcept_no": "20260813000484", "account_id": "ifrs-full_Revenue", "thstrm_amount": "2"}]}
    c = client({"fnlttSinglAcntAll.json": [as_json(first), as_json(second), as_json(second)]})

    fetch_financials(c, store, "00160621", 2024, "11011", "OFS", "run1")
    fetch_financials(c, store, "00160621", 2024, "11011", "OFS", "run2")

    base = tmp_path / "00160621" / "api" / "fnlttSinglAcntAll" / "2024" / "11011" / "OFS"
    assert json.loads((base / "run1" / "response.json").read_bytes())["list"][0]["thstrm_amount"] == "1"
    assert json.loads((base / "run2" / "response.json").read_bytes())["list"][0]["thstrm_amount"] == "2"
    with pytest.raises(FileExistsError):  # 같은 run_id로는 덮어쓰지 않는다
        fetch_financials(c, store, "00160621", 2024, "11011", "OFS", "run2")


def test_response_is_linked_to_stored_filing_or_marked_missing(tmp_path):
    store = RawStore(tmp_path)
    fetch_filing(client({"document.xml": [FakeResponse(make_zip())]}), store, "01009789", FILING, "run1")
    body = {"status": "000", "list": [
        {"rcept_no": "20260318000694", "account_id": "ifrs-full_Revenue", "thstrm_amount": "1"},
        {"rcept_no": "20990101000001", "account_id": "dart_OperatingIncomeLoss", "thstrm_amount": "1"},
    ]}
    result = fetch_financials(client({"fnlttSinglAcntAll.json": [as_json(body)]}), store, "01009789", 2025, "11011", "CFS", "run1")

    assert result["filing_links"] == {"20260318000694": "stored", "20990101000001": "missing"}


# ---------------------------------------------------------------- 공시검색


def test_lists_original_and_all_corrections_for_target_years_only(tmp_path):
    store = RawStore(tmp_path)
    page = {"status": "000", "total_page": 1, "list": [
        {"rcept_no": "20250320001367", "report_nm": "사업보고서 (2024.12)", "rcept_dt": "20250320", "corp_name": "한국화장품제조"},
        {"rcept_no": "20250814003121", "report_nm": "[기재정정]사업보고서 (2024.12)", "rcept_dt": "20250814", "corp_name": "한국화장품제조"},
        {"rcept_no": "20260813000484", "report_nm": "[기재정정]사업보고서 (2024.12)", "rcept_dt": "20260813", "corp_name": "한국화장품제조"},
        {"rcept_no": "20220318000001", "report_nm": "사업보고서 (2021.12)", "rcept_dt": "20220318", "corp_name": "한국화장품제조"},
    ]}
    filings = list_annual_reports(client({"list.json": [as_json(page)]}), store, "00160621", [2022, 2023, 2024, 2025], "run1")

    # 중간 정정본까지 모두, 대상 연도 밖(2021)은 제외
    assert [f["rcept_no"] for f in filings] == ["20250320001367", "20250814003121", "20260813000484"]
    assert (tmp_path / "00160621" / "api" / "list").exists()  # 검색 응답도 원본으로 남는다


def test_list_follows_pages(tmp_path):
    store = RawStore(tmp_path)
    p1 = {"status": "000", "total_page": 2, "list": [{"rcept_no": "1", "report_nm": "사업보고서 (2022.12)", "rcept_dt": "20230320", "corp_name": "x"}]}
    p2 = {"status": "000", "total_page": 2, "list": [{"rcept_no": "2", "report_nm": "사업보고서 (2023.12)", "rcept_dt": "20240320", "corp_name": "x"}]}
    filings = list_annual_reports(client({"list.json": [as_json(p1), as_json(p2)]}), store, "00000001", [2022, 2023], "run1")

    assert [f["rcept_no"] for f in filings] == ["1", "2"]


# ---------------------------------------------------------------- 인증키


def test_key_is_redacted_from_request_errors():
    c = client({"list.json": [requests.ConnectionError(f"failed: https://x/api/list.json?crtfc_key={KEY}")]})
    with pytest.raises(DartError) as e:
        c.get("list.json", {})
    assert KEY not in str(e.value)
    assert "***" in str(e.value)


def test_key_is_redacted_from_http_errors():
    c = client({"list.json": [FakeResponse(b"", status_code=500)]})
    with pytest.raises(DartError) as e:
        c.get("list.json", {})
    assert KEY not in str(e.value)
