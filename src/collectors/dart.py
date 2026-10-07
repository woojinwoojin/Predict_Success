"""OpenDART 원본 수집. 받은 그대로, 덮어쓰지 않고 저장한다 (docs/decisions.md D4).

- 원문(사업보고서 ZIP)은 접수번호별: {corp}/filings/{rcept_no}/original.zip + metadata.json
- API 응답은 조회 조건·수집시각별: {corp}/api/{endpoint}/{조건...}/{run_id}/response.json + metadata.json
- 인증키는 경로·메타데이터·오류 메시지에 남기지 않는다.
"""

import hashlib
import io
import json
import re
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE_URL = "https://opendart.fss.or.kr/api"
# "사업보고서 (2024.12)", "[기재정정]사업보고서 (2024.12)", "[첨부정정]사업보고서 (2025.12)"
ANNUAL_REPORT = re.compile(r"^(?:\[(?P<label>[^\]]+)\])?\s*사업보고서\s*\((?P<year>\d{4})\.(?P<month>\d{2})\)")


class DartError(Exception):
    pass


def new_run_id(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return now.strftime("%Y%m%dT%H%M%S%fZ")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def parse_annual_report_name(report_nm: str) -> dict | None:
    """사업보고서 이름에서 보고대상연도와 정정 구분을 읽는다. 사업보고서가 아니면 None."""
    m = ANNUAL_REPORT.match(report_nm.strip())
    if not m:
        return None
    return {"bsns_year": int(m["year"]), "period_end_month": m["month"], "correction": m["label"]}


def is_valid_zip(data: bytes) -> bool:
    """오류 응답(XML·JSON)을 ZIP으로 착각하지 않도록, 실제로 열리고 손상이 없는지 확인한다."""
    if not data.startswith(b"PK"):
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            return bool(z.namelist()) and z.testzip() is None
    except zipfile.BadZipFile:
        return False


class DartClient:
    def __init__(self, api_key: str, session: requests.Session | None = None, pause: float = 0.15):
        if not api_key:
            raise DartError("DART_API_KEY가 없습니다 (.env 확인)")
        self._key = api_key
        self.session = session or requests.Session()
        self.pause = pause  # 분당 호출 한도를 넘지 않게

    def _redact(self, text: str) -> str:
        return text.replace(self._key, "***")

    def get(self, endpoint: str, params: dict) -> requests.Response:
        try:
            r = self.session.get(f"{BASE_URL}/{endpoint}", params={**params, "crtfc_key": self._key}, timeout=30)
            r.raise_for_status()
        except requests.RequestException as e:
            # requests의 오류 메시지에는 키가 들어간 URL이 포함된다
            raise DartError(self._redact(f"{endpoint} 요청 실패: {e}")) from None
        finally:
            time.sleep(self.pause)
        return r


@dataclass
class RawStore:
    root: Path  # data/raw/dart

    def filing_dir(self, corp_code: str, rcept_no: str) -> Path:
        return self.root / corp_code / "filings" / rcept_no

    def api_dir(self, corp_code: str, endpoint: str, *conditions: str, run_id: str) -> Path:
        return self.root / corp_code / "api" / endpoint / Path(*conditions) / run_id

    def write_new(self, directory: Path, files: dict[str, bytes]) -> None:
        """새 파일만 쓴다. 하나라도 이미 있으면 아무것도 쓰지 않고 실패한다 (덮어쓰기 금지).

        폴더는 있어도 된다: 원문을 받지 못한 시도(attempts/)가 접수번호 폴더를 먼저 만들 수 있다.
        """
        existing = [name for name in files if (directory / name).exists()]
        if existing:
            raise FileExistsError(f"이미 있는 원본은 덮어쓰지 않습니다: {directory} {existing}")
        directory.mkdir(parents=True, exist_ok=True)
        for name, data in files.items():
            with open(directory / name, "xb") as f:  # x: 그 사이에 생겼어도 덮어쓰지 않는다
                f.write(data)


def _json_bytes(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")


def list_annual_reports(client: DartClient, store: RawStore, corp_code: str, years: list[int], run_id: str) -> list[dict]:
    """대상 연도의 사업보고서(최초본 + 모든 정정본)를 공시검색으로 찾는다. 검색 응답도 원본으로 남긴다."""
    bgn_de = f"{min(years) + 1}0101"  # 2022 사업보고서는 2023년에 접수된다
    end_de = datetime.now(timezone.utc).strftime("%Y%m%d")
    params = {"corp_code": corp_code, "bgn_de": bgn_de, "end_de": end_de, "pblntf_detail_ty": "A001", "page_count": 100}
    found, page = [], 1
    while True:
        r = client.get("list.json", {**params, "page_no": page})
        body = r.json()
        store.write_new(
            store.api_dir(corp_code, "list", "A001", f"{bgn_de}_{end_de}", f"page{page}", run_id=run_id),
            {
                "response.json": r.content,
                "metadata.json": _json_bytes({
                    "endpoint": "list.json", "request": {**params, "page_no": page}, "collected_at": utc_now(),
                    "status": body.get("status"), "message": body.get("message"), "sha256": sha256(r.content),
                }),
            },
        )
        if body.get("status") == "013":  # 조회된 데이터 없음
            break
        if body.get("status") != "000":
            raise DartError(f"공시검색 실패 {corp_code}: {body.get('status')} {body.get('message')}")
        for item in body["list"]:
            parsed = parse_annual_report_name(item["report_nm"])
            if parsed and parsed["bsns_year"] in years:
                found.append({**item, **parsed})
        if page >= int(body.get("total_page", 1)):
            break
        page += 1
    return sorted(found, key=lambda x: (x["bsns_year"], x["rcept_dt"], x["rcept_no"]))


def fetch_filing(client: DartClient, store: RawStore, corp_code: str, filing: dict, run_id: str) -> dict:
    """사업보고서 원문 ZIP을 받는다. 이미 받은 접수번호는 다시 받지 않는다 (원문은 접수번호로 고정)."""
    directory = store.filing_dir(corp_code, filing["rcept_no"])
    if (directory / "original.zip").exists():
        return {"rcept_no": filing["rcept_no"], "result": "already_stored"}
    r = client.get("document.xml", {"rcept_no": filing["rcept_no"]})
    if not is_valid_zip(r.content):
        # 오류 응답은 원문(original.zip)으로 저장하지 않는다. 받지 못한 사실과 응답은 시도 기록으로 남긴다.
        # 예: [첨부정정]은 본문이 없어 "014 파일이 존재하지 않습니다"가 온다.
        store.write_new(directory / "attempts" / run_id, {
            "response.bin": r.content,
            "metadata.json": _json_bytes({
                "rcept_no": filing["rcept_no"], "report_nm": filing["report_nm"], "rcept_dt": filing["rcept_dt"],
                "collected_at": utc_now(), "result": "invalid_zip", "sha256": sha256(r.content),
            }),
        })
        return {"rcept_no": filing["rcept_no"], "result": "invalid_zip", "head": r.content[:200].decode("utf-8", "replace")}
    store.write_new(directory, {
        "original.zip": r.content,
        "metadata.json": _json_bytes({
            "corp_code": corp_code, "corp_name": filing.get("corp_name"), "rcept_no": filing["rcept_no"],
            "report_nm": filing["report_nm"], "bsns_year": filing["bsns_year"], "correction": filing["correction"],
            "rcept_dt": filing["rcept_dt"],  # 공개일 (접수일)
            "collected_at": utc_now(),  # 수집일 — 공개일과 구분한다
            "source": "OpenDART document.xml", "found_by_list_run": run_id,
            "sha256": sha256(r.content), "zip_members": zipfile.ZipFile(io.BytesIO(r.content)).namelist(),
        }),
    })
    return {"rcept_no": filing["rcept_no"], "result": "stored"}


def fetch_financials(client: DartClient, store: RawStore, corp_code: str, bsns_year: int, reprt_code: str, fs_div: str, run_id: str) -> dict:
    """전체 재무제표 API 응답을 조회 조건·수집시각별로 저장한다. '자료 없음'도 그대로 남긴다."""
    params = {"corp_code": corp_code, "bsns_year": str(bsns_year), "reprt_code": reprt_code, "fs_div": fs_div}
    r = client.get("fnlttSinglAcntAll.json", params)
    body = r.json()
    rcept_nos = sorted({item["rcept_no"] for item in body.get("list", []) if item.get("rcept_no")})
    # API 값이 어느 원문에서 왔는지 연결한다. 원문이 없으면 그 상태를 기록한다.
    links = {no: ("stored" if (store.filing_dir(corp_code, no) / "original.zip").exists() else "missing") for no in rcept_nos}
    store.write_new(
        store.api_dir(corp_code, "fnlttSinglAcntAll", str(bsns_year), reprt_code, fs_div, run_id=run_id),
        {
            "response.json": r.content,
            "metadata.json": _json_bytes({
                "endpoint": "fnlttSinglAcntAll.json", "request": params, "collected_at": utc_now(),
                "status": body.get("status"), "message": body.get("message"), "sha256": sha256(r.content),
                "rcept_nos": rcept_nos, "filing_links": links,
                "verification_status": "not_compared",  # 원문과 수치 대조 전
            }),
        },
    )
    return {"bsns_year": bsns_year, "fs_div": fs_div, "status": body.get("status"), "rcept_nos": rcept_nos, "filing_links": links}
