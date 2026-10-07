"""재무 API 응답 → 관측값 테이블(A). 출처별 값 하나 = 한 행 (docs/decisions.md D5).

원본(data/raw)은 읽기만 한다. 결과는 언제든 다시 만들 수 있는 중간 산출물이다.
"""

import html
import json
import re
import zipfile
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

# API 금액 열 → source_column. thstrm_add_amount(누적)는 분기·반기 보고서에만 값이 있다.
AMOUNT_COLUMNS = {
    "thstrm_amount": ("당기", "thstrm_nm"),
    "thstrm_add_amount": ("당기누적", "thstrm_nm"),
    "frmtrm_amount": ("전기", "frmtrm_nm"),
    "bfefrmtrm_amount": ("전전기", "bfefrmtrm_nm"),
}
# 재무상태표는 시점, 손익·현금흐름은 기간. 자본변동표는 기초·기말 잔액과 변동이 섞여 있어 정하지 않는다.
PERIOD_TYPE = {"BS": "instant", "IS": "duration", "CIS": "duration", "CF": "duration"}
# OpenDART 전체 재무제표 API 금액은 원 단위 (2025 파일럿을 사업보고서와 대조해 확인, D2·D4)
UNIT, UNIT_MULTIPLIER = "원", 1
UNIT_BASIS = "OpenDART fnlttSinglAcntAll 금액(원). 2025 파일럿 5개사 원문 대조로 확인"
NON_BODY_CORRECTIONS = {"첨부정정", "첨부추가"}  # 재무제표 본문을 바꾸지 않는 정정

TERM = re.compile(r"제\s*(\d+)\s*기")
FISCAL_PERIOD = re.compile(
    r"제\s*(\d+)\s*(?:\(\s*(?:당|전|전전)\s*\)\s*)?기\s*(?:\(?\s*(?:당기|전기|전전기)\s*\)?\s*)?"
    r"(\d{4})\s*[.\-년]\s*(\d{1,2})\s*[.\-월]\s*(\d{1,2})\s*일?\s*부터\s*"
    r"(\d{4})\s*[.\-년]\s*(\d{1,2})\s*[.\-월]\s*(\d{1,2})\s*일?\s*까지"
)


def parse_amount(raw) -> tuple[Decimal | None, str]:
    """숫자 0은 정상 값이다. 원문 값 없음과 변환 실패를 구분한다."""
    if raw is None or str(raw).strip() in ("", "-"):
        return None, "empty_in_source"
    text = str(raw).strip().replace(",", "")
    try:
        return Decimal(text), "parsed"
    except InvalidOperation:
        return None, "parse_failed"


def _iso(y, m, d) -> str:
    return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"


def is_plausible_fiscal_period(start: str, end: str) -> bool:
    """잘못 붙은 날짜를 막는 규칙. 당기·전기 열이 나란히 있는 표를 평문으로 펴면
    "제12기 2024년 1월 1일부터 2025년 12월 31일까지"처럼 다른 열의 날짜가 이어 붙는다.
    역순이거나 1년을 넘는 매치만 버린다(국내 사업연도는 1년을 넘을 수 없다: 상법·법인세법).
    1년보다 짧은 기간(결산기 변경 등)은 정상으로 받아들인다 — 연간 여부는 is_annual_period로 따로 표시한다."""
    s, e = date.fromisoformat(start), date.fromisoformat(end)
    try:
        one_year_later = s.replace(year=s.year + 1)
    except ValueError:  # 2월 29일 시작
        one_year_later = date(s.year + 1, 3, 1)
    return s <= e < one_year_later


def is_annual_period(start: str, end: str) -> bool:
    """정상적인 12개월 기간인지. 짧은 기간을 막지는 않고, 기간 비교 지표에서 주의하도록 표시만 한다."""
    s, e = date.fromisoformat(start), date.fromisoformat(end)
    try:
        return (e - s).days + 1 in (365, 366) and e == s.replace(year=s.year + 1) - timedelta(days=1)
    except ValueError:
        return False


