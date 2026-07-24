"""Data source modules.

Each module exposes a single public entry point used by the pipeline:

  parcels.sync_county(conn, county)        — bulk parcel/owner data (TxGIO)
  exemptions.sync_county(conn, county)     — homestead exemption flags
  preforeclosure.fetch(county)             — notice-of-trustee-sale records
  divorce.fetch(county)                    — divorce filings (STUBBED, see module)

Add new sources as new modules; keep them independent so a failure in one
source never blocks the others.
"""
