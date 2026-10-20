"""시장 통계 원본 수집 (docs/decisions.md D18).

e-나라지표(index.go.kr) 지표 페이지(정의·출처)와 통계표 페이지를 받은 그대로 보존한다.
- 저장: data/raw/stats/index_go_kr/{idx_cd}/{run_id}/indicator.html, table.html + metadata.json
- 페이지에 적힌 '최근 갱신일'은 이 수집본의 갱신일일 뿐, 각 연도 값이 처음 공개된 날이 아니다
"""

import html
import json
import re
import sys
from pathlib import Path

import requests

from src.collectors.dart import new_run_id, sha256, utc_now

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "data" / "raw" / "stats"
INDICATOR_URL = "https://www.index.go.kr/unify/idx-info.do?idxCd={idx_cd}"
TABLE_URL = "https://www.index.go.kr/unity/potal/eNara/sub/showStblGams3.do?stts_cd={stts_cd}&idx_cd={idx_cd}&freq=Y&period={start}:{end}"


def page_dates(page: str) -> dict:
    """지표 페이지에 적힌 최근 갱신일·입력예정일."""
    text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", page)))
    out = {}
    for key, label in (("updated", "최근 갱신일"), ("next_update", "입력예정일")):
        m = re.search(label + r"\s*:?\s*(\d{4}-\d{2}-\d{2})", text)
        out[key] = m.group(1) if m else None
    return out


def fetch_index_table(idx_cd: str, stts_cd: str, start: int, end: int, session: requests.Session | None = None) -> Path:
    s = session or requests.Session()
    run_id = new_run_id()
    out = RAW / "index_go_kr" / idx_cd / run_id
    out.mkdir(parents=True, exist_ok=False)
    urls = {"indicator.html": INDICATOR_URL.format(idx_cd=idx_cd),
            "table.html": TABLE_URL.format(stts_cd=stts_cd, idx_cd=idx_cd, start=start, end=end)}
    files = {}
    for name, url in urls.items():
        r = s.get(url, timeout=30)
        r.raise_for_status()
        (out / name).write_bytes(r.content)
        files[name] = {"url": url, "status_code": r.status_code, "sha256": sha256(r.content), "bytes": len(r.content)}
    meta = {"source": "e-나라지표 (index.go.kr)", "idx_cd": idx_cd, "stts_cd": stts_cd, "period": f"{start}:{end}",
            "collected_at": utc_now(), "files": files,
            "page_dates": page_dates((out / "indicator.html").read_text(encoding="utf-8", errors="replace")),
            "note": "페이지의 갱신일은 수집본의 갱신일이며 각 연도 값의 최초 공개일이 아니다"}
    (out / "metadata.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


BROWSER_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"}
GUIDELINE_DOCS = ROOT / "data" / "manual" / "stat_guideline_docs.csv"


def fetch_guideline_docs(session: requests.Session | None = None, only: list[str] | None = None) -> dict:
    """통계 작성지침 문서(D20)를 받은 그대로 보존한다: data/raw/stats/guidelines/{doc_id}/{run_id}/original.{ext} + metadata.json.

    등록부(stat_guideline_docs.csv)에서 status가 '수집'인 문서만 받는다. 대한화장품협회 첨부파일은 브라우저 헤더가 없으면
    200 응답에 404 안내 HTML을 돌려주므로, 응답 형식이 등록부의 doc_type과 맞지 않으면 저장하지 않고 중단한다.
    """
    import csv
    s = session or requests.Session()
    rows = [r for r in csv.DictReader(GUIDELINE_DOCS.open(encoding="utf-8-sig")) if r["status"] == "수집" and (not only or r["doc_id"] in only)]
    magic = {"pdf": b"%PDF", "xlsx": b"PK"}
    saved = {}
    for r in rows:
        resp = s.get(r["url"], headers={**BROWSER_HEADERS, "Referer": "https://kcia.or.kr/"}, timeout=60)
        resp.raise_for_status()
        data = resp.content
        if r["doc_type"] in magic and not data.startswith(magic[r["doc_type"]]):
            raise IOError(f"{r['doc_id']}: {r['doc_type']}가 아닌 응답 ({resp.headers.get('content-type')}, {len(data)} bytes)")
        out = RAW / "guidelines" / r["doc_id"] / new_run_id()
        out.mkdir(parents=True, exist_ok=False)
        name = f"original.{r['doc_type']}"
        (out / name).write_bytes(data)
        (out / "metadata.json").write_text(json.dumps({
            "doc_id": r["doc_id"], "title": r["title"], "issuer": r["issuer"], "url": r["url"], "version": r["version"],
            "applies_to_years": r["applies_to_years"], "collected_at": utc_now(), "status_code": resp.status_code,
            "content_type": resp.headers.get("content-type"),
            "file": {"name": name, "sha256": sha256(data), "bytes": len(data)},
            "note": "게시판 현재본은 수집 시점의 문안이며, 과거 연도에 적용된 문안이라는 뜻이 아니다"}, ensure_ascii=False, indent=2), encoding="utf-8")
        saved[r["doc_id"]] = out
    return saved


def register_manual_file(src: Path, provider: str, table_id: str, meta: dict) -> Path:
    """브라우저로 직접 내려받은 통계 파일을 원본 보존 위치로 옮기고 metadata.json을 남긴다.

    자동 수집이 막힌 표(KOSIS 등)에 쓴다. 파일은 바꾸지 않고(이름만 original.확장자) 해시·내려받은 경로·출처를 기록한다.
    """
    data = src.read_bytes()
    out = RAW / provider / table_id / new_run_id()
    out.mkdir(parents=True, exist_ok=False)
    dst = out / f"original{src.suffix.lower()}"
    dst.write_bytes(data)
    (out / "metadata.json").write_text(json.dumps({
        **meta, "table_id": table_id, "registered_at": utc_now(), "original_name": src.name,
        "method": "사용자가 브라우저에서 직접 내려받음 (자동 수집 실패 — 세션 필요)",
        "file": {"name": dst.name, "sha256": sha256(data), "bytes": len(data)}}, ensure_ascii=False, indent=2), encoding="utf-8")
    if sha256(dst.read_bytes()) != sha256(data):
        raise IOError("복사본 해시 불일치")
    src.unlink()
    return out


if __name__ == "__main__":
    if sys.argv[1:2] == ["guidelines"]:  # 작성지침 문서 (D20): python -m src.collectors.stats guidelines [G01 G02 ...]
        for doc_id, path in fetch_guideline_docs(only=sys.argv[2:] or None).items():
            print(doc_id, path)
    else:
        # 화장품 생산현황 (idx 3043) / 통계표 '화장품 생산실적' (304301)
        print(fetch_index_table(*(sys.argv[1:3] or ["3043", "304301"]), start=2015, end=2025))
