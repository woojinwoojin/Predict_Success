"""재무상태표 구간 확인과 매각예정항목 조정(docs/decisions.md D8) 테스트."""

from pathlib import Path

from src.normalization.account_map import load_rules, map_account
from src.normalization.bs_sections import annotate_sections, held_for_sale_adjustment
from src.normalization.dart_facts import account_key

CONFIG = load_rules(Path(__file__).resolve().parents[1] / "configs" / "account_map_v1.yaml")
NONSTD = "-표준계정코드 미사용-"


def bs(ord_, account_id, nm, value, snapshot="s", column="당기"):
    return {"row_id": f"{snapshot}#{ord_}#{column}", "snapshot_id": snapshot, "source_column": column, "sj_div": "BS", "ord": str(ord_),
            "account_id": account_id, "account_nm": nm, "account_detail": "-", "raw_amount": value,
            "value": value if value != "" else None, "value_status": "parsed" if value != "" else "empty_in_source"}


def balance_sheet(cl_items, ncl_items, ca_items=(("ifrs-full_Inventories", "재고자산", "100"),), hfs_in_ca=None, hfs_after=None):
    rows, o = [], 1
    ca_total = sum(int(v) for *_, v in ca_items) + (int(hfs_in_ca) if hfs_in_ca else 0)
    nca_total = 500
    total = ca_total + nca_total + (int(hfs_after) if hfs_after else 0)
    rows.append(bs(o, "ifrs-full_Assets", "자산총계", str(total))); o += 1
    rows.append(bs(o, "ifrs-full_CurrentAssets", "유동자산", str(ca_total))); o += 1
    for aid, nm, v in ca_items:
        rows.append(bs(o, aid, nm, v)); o += 1
    if hfs_in_ca:
        rows.append(bs(o, "ifrs-full_NoncurrentAssetsOrDisposalGroupsClassifiedAsHeldForSale", "매각예정자산", hfs_in_ca)); o += 1
    rows.append(bs(o, "ifrs-full_NoncurrentAssets", "비유동자산", str(nca_total))); o += 1
    rows.append(bs(o, "ifrs-full_PropertyPlantAndEquipment", "유형자산", str(nca_total))); o += 1
    if hfs_after:
        rows.append(bs(o, "ifrs-full_NoncurrentAssetsOrDisposalGroupsClassifiedAsHeldForSale", "매각예정자산", hfs_after)); o += 1
    cl_total = sum(int(v) for *_, v in cl_items)
    ncl_total = sum(int(v) for *_, v in ncl_items)
    rows.append(bs(o, "ifrs-full_Liabilities", "부채총계", str(cl_total + ncl_total))); o += 1
    rows.append(bs(o, "ifrs-full_CurrentLiabilities", "유동부채", str(cl_total))); o += 1
    for aid, nm, v in cl_items:
        rows.append(bs(o, aid, nm, v)); o += 1
    rows.append(bs(o, "ifrs-full_NoncurrentLiabilities", "비유동부채", str(ncl_total))); o += 1
    for aid, nm, v in ncl_items:
        rows.append(bs(o, aid, nm, v)); o += 1
    rows.append(bs(o, "ifrs-full_Equity", "자본총계", "0"))
    annotate_sections(rows)
    return rows


def find(rows, nm, section=None):
    return next(r for r in rows if r["account_nm"] == nm and (section is None or r["bs_section"] == section))


# ---------------------------------------------------------------- 구간 확인


def test_section_is_confirmed_only_when_items_sum_to_subtotal():
    rows = balance_sheet(cl_items=[(NONSTD, "사채", "40"), ("ifrs-full_TradeAndOtherCurrentPayables", "매입채무", "60")],
                         ncl_items=[("dart_BondsIssued", "사채", "300")])
    assert (find(rows, "사채", "current_liabilities")["bs_section_confirmed"], find(rows, "사채", "noncurrent_liabilities")["bs_section_confirmed"]) == (True, True)

    broken = [dict(r) for r in rows]
    next(r for r in broken if r["account_nm"] == "매입채무").update(value="61", raw_amount="61")
    annotate_sections_fresh = __import__("src.normalization.bs_sections", fromlist=["annotate_sections"]).annotate_sections
    for r in broken:
        r.pop("bs_section"); r.pop("bs_section_confirmed")
    annotate_sections_fresh(broken)
    assert find(broken, "매입채무")["bs_section_confirmed"] is False


def test_comparison_key_separates_current_and_noncurrent_bonds():
    rows = balance_sheet(cl_items=[(NONSTD, "사채", "40")], ncl_items=[(NONSTD, "사채", "300")])
    current, noncurrent = find(rows, "사채", "current_liabilities"), find(rows, "사채", "noncurrent_liabilities")
    assert account_key(current) != account_key(noncurrent)
    assert account_key(current)[1].endswith("@current_liabilities")


def test_ambiguous_account_without_confirmed_section_is_not_compared():
    row = bs(9, "ifrs-full_BondsIssued", "사채", "40")
    row.update(bs_section="current_liabilities", bs_section_confirmed=False)
    assert account_key(row) is None
    explicit = bs(9, "ifrs-full_ShorttermBorrowings", "단기차입금", "40")  # 코드가 유동을 명시하면 구간 없이도 비교한다
    assert account_key(explicit) == ("BS", "ifrs-full_ShorttermBorrowings", "-")


# ---------------------------------------------------------------- 단기 이자부채와 구간


def debt(rows):
    return map_account("short_term_interest_bearing_debt", CONFIG["accounts"]["short_term_interest_bearing_debt"], rows, CONFIG)


def test_bond_in_confirmed_current_section_counts_and_noncurrent_does_not():
    rows = balance_sheet(cl_items=[("ifrs-full_BondsIssued", "사채", "40"), ("ifrs-full_ShorttermBorrowings", "단기차입금", "60")],
                         ncl_items=[("ifrs-full_BondsIssued", "사채", "300")])
    result = debt(rows)
    assert (result["status"], result["value"]) == ("mapped", "100")


def test_bond_with_unconfirmed_section_is_a_review_candidate():
    rows = balance_sheet(cl_items=[("ifrs-full_BondsIssued", "사채", "40")], ncl_items=[])
    for r in rows:
        if r["account_nm"] == "사채":
            r["bs_section_confirmed"] = False
    assert debt(rows)["status"] == "review_needed"


# ---------------------------------------------------------------- 매각예정항목


def test_held_for_sale_inside_current_subtotal_is_not_added_again():
    rows = balance_sheet(cl_items=[], ncl_items=[], hfs_in_ca="20")
    adj = held_for_sale_adjustment(rows, "assets")
    assert (adj["included"], adj["reported"], adj["adjusted"]) == ("포함", "120", "120")


def test_held_for_sale_outside_current_subtotal_is_added_with_evidence():
    rows = balance_sheet(cl_items=[], ncl_items=[], hfs_after="20")
    adj = held_for_sale_adjustment(rows, "assets")
    assert (adj["included"], adj["reported"], adj["adjusted"]) == ("미포함", "100", "120")
    assert "총계" in adj["evidence"]


def test_negative_held_for_sale_balance_is_not_adjusted():
    rows = balance_sheet(cl_items=[], ncl_items=[], hfs_after="-5")
    adj = held_for_sale_adjustment(rows, "assets")
    assert adj["included"] == "확인 필요"
    assert adj["adjusted"] is None


def test_no_held_for_sale_means_reported_equals_adjusted():
    adj = held_for_sale_adjustment(balance_sheet(cl_items=[], ncl_items=[]), "assets")
    assert (adj["included"], adj["adjusted"]) == ("해당 없음", "100")