def fiscal_periods_from_filing(zip_path: Path) -> dict[int, tuple[str, str] | None]:
    """원문에 적힌 "제 N 기 YYYY.MM.DD 부터 YYYY.MM.DD 까지"로 기수 → (시작일, 종료일)을 만든다.

    같은 기수에 서로 다른 (그럴듯한) 기간이 적혀 있으면 None (확인 필요).
    """
    found: dict[int, set] = {}
    with zipfile.ZipFile(zip_path) as z:
        for name in z.namelist():
            text = html.unescape(re.sub(r"<[^>]+>", " ", z.read(name).decode("utf-8", errors="replace")))
            for m in FISCAL_PERIOD.finditer(text):
                try:
                    period = (_iso(m[2], m[3], m[4]), _iso(m[5], m[6], m[7]))
                    if not is_plausible_fiscal_period(*period):
                        continue
                except ValueError:  # 존재하지 않는 날짜
                    continue
                found.setdefault(int(m[1]), set()).add(period)
    return {term: (next(iter(periods)) if len(periods) == 1 else None) for term, periods in found.items()}


def load_filings(raw_root: Path) -> dict[str, dict]:
    """접수번호 → 원문 메타데이터 + 연결 상태 (stored / attempt_only)."""
    filings = {}
    for corp_dir in raw_root.iterdir():
        for filing_dir in (corp_dir / "filings").glob("*") if (corp_dir / "filings").exists() else []:
            meta_path = filing_dir / "metadata.json"
            if meta_path.exists():
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                filings[filing_dir.name] = {**meta, "link": "stored", "zip": filing_dir / "original.zip"}
            elif (filing_dir / "attempts").exists():
                filings[filing_dir.name] = {"rcept_no": filing_dir.name, "link": "attempt_only"}
    return filings


def snapshots(raw_root: Path):
    """재무 API 응답 스냅샷: {corp}/api/fnlttSinglAcntAll/{year}/{reprt}/{fs}/{run_id}/"""
    for meta_path in sorted(raw_root.glob("*/api/fnlttSinglAcntAll/*/*/*/*/metadata.json")):
        directory = meta_path.parent
        yield directory.relative_to(raw_root).as_posix(), directory, json.loads(meta_path.read_text(encoding="utf-8"))


def flatten_snapshot(snapshot_id: str, response: dict, meta: dict, filings: dict, periods: dict) -> list[dict]:
    rows = []
    request = meta["request"]
    for index, item in enumerate(response.get("list", [])):
        rcept_no = item.get("rcept_no")
        filing = filings.get(rcept_no, {"link": "missing"})
        sj_div = item.get("sj_div")
        for column, (source_column, label_field) in AMOUNT_COLUMNS.items():
            if column not in item:
                continue
            if column == "thstrm_add_amount" and str(item[column]).strip() == "":
                continue  # 사업보고서에는 누적 열이 비어 있다 — 관측값이 아니라 해당 없음
            value, value_status = parse_amount(item[column])
            label = item.get(label_field, "")
            period_type = PERIOD_TYPE.get(sj_div)
            period_start = period_end = None
            term = TERM.search(label or "")
            period = periods.get(rcept_no, {}).get(int(term[1])) if term else None
            if period_type is None:
                period_status = "review_needed:period_type"
            elif period is None:
                period_status = "review_needed:period_not_found"
            else:
                period_start, period_end = period
                if period_type == "instant":
                    period_start = None
                period_status = "from_filing_text"
            rows.append({
                "row_id": f"{snapshot_id}#{index}#{column}",
                "snapshot_id": snapshot_id, "row_index": index, "ord": item.get("ord"),
                "corp_code": item.get("corp_code") or request["corp_code"],
                "fs_div": request["fs_div"], "sj_div": sj_div, "sj_nm": item.get("sj_nm"),
                "account_id": item.get("account_id"), "account_nm": item.get("account_nm"),
                "account_detail": item.get("account_detail"),
                "report_bsns_year": int(request["bsns_year"]), "reprt_code": request["reprt_code"],
                "source_column": source_column, "column_label": label,
                "raw_amount": item[column], "value": None if value is None else str(value), "value_status": value_status,
                "currency": item.get("currency"), "unit": UNIT, "unit_multiplier": UNIT_MULTIPLIER,
                "period_type": period_type, "period_start": period_start, "period_end": period_end, "period_status": period_status,
                "period_is_annual": None if period is None or period_type is None else is_annual_period(*period),
                "rcept_no": rcept_no, "rcept_dt": filing.get("rcept_dt"), "report_nm": filing.get("report_nm"),
                "filing_link": filing["link"], "collected_at": meta["collected_at"],
            })
    return rows


