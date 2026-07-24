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
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from seller_finder.state import get_db  # noqa: E402


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 1
    path = Path(sys.argv[1])
    conn = get_db()
    conn.execute(
        """CREATE TABLE IF NOT EXISTS deed_dates (
               county TEXT NOT NULL, prop_id TEXT NOT NULL, deed_date TEXT,
               PRIMARY KEY (county, prop_id))"""
    )
    n = 0
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            row = {k.lower().strip(): (v or "").strip() for k, v in row.items()}
            if not row.get("prop_id") or not row.get("deed_date"):
                continue
            conn.execute(
                "INSERT OR REPLACE INTO deed_dates (county, prop_id, deed_date) VALUES (?,?,?)",
                (row.get("county", "bexar").lower(), row["prop_id"], row["deed_date"]),
            )
            n += 1
    conn.commit()
    print(f"Imported {n} deed dates. 'Owned 10+ years' signal is now active for these parcels.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
