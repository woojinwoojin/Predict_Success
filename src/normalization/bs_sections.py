"""재무상태표 행의 유동·비유동 구간을 산술로 확인한다 (docs/decisions.md D8).

API 응답의 행 순서(ord)에서 소계 행 뒤에 오는 항목들을 그 소계의 구간 후보로 보고,
항목 합이 소계와 원 단위까지 같으면 bs_section_sum_ok = True. 이것은 **보조 검증**이다 (D9):
구간 경계와 원문·계정 의미까지 확인된 경우에만 유동·비유동 분류 근거로 쓴다. 비교 키를 나누는 데에만(잘못 합치지 않게) 쓴다.
순서만으로는 판단하지 않는다 (자본 항목이 자본총계보다 먼저 나오는 등 회사마다 배치가 다르다).
"""

from decimal import Decimal

SUBTOTALS = {
    "ifrs-full_CurrentAssets": "current_assets",
    "ifrs-full_NoncurrentAssets": "noncurrent_assets",
    "ifrs-full_CurrentLiabilities": "current_liabilities",
    "ifrs-full_NoncurrentLiabilities": "noncurrent_liabilities",
}
STOPPERS = set(SUBTOTALS) | {"ifrs-full_Assets", "ifrs-full_Liabilities", "ifrs-full_Equity", "ifrs-full_EquityAndLiabilities",
                             "ifrs-full_EquityAttributableToOwnersOfParent"}
HFS_ASSET_IDS = {"ifrs-full_NoncurrentAssetsOrDisposalGroupsClassifiedAsHeldForSale"}
HFS_LIABILITY_IDS = {"ifrs-full_LiabilitiesIncludedInDisposalGroupsClassifiedAsHeldForSale"}


def is_held_for_sale(row: dict) -> bool:
    return row["account_id"] in HFS_ASSET_IDS | HFS_LIABILITY_IDS or str(row["account_nm"]).strip().startswith("매각예정")


def _value(row: dict) -> Decimal:
    return Decimal(row["value"]) if row["value_status"] == "parsed" else Decimal(0)


def annotate_sections(rows: list[dict]) -> None:
    """재무상태표 행에 bs_section / bs_section_sum_ok를 붙인다 (보고서·열 단위로 따로 확인)."""
    groups: dict[tuple, list] = {}
    for r in rows:
        r.setdefault("bs_section", None)
        r.setdefault("bs_section_sum_ok", False)
        if r["sj_div"] == "BS":
            groups.setdefault((r["snapshot_id"], r["source_column"]), []).append(r)
    for group in groups.values():
        ordered = sorted(group, key=lambda r: int(r["ord"]))
        for i, subtotal in enumerate(ordered):
            if subtotal["account_id"] not in SUBTOTALS or subtotal["value_status"] != "parsed":
                continue
            members = []
            for r in ordered[i + 1:]:
                if r["account_id"] in STOPPERS:
                    break
                members.append(r)
            regular = sum((_value(r) for r in members if not is_held_for_sale(r)), Decimal(0))
            hfs = sum((_value(r) for r in members if is_held_for_sale(r)), Decimal(0))
            total = Decimal(subtotal["value"])
            confirmed = regular == total or (hfs != 0 and regular + hfs == total)
            for r in members:
                r["bs_section"] = SUBTOTALS[subtotal["account_id"]]
                r["bs_section_sum_ok"] = confirmed
                # 매각예정 행은 소계에 실제로 포함됐을 때만 그 구간으로 확인한다
                if is_held_for_sale(r):
                    r["bs_section_sum_ok"] = confirmed and regular != total


def held_for_sale_adjustment(rows: list[dict], kind: str) -> dict:
    """한 보고서·열의 유동자산(kind='assets') 또는 유동부채(kind='liabilities')의 매각예정항목 조정.

    포함: 매각예정 행이 유동 구간 안에 있고, 그 행을 더해야 소계와 같다
    미포함: 유동 구간 합이 매각예정 없이 소계와 같고, 유동 + 비유동 + 매각예정 = 총계로도 확인된다
    그 밖: 확인 필요. 조정값은 미포함이 확인된 경우에만 만든다.
    """
    ids = HFS_ASSET_IDS if kind == "assets" else HFS_LIABILITY_IDS
    current_id, noncurrent_id, total_id = (
        ("ifrs-full_CurrentAssets", "ifrs-full_NoncurrentAssets", "ifrs-full_Assets") if kind == "assets"
        else ("ifrs-full_CurrentLiabilities", "ifrs-full_NoncurrentLiabilities", "ifrs-full_Liabilities"))
    section = "current_assets" if kind == "assets" else "current_liabilities"
    pick = lambda aid: next((r for r in rows if r["sj_div"] == "BS" and r["account_id"] == aid and r["value_status"] == "parsed"), None)
    current, noncurrent, total = pick(current_id), pick(noncurrent_id), pick(total_id)
    hfs_rows = [r for r in rows if r["sj_div"] == "BS" and (r["account_id"] in ids or
                (kind == "assets" and str(r["account_nm"]).strip() in ("매각예정자산", "매각예정비유동자산")) or
                (kind == "liabilities" and str(r["account_nm"]).strip() in ("매각예정부채",)))]
    hfs = sum((_value(r) for r in hfs_rows), Decimal(0))
    out = {"reported": None if current is None else current["value"], "held_for_sale": str(hfs) if hfs_rows else None,
           "included": "해당 없음", "adjusted": None if current is None else current["value"], "evidence": ""}
    if current is None:
        return {**out, "included": "확인 필요", "adjusted": None, "evidence": "유동 소계 행 없음"}
    if not hfs_rows or hfs == 0:
        return out
    if hfs < 0:  # 자산·부채 잔액이 음수인 것은 원문 표기 자체를 확인해야 한다 (코스맥스 2025: 주석은 '매각예정자산 없음')
        return {**out, "included": "확인 필요", "adjusted": None, "evidence": f"매각예정 잔액이 음수 ({hfs:,}) — 원문 확인 전 조정하지 않음"}
    inside = [r for r in hfs_rows if r.get("bs_section") == section and r.get("bs_section_sum_ok")]
    if inside:
        return {**out, "included": "포함", "evidence": f"매각예정 {hfs:,}이 유동 구간 안에 있고, 구간 항목 합 + 매각예정 = 소계 {Decimal(current['value']):,}"}
    current_rows = [r for r in rows if r.get("bs_section") == section]
    section_ok = bool(current_rows) and all(r["bs_section_sum_ok"] for r in current_rows if not is_held_for_sale(r))
    identity = (noncurrent is not None and total is not None
                and Decimal(current["value"]) + Decimal(noncurrent["value"]) + hfs == Decimal(total["value"]))
    if section_ok and identity:
        adjusted = Decimal(current["value"]) + hfs
        return {**out, "included": "미포함", "adjusted": str(adjusted),
                "evidence": f"유동 구간 합 = 소계(매각예정 없이), 유동 {Decimal(current['value']):,} + 비유동 {Decimal(noncurrent['value']):,} + 매각예정 {hfs:,} = 총계 {Decimal(total['value']):,}"}
    return {**out, "included": "확인 필요", "adjusted": None, "evidence": "구간 합 또는 총계 항등식으로 포함 여부를 확인하지 못함"}
