#!/usr/bin/env python3
"""Import deed dates from a BCAD appraisal-export CSV to unlock the
"owned 10+ years" scoring signal.

How to get the data (free):
  1. Email openrecords@bcad.org (or use https://help.bcad.org, request type
     "Open Records") asking for the current *appraisal data export* via their
     FTP server. Current-year data products are free.
  2. The export contains deed/ownership records. Extract prop_id + deed date
     into a CSV with headers: county,prop_id,deed_date  (deed_date ISO YYYY-MM-DD)
  3. Run:  python3 scripts/import_deed_dates.py path/to/deed_dates.csv
  4. Commit is not needed — the table lives in the encrypted state DB. Run
     this locally with the decrypted DB, or drop the CSV in data/inbox/ and
     import during a workflow_dispatch run.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from seller_finder.state import get_db  # noqa: E402
from seller_finder.sources import deeds  # noqa: E402


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 1
    path = Path(sys.argv[1])
    conn = get_db()
    n = deeds.ingest_file(conn, path)
    print(f"Imported {n} deed dates. 'Owned 10+ years' signal is now active for these parcels.")
    print("Tip: you can also drop deeds_*.csv into data/inbox/ — the weekly run imports it automatically.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
