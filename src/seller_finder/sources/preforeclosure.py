"""Pre-foreclosure / Notice of Trustee Sale — Bexar County Clerk.

The Bexar County Clerk publishes the current month's foreclosure-sale notices
through a public ArcGIS service (the same data behind the county's
Foreclosure Map at maps.bexar.org/foreclosures). Two layers:
  layer 0 = Mortgage foreclosures, layer 1 = Tax foreclosures.

Fields: ADDRESS, DOC_NUMBER, YEAR, MONTH, TYPE, CITY, ZIP. There's no owner
name in the feed, so the pipeline matches ADDRESS against parcel situs
addresses to attach the owner.

Comal County posts notices only at the courthouse/physical kiosk — no clean
digital feed found, so this module is Bexar-only for now (see README).
"""
import logging

import requests

from .. import config
from ..sources.parcels import normalize_addr

LOGGER = logging.getLogger("sources.preforeclosure")

UA = {"User-Agent": "LDR-Seller-Finder/1.0 (public records research)"}


def fetch(county: str) -> list[dict]:
    """Return current-month notices: [{address, doc_number, year, month, kind, city, zip}]."""
    src = config.SETTINGS.get("foreclosure_sources", {}).get(county)
    if not src:
        LOGGER.info("No foreclosure source configured for %s — skipping", county)
        return []

    notices = []
    for kind, key in (("mortgage", "mortgage_url"), ("tax", "tax_url")):
        url = src.get(key)
        if not url:
            continue
        params = {
            "where": "1=1",
            "outFields": "ADDRESS,DOC_NUMBER,YEAR,MONTH,TYPE,CITY,ZIP",
            "returnGeometry": "false",
            "f": "json",
        }
        try:
            resp = requests.get(url, params=params, headers=UA, timeout=120)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            LOGGER.error("Foreclosure fetch failed (%s %s): %s", county, kind, exc)
            continue
        for f in data.get("features", []):
            a = f.get("attributes", {})
            if not a.get("ADDRESS"):
                continue
            notices.append({
                "address": str(a["ADDRESS"]).strip(),
                "doc_number": str(a.get("DOC_NUMBER") or ""),
                "year": a.get("YEAR"),
                "month": a.get("MONTH"),
                "kind": kind,
                "city": str(a.get("CITY") or "").strip(),
                "zip": str(a.get("ZIP") or "").strip(),
            })
    LOGGER.info("Foreclosure notices for %s: %d", county, len(notices))
    return notices


def match_to_parcels(conn, county: str, notices: list[dict]) -> list[dict]:
    """Attach prop_id/owner to each notice by matching the property address.

    Match strategy: normalized address head (number + street) + zip.
    """
    if not notices:
        return []

    # Build lookup of parcel situs addresses for the county.
    lookup: dict[str, dict] = {}
    for row in conn.execute(
        "SELECT prop_id, owner_name, situs_addr, situs_zip, mail_addr FROM pc.parcels WHERE county=?",
        (county,),
    ):
        key = _addr_key(row["situs_addr"], row["situs_zip"])
        if key:
            lookup[key] = dict(row)

    matched = []
    for n in notices:
        key = _addr_key(n["address"], n["zip"])
        hit = lookup.get(key)
        if hit:
            matched.append({**n, "prop_id": hit["prop_id"], "owner_name": hit["owner_name"]})
    LOGGER.info("Foreclosure notices matched to parcels: %d/%d", len(matched), len(notices))
    return matched


def _addr_key(addr: str, zip_code: str) -> str:
    norm = normalize_addr(addr or "")
    if not norm:
        return ""
    head = " ".join(norm.split()[:3])
    return f"{head}|{(zip_code or '')[:5]}"
