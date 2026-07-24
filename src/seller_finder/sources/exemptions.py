"""Homestead exemption flags — Bexar County ArcGIS parcel layer.

Bexar County's public parcel MapServer exposes an `Exempts` field containing
exemption codes (e.g. "HS", "HS-OV65"). We page through the layer and store
the current exemption string per parcel, then detect *homestead removed*:
an exemption string that contained HS on the previous run but no longer does.

Comal has no free exemption feed — the module simply skips counties without
an `exemption_sources` entry in settings.yaml (documented in README).
"""
import logging
import time

import requests

from .. import config

LOGGER = logging.getLogger("sources.exemptions")

PAGE_SIZE = 1000
UA = {"User-Agent": "LDR-Seller-Finder/1.0 (public records research)"}


def _has_homestead(exempts: str) -> bool:
    codes = [c.strip().upper() for c in (exempts or "").replace("-", ",").split(",")]
    return "HS" in codes


def fetch_exemptions(county: str):
    """Yield (prop_id, exempts_string) for every parcel with any exemption."""
    src = config.SETTINGS.get("exemption_sources", {}).get(county)
    if not src:
        LOGGER.info("No exemption source configured for %s — skipping", county)
        return

    url = src["url"]
    prop_field = src.get("prop_id_field", "PropID")
    ex_field = src.get("exempts_field", "Exempts")
    offset = 0
    session = requests.Session()

    while True:
        params = {
            "where": f"{ex_field} IS NOT NULL AND {ex_field} <> ''",
            "outFields": f"{prop_field},{ex_field}",
            "returnGeometry": "false",
            "orderByFields": prop_field,
            "resultOffset": offset,
            "resultRecordCount": PAGE_SIZE,
            "f": "json",
        }
        for attempt in range(3):
            try:
                resp = session.get(url, params=params, headers=UA, timeout=120)
                resp.raise_for_status()
                data = resp.json()
                break
            except Exception as exc:  # noqa: BLE001
                if attempt == 2:
                    raise
                LOGGER.warning("Exemption page retry (%s): %s", offset, exc)
                time.sleep(5 * (attempt + 1))
        feats = data.get("features", [])
        if not feats:
            break
        for f in feats:
            attrs = f.get("attributes", {})
            prop_id = attrs.get(prop_field)
            if prop_id is None:
                continue
            # PropID comes back as float; normalize to int-string to match TxGIO Prop_ID
            prop_id = str(int(prop_id)) if isinstance(prop_id, float) else str(prop_id).strip()
            yield prop_id, str(attrs.get(ex_field) or "")
        offset += len(feats)
        if len(feats) < PAGE_SIZE:
            break
    LOGGER.info("Exemptions fetched for %s: %d rows", county, offset)


def sync_county(conn, county: str) -> dict:
    """Update parcels.exempts; return stats incl. homestead-removed prop_ids."""
    stats = {"county": county, "updated": 0, "homestead": 0, "homestead_removed": []}
    src = config.SETTINGS.get("exemption_sources", {}).get(county)
    if not src:
        return stats

    prev = {
        row["prop_id"]: row["exempts"]
        for row in conn.execute(
            "SELECT prop_id, exempts FROM parcels WHERE county=? AND exempts IS NOT NULL", (county,)
        )
    }

    seen_hs = set()
    batch = []
    for prop_id, exempts in fetch_exemptions(county):
        batch.append((exempts, county, prop_id))
        if _has_homestead(exempts):
            seen_hs.add(prop_id)
        if len(batch) >= 5000:
            conn.executemany(
                "UPDATE parcels SET exempts=? WHERE county=? AND prop_id=?", batch
            )
            stats["updated"] += len(batch)
            batch = []
    if batch:
        conn.executemany("UPDATE parcels SET exempts=? WHERE county=? AND prop_id=?", batch)
        stats["updated"] += len(batch)

    # Homestead removed = previously had HS, current pull says otherwise.
    for prop_id, old_exempts in prev.items():
        if _has_homestead(old_exempts) and prop_id not in seen_hs:
            stats["homestead_removed"].append(prop_id)

    stats["homestead"] = len(seen_hs)
    conn.commit()
    LOGGER.info(
        "Exemption sync %s: updated=%d homestead=%d removed=%d",
        county, stats["updated"], stats["homestead"], len(stats["homestead_removed"]),
    )
    return stats
