"""e-나라지표 통계표 페이지(table.html)를 행·연도별 값으로 읽는다 (D18).

표의 행 이름(<th>)과 연도 칸(item-id='2025Y')을 그대로 읽고, 단위·출처·통계공표시기·주석 문구도 함께 남긴다.
쉼표만 제거하며 반올림·단위 변환은 하지 않는다.
"""

import html
import re
from decimal import Decimal, InvalidOperation


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def parse_table(page: str) -> dict:
    unit = re.search(r'id="sttsCdUnit">\s*\[단위\s*:\s*([^\]]+)\]', page)
    rows = []
    for tr in re.findall(r"<tr id='tr_\d+_\d+'>(.*?)</tr>", page, flags=re.S):
        th = re.search(r"<th[^>]*>(.*?)</th>", tr, flags=re.S)
        cells = re.findall(r"item-id='(\d{4})Y'[^>]*>(.*?)</td>", tr, flags=re.S)
        if not th or not cells:
            continue
        values = {}
        for year, raw in cells:
            raw = _text(raw)
            try:
                values[int(year)] = Decimal(raw.replace(",", "")) if raw not in ("", "-") else None
            except InvalidOperation:
                values[int(year)] = None
        rows.append({"label": _text(th.group(1)), "raw": {int(y): _text(v) for y, v in cells}, "values": values})
    # 같은 이름의 행(증감률)은 바로 위 행에 딸린 것으로 이름을 붙인다
    for i, r in enumerate(rows):
        if r["label"] in ("증감률", "전년대비 성장률") and i > 0:
            r["of"] = rows[i - 1]["label"]
    foot = _text(page[page.find('id="srcTitle"'):]) if 'id="srcTitle"' in page else ""

    def after(label, stop):
        m = re.search(label + r"\s*:\s*(.*?)\s*(?:" + stop + r"|$)", foot)
        return m.group(1).strip() if m else None

    return {"unit": unit.group(1).strip() if unit else None, "rows": rows,
            "source": after("출처", "통계공표시기|주석"), "release_rule": after("통계공표시기", "주석|관련"),
            "notes": after("주석", "관련 KOSIS|$")}
