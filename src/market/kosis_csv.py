"""KOSIS에서 내려받은 표 CSV(첫 열 분류, 나머지 열 연도)를 읽는다 (D19).

인코딩은 metadata.json에 적힌 값을 쓴다. 쉼표만 제거하며 반올림·단위 변환은 하지 않는다. 단위는 CSV에 없으므로 metadata에서 가져온다.
"""

import csv
import io
from decimal import Decimal, InvalidOperation


def parse_kosis_csv(data: bytes, encoding: str) -> dict:
    reader = list(csv.reader(io.StringIO(data.decode(encoding))))
    header = reader[0]
    years = [int(h) for h in header[1:]]
    rows = []
    for line in reader[1:]:
        if not line or not line[0].strip():
            continue
        values = {}
        for y, raw in zip(years, line[1:]):
            raw = raw.strip().replace(",", "")
            try:
                values[y] = Decimal(raw) if raw not in ("", "-") else None
            except InvalidOperation:
                values[y] = None
        rows.append({"label": line[0].strip(), "values": values})
    return {"dimension": header[0], "years": years, "rows": rows}
