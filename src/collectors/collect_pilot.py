"""파일럿 원본 수집: python -m src.collectors.collect_pilot

공시검색 → 사업보고서 원문(최초본·정정본) → 재무 API(연결·별도) 순서로 받는다.
재무 API를 마지막에 받아야 응답의 접수번호를 이미 받은 원문과 연결할 수 있다.
실행 요약은 data/raw/dart/_runs/{run_id}.json에 남긴다.
"""

import json
import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

from src.collectors.dart import DartClient, RawStore, fetch_filing, fetch_financials, list_annual_reports, new_run_id, utc_now

ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    load_dotenv(ROOT / ".env")
    config = yaml.safe_load((ROOT / "configs" / "pilot_v1.yaml").read_text(encoding="utf-8"))
    client = DartClient(os.getenv("DART_API_KEY", ""))
    store = RawStore(ROOT / "data" / "raw" / "dart")
    run_id = new_run_id()
    summary = {"run_id": run_id, "config": config["version"], "started_at": utc_now(), "companies": []}

    for company in config["companies"]:
        code = company["corp_code"]
        filings = list_annual_reports(client, store, code, config["bsns_years"], run_id)
        filing_results = [fetch_filing(client, store, code, f, run_id) for f in filings]
        financials = [
            fetch_financials(client, store, code, year, config["reprt_code"], fs, run_id)
            for year in config["bsns_years"] for fs in config["fs_divs"]
        ]
        years_found = sorted({f["bsns_year"] for f in filings})
        summary["companies"].append({
            "corp_code": code, "name": company["name"],
            "filings": [{"rcept_no": f["rcept_no"], "report_nm": f["report_nm"], "bsns_year": f["bsns_year"], "rcept_dt": f["rcept_dt"]} for f in filings],
            "missing_years": [y for y in config["bsns_years"] if y not in years_found],
            "filing_results": filing_results, "financials": financials,
        })
        print(f"{company['name']}: 보고서 {len(filings)}건, 재무 응답 {len(financials)}건", flush=True)

    summary["finished_at"] = utc_now()
    runs = store.root / "_runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / f"{run_id}.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"요약: {runs / (run_id + '.json')}")


if __name__ == "__main__":
    main()
