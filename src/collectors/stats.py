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


if __name__ == "__main__":
    # 화장품 생산현황 (idx 3043) / 통계표 '화장품 생산실적' (304301)
    print(fetch_index_table(*(sys.argv[1:3] or ["3043", "304301"]), start=2015, end=2025))
