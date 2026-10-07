"""분석 입력 선택: 공개된 보고서 중 기준일 최신 유효본 → 그 보고서와 연결된 스냅샷 하나 (D5, D11 ②).

관측값 보관(모든 스냅샷)과 분석 입력 선택(키마다 하나)을 분리한다.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from src.collectors.dart import parse_annual_report_name
from src.normalization.dart_facts import NON_BODY_CORRECTIONS


def load_published_filings(raw_root: Path) -> dict[str, dict]:
    """저장한 공시검색 응답(list.json)에서 공개된 사업보고서 전체를 모은다 — 원문 저장 성공 여부와 무관하다."""
    published = {}
    for path in sorted(raw_root.glob("*/api/list/A001/*/*/*/response.json")):
        body = json.loads(path.read_text(encoding="utf-8"))
        for item in body.get("list", []):
            parsed = parse_annual_report_name(item.get("report_nm", ""))
            if parsed:
                published[item["rcept_no"]] = {"rcept_no": item["rcept_no"], "corp_code": item["corp_code"], "rcept_dt": item["rcept_dt"],
                                               "report_nm": item["report_nm"], **parsed}
    return published


def latest_valid_filing(published: dict, corp_code: str, bsns_year: int, as_of: str) -> str | None:
    """기준일(YYYYMMDD) 이전에 공개된 해당 사업연도 사업보고서 중 최신 유효 정정본의 접수번호.

    저장 여부는 보지 않는다 — 최신본 원문을 못 받았다고 이전 보고서로 내려가지 않게 (검토 지적 ③).
    재무제표 본문을 바꾸지 않는 정정(첨부정정 등)은 후보에서 뺀다.
    """
    candidates = [f for f in published.values()
                  if f["corp_code"] == corp_code and f["bsns_year"] == bsns_year and f["rcept_dt"] <= as_of
                  and f.get("correction") not in NON_BODY_CORRECTIONS]
    return max(candidates, key=lambda f: (f["rcept_dt"], f["rcept_no"]))["rcept_no"] if candidates else None


@dataclass
class Selection:
    status: str  # selected / no_data / held
    snapshot_id: str | None
    rcept_no: str | None
    note: str


def select_snapshot(snaps: list[dict], published: dict, filings: dict, corp_code: str, fs_div: str, bsns_year: int, as_of: str) -> Selection:
    """한 기업·재무기준·사업연도의 분석 입력 스냅샷을 하나 고른다."""
    group = [s for s in snaps if s["corp_code"] == corp_code and s["fs_div"] == fs_div and int(s["bsns_year"]) == bsns_year]
    selected = latest_valid_filing(published, corp_code, bsns_year, as_of)
    if selected is None:
        return Selection("held", None, None, f"기준일 {as_of}까지 공개된 유효 사업보고서 없음")
    if filings.get(selected, {}).get("link") != "stored":
        return Selection("held", None, selected, f"선택된 보고서 {selected}의 원문 미저장 — 이전 보고서로 대체하지 않음")
    matching = [s for s in group if s["status"] == "000" and s["rcept_nos"] == [selected]]
    if matching:
        # 같은 보고서를 여러 번 조회했으면 가장 최근 수집을 쓴다 (run_id는 수집 시각 순)
        latest = max(matching, key=lambda s: s["snapshot_id"].rsplit("/", 1)[-1])
        note = f"같은 보고서 스냅샷 {len(matching)}개 중 최근 수집" if len(matching) > 1 else ""
        return Selection("selected", latest["snapshot_id"], selected, note)
    if group and all(s["status"] == "013" for s in group):
        latest = max(group, key=lambda s: s["snapshot_id"].rsplit("/", 1)[-1])
        return Selection("no_data", latest["snapshot_id"], selected, "API 응답: 조회된 데이터 없음")
    found = sorted({tuple(s["rcept_nos"]) for s in group if s["status"] == "000"})
    return Selection("held", None, selected, f"기준일 {as_of}의 선택 보고서 {selected} ≠ API 값의 보고서 {found}")
