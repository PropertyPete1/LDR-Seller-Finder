"""Homestead exemption flags — Bexar County ArcGIS parcel layer.

Bexar County's public parcel MapServer exposes an `Exempts` field containing
exemption codes (e.g. "HS", "HS-OV65"). We page through the layer and store
the current exemption string per parcel, then detect *homestead removed*:
an exemption string that contained HS on the previous run but no longer does.

Comal has no free exemption feed — the module simply skips counties without
an `exemption_sources` entry in settings.yaml (documented in README).
"""
import logging

import requests

from .. import config
from ..arcgis import ArcGISError
from ..arcgis import query as arcgis_query
from ..state import now_iso

LOGGER = logging.getLogger("sources.exemptions")

PAGE_SIZE = 1000


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
        # Raises ArcGISError on transport failure AND on the HTTP-200 error
        # bodies ArcGIS uses (see arcgis.py). Letting one of those become an
        # empty page would end the paging loop early and feed a truncated
        # snapshot straight into homestead-removed detection.
        data = arcgis_query(session, url, params, timeout=120, attempts=3)
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
    """Refresh the compact exempt_parcels snapshot (committed DB); return
    stats incl. homestead-removed prop_ids.

    exempt_parcels holds only parcels that currently carry exemptions
    (~405K for Bexar, a few MB) — exactly the derived state needed to diff
    homestead status between runs without committing raw parcel records.
    """
    stats = {"county": county, "updated": 0, "homestead": 0, "homestead_removed": []}
    src = config.SETTINGS.get("exemption_sources", {}).get(county)
    if not src:
        return stats

    prev = {
        row["prop_id"]: row["exempts"]
        for row in conn.execute(
            "SELECT prop_id, exempts FROM exempt_parcels WHERE county=?", (county,)
        )
    }

    ts = now_iso()
    seen_hs = set()
    current: list[tuple] = []
    for prop_id, exempts in fetch_exemptions(county):
        current.append((county, prop_id, exempts, ts))
        if _has_homestead(exempts):
            seen_hs.add(prop_id)

    # Defensive: homestead_removed is computed as "in the previous snapshot but
    # not in this one", so a SHORT pull is as dangerous as an empty one — an
    # ArcGIS page that stops early (server-side maxRecordCount, a truncated
    # response) would mark thousands of parcels homestead-removed. +10 is
    # enough to lift an absentee lead from 30 to the 40 trace threshold, so a
    # truncated pull turns straight into skip-trace spend and FUB pushes.
    # Require the new snapshot to be within a sane fraction of the old one
    # before trusting a removal diff.
    min_ratio = float(config.SETTINGS.get("exemption_min_snapshot_ratio", 0.5))
    if prev and len(current) < len(prev) * min_ratio:
        LOGGER.warning(
            "Exemption feed for %s returned %d rows vs %d previously (< %.0f%% "
            "of the previous snapshot) — looks truncated. Keeping previous "
            "snapshot, skipping homestead-removed detection.",
            county, len(current), len(prev), min_ratio * 100)
        stats["truncated_feed"] = True
        return stats

    # Homestead removed = previously had HS, current pull says otherwise.
    for prop_id, old_exempts in prev.items():
        if _has_homestead(old_exempts) and prop_id not in seen_hs:
            stats["homestead_removed"].append(prop_id)

    # Replace this county's snapshot with the current pull.
    conn.execute("DELETE FROM exempt_parcels WHERE county=?", (county,))
    conn.executemany(
        "INSERT OR REPLACE INTO exempt_parcels (county, prop_id, exempts, last_seen_at) "
        "VALUES (?,?,?,?)", current)
    stats["updated"] = len(current)
    stats["homestead"] = len(seen_hs)
    conn.commit()
    LOGGER.info(
        "Exemption sync %s: updated=%d homestead=%d removed=%d",
        county, stats["updated"], stats["homestead"], len(stats["homestead_removed"]),
    )
    return stats
