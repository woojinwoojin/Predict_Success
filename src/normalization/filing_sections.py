"""사업보고서 원문에서 주석 구간을 찾는다 (수기 확인용 검색 도구, D10).

목차에도 같은 제목이 나오므로, 제목 바로 뒤에 실제 주석 1번이 이어지는 위치를 시작으로 쓴다.
"""

import re
import zipfile
from pathlib import Path

from src.normalization.dart_facts import filing_text

NOTE_TITLE = {"CFS": r"\d\.\s*연결재무제표\s*주석", "OFS": r"\d\.\s*재무제표\s*주석"}
FIRST_NOTE = r"^\s*(?:‖\s*)*(?:\d{4}년|제\s*\d+\s*(?:\(\s*당\s*\)\s*)?기|1\.\s*|1\)\s*|\(?1\)?\s*일반|일반\s*사항|회사의\s*개요)"
NEXT_CHAPTER = {"CFS": r"\d\.\s*재무제표\s*\d-1\.\s*재무상태표", "OFS": r"\d\.\s*(?:배당에\s*관한\s*사항|기타\s*재무에\s*관한\s*사항)|IV\.\s*이사의\s*경영진단"}


def report_text(zip_path: Path, rcept_no: str) -> str:
    raw = zipfile.ZipFile(zip_path).read(f"{rcept_no}.xml").decode("utf-8", errors="replace")
    return re.sub(r"\s+", " ", filing_text(raw))


def notes_section(text: str, fs_div: str) -> tuple[int, int]:
    """(시작, 끝) 위치. 연결 주석이면 '재무제표 주석' 앞에 '연결'이 붙은 제목만 쓴다."""
    starts = [m for m in re.finditer(NOTE_TITLE[fs_div], text)
              if re.match(FIRST_NOTE, text[m.end(): m.end() + 80])]
    if fs_div == "OFS":
        starts = [m for m in starts if "연결" not in text[max(0, m.start() - 4): m.end()]]
    if not starts:
        return -1, -1
    s = starts[-1].start()  # 목차가 아닌 본문 (뒤쪽)
    end = re.search(NEXT_CHAPTER[fs_div], text[s + 50:])
    return s, (s + 50 + end.start()) if end else len(text)