def build_facts(raw_root: Path) -> tuple[list[dict], list[dict]]:
    """모든 스냅샷을 펼친다. (관측값 행, 스냅샷 목록) — 자료 없음 응답도 스냅샷 목록에 남는다."""
    filings = load_filings(raw_root)
    periods = {no: fiscal_periods_from_filing(f["zip"]) for no, f in filings.items() if f["link"] == "stored"}
    rows, snaps = [], []
    for snapshot_id, directory, meta in snapshots(raw_root):
        response = json.loads((directory / "response.json").read_text(encoding="utf-8"))
        flat = flatten_snapshot(snapshot_id, response, meta, filings, periods)
        rows.extend(flat)
        snaps.append({"snapshot_id": snapshot_id, **meta["request"], "status": meta["status"], "rows": len(flat),
                      "rcept_nos": meta.get("rcept_nos", [])})
    return rows, snaps


def account_key(row: dict) -> tuple:
    """같은 계정인지 비교할 때의 키. 표준코드가 없으면 계정명으로 비교하되 그 사실을 키에 남긴다."""
    standard = row["account_id"] not in (None, "", "-", "-표준계정코드 미사용-")
    return (row["sj_div"], row["account_id"] if standard else f"name:{row['account_nm']}", row["account_detail"])


def same_period_mismatches(rows: list[dict]) -> dict:
    """서로 다른 보고서·열에서 같은 기간·계정의 값이 다른지 본다. 재작성으로 확정하지 않는다 (D5).

    한 응답 안에서 같은 키가 여러 번 나오면 어느 행끼리 비교할지 알 수 없어 비교에서 빼고 따로 센다.
    """
    within = {}
    for r in rows:
        k = (r["snapshot_id"], r["source_column"]) + account_key(r)
        within[k] = within.get(k, 0) + 1
    ambiguous = {k[2:] + (k[0].split("/")[0],) for k, n in within.items() if n > 1}

    groups: dict[tuple, list] = {}
    for r in rows:
        if r["value_status"] != "parsed" or r["period_end"] is None:
            continue
        key = account_key(r)
        if key + (r["corp_code"],) in ambiguous:
            continue
        # 비교 조건: 기업·연결/별도·재무제표·계정·세부 구분·기간 유형·기간·통화가 모두 같아야 같은 값으로 본다
        groups.setdefault((r["corp_code"], r["fs_div"]) + key + (r["period_type"], r["period_start"], r["period_end"], r["currency"]), []).append(r)
    compared = {k: g for k, g in groups.items() if len({(r["rcept_no"], r["source_column"]) for r in g}) > 1}
    mismatched = {k: g for k, g in compared.items() if len({Decimal(r["value"]) for r in g}) > 1}
    return {"compared_groups": len(compared), "mismatched": mismatched, "ambiguous_keys": len(ambiguous)}


def mismatch_kind(group: list[dict]) -> dict:
    """불일치의 겉모습만 분류한다. 원인(재작성·재분류·표기)은 원문을 확인한 뒤 판단한다."""
    values = {Decimal(r["value"]) for r in group}
    return {
        "kind": "sign_only" if len({abs(v) for v in values}) == 1 else "magnitude",
        "label_changed": len({r["account_nm"] for r in group}) > 1,
        # 표준코드 없이 계정명으로 묶인 그룹 — 서로 다른 항목이 같은 이름일 수 있어 신뢰도가 낮다
        "nonstandard_key": account_key(group[0])[1].startswith("name:"),
    }
