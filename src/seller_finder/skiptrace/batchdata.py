"""BatchData / BatchSkipTracing provider.

API: POST https://api.batchdata.com/api/v1/property/skip-trace
Auth: Authorization: Bearer <BATCHDATA_API_KEY>
Up to 100 properties per request; billed per MATCHED result, so caching in
the state DB (never trace the same owner twice) directly controls cost.
TCPA-blacklisted phones are filtered out by BatchData by default — we keep
that default and also store dnc/litigator flags.
"""
import logging

import requests

from .. import config
from .base import SkipTraceProvider, SkipTraceRequest, SkipTraceResult

LOGGER = logging.getLogger("skiptrace.batchdata")

API_URL = "https://api.batchdata.com/api/v1/property/skip-trace"
BATCH_SIZE = 100


class BatchDataProvider(SkipTraceProvider):
    name = "batchdata"

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or config.BATCHDATA_API_KEY
        if not self.api_key:
            LOGGER.warning("BATCHDATA_API_KEY not set — traces will fail until configured")

    def trace_batch(self, requests_: list[SkipTraceRequest]) -> list[SkipTraceResult]:
        results: list[SkipTraceResult] = []
        for i in range(0, len(requests_), BATCH_SIZE):
            chunk = requests_[i:i + BATCH_SIZE]
            results.extend(self._trace_chunk(chunk))
        return results

    def _trace_chunk(self, chunk: list[SkipTraceRequest]) -> list[SkipTraceResult]:
        body = {"requests": []}
        for r in chunk:
            item: dict = {
                "propertyAddress": {
                    "street": r.street, "city": r.city, "state": r.state, "zip": r.zip,
                }
            }
            if r.owner_first or r.owner_last:
                item["name"] = {"first": r.owner_first, "last": r.owner_last}
            body["requests"].append(item)

        try:
            resp = requests.post(
                API_URL,
                json=body,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                timeout=300,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            LOGGER.error("BatchData request failed: %s", exc)
            return [SkipTraceResult(provider=self.name) for _ in chunk]

        persons = data.get("results", {}).get("persons", [])
        # Index persons by property-address hash-ish key so results align.
        by_addr: dict[str, dict] = {}
        for p in persons:
            addr = p.get("propertyAddress", {}) or {}
            key = self._addr_key(addr.get("street", ""), addr.get("zip", ""))
            by_addr.setdefault(key, p)

        out = []
        for r in chunk:
            p = by_addr.get(self._addr_key(r.street, r.zip))
            if not p or not (p.get("meta", {}) or {}).get("matched"):
                out.append(SkipTraceResult(provider=self.name))
                continue
            out.append(SkipTraceResult(
                matched=True,
                emails=[e.get("email") for e in (p.get("emails") or []) if e.get("email")],
                phones=[{
                    "number": ph.get("number"),
                    "type": ph.get("type"),
                    "score": ph.get("score"),
                    "reachable": ph.get("reachable"),
                } for ph in (p.get("phoneNumbers") or []) if ph.get("number")],
                dnc=bool((p.get("dnc") or {})),
                litigator=bool(p.get("litigator")),
                raw=p,
                provider=self.name,
            ))
        return out

    @staticmethod
    def _addr_key(street: str, zip_code: str) -> str:
        return f"{' '.join((street or '').upper().split())}|{(zip_code or '')[:5]}"
