"""BatchData / BatchSkipTracing provider.

API: POST https://api.batchdata.com/api/v1/property/skip-trace
     (verified against developer.batchdata.com, July 2026)
Auth: Authorization: Bearer <BATCHDATA_API_KEY>
Up to 100 properties per request; billed per MATCHED result, so caching in
the state DB (never trace the same owner twice) directly controls cost.
TCPA-blacklisted phones are filtered out by BatchData by default — we keep
that default and also store dnc/litigator flags.

Error handling contract:
  * The raw HTTP status and response body are logged for every request so
    failures are visible in the Actions log (the API key is never logged).
  * API errors (401/402/403/422/429/5xx, network failures) are returned as
    results with `error` set — the tracer must NOT cache these or mark the
    lead traced. 403 usually means the API token lacks the "Property Skip
    Trace" endpoint permission or the PayGo wallet is empty (per BatchData's
    own troubleshooting guide).
  * 429 (rate limit) is retried with exponential backoff before giving up.
"""
import logging
import time

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

        data, err = self._post_with_retry(body, len(chunk))
        if err is not None:
            # API-level failure: every request in this chunk is an ERROR, not a
            # no-match. The tracer must not cache these results.
            return [SkipTraceResult(provider=self.name, error=err) for _ in chunk]

        # A 200 does not by itself mean success: BatchData wraps its own status
        # in the body, and a token/permission problem can arrive as
        # {"status": {"code": 401, ...}} under HTTP 200. Without this check the
        # chunk falls through to the no-match path below and gets CACHED as
        # genuine no-matches — the exact shape of the 403-became-64-no-matches
        # bug, just one layer down.
        body_err = self._body_level_error(data, len(chunk))
        if body_err:
            LOGGER.error("BatchData HTTP 200 but body reports failure (%d items): %s",
                         len(chunk), body_err)
            return [SkipTraceResult(provider=self.name, error=body_err) for _ in chunk]

        persons = data.get("results", {}).get("persons", [])
        # meta.results = {requestCount, matchCount, noMatchCount, errorCount}
        meta = (data.get("results", {}) or {}).get("meta", {}) or {}
        counts = meta.get("results") if isinstance(meta.get("results"), dict) else meta
        LOGGER.info(
            "BatchData OK: sent=%d matchCount=%s noMatchCount=%s errorCount=%s",
            len(chunk), counts.get("matchCount", "?"),
            counts.get("noMatchCount", "?"), counts.get("errorCount", "?"))
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

    def _post_with_retry(self, body: dict, n_items: int,
                         max_attempts: int = 3) -> tuple[dict | None, str | None]:
        """POST to the API; retry 429/5xx with backoff. Returns (data, error).

        Logs the raw HTTP status + response body on every failure so the
        Actions log shows exactly what BatchData said (key never logged).
        """
        last_err = "unknown error"
        for attempt in range(1, max_attempts + 1):
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
            except Exception as exc:  # noqa: BLE001 — network-level failure
                last_err = f"network error: {exc}"
                LOGGER.error("BatchData request failed (attempt %d/%d, %d items): %s",
                             attempt, max_attempts, n_items, last_err)
                time.sleep(2 ** attempt)
                continue

            body_text = (resp.text or "")[:2000]
            if resp.status_code == 200:
                try:
                    return resp.json(), None
                except ValueError:
                    last_err = f"HTTP 200 but non-JSON body: {body_text}"
                    LOGGER.error("BatchData response parse failed: %s", last_err)
                    return None, last_err

            last_err = f"HTTP {resp.status_code}: {body_text}"
            LOGGER.error(
                "BatchData API error (attempt %d/%d, %d items) — status=%d body=%s",
                attempt, max_attempts, n_items, resp.status_code, body_text,
            )
            if resp.status_code == 403:
                LOGGER.error(
                    "HINT: BatchData 403 usually means the API token is missing the "
                    "'Property Skip Trace' endpoint permission (dashboard → API Tokens "
                    "→ View/Update) or the PayGo wallet has no balance.")
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < max_attempts:
                time.sleep(5 * 2 ** (attempt - 1))
                continue
            break  # 4xx (non-429) won't succeed on retry
        return None, last_err

    @staticmethod
    def _body_level_error(data: dict, n_items: int) -> str | None:
        """Detect a failure reported inside a HTTP 200 body. None = healthy.

        Two signals, both conservative — a normal all-no-match response (which
        we DO want cached) reports code 200 and errorCount 0, so it passes.
        """
        if not isinstance(data, dict):
            return f"HTTP 200 but response was {type(data).__name__}, not an object"
        status = data.get("status")
        if isinstance(status, dict):
            code = status.get("code")
            if isinstance(code, int) and code >= 400:
                text = status.get("text") or status.get("message") or ""
                return f"HTTP 200 but body status.code={code}: {str(text)[:300]}"
        meta = (data.get("results") or {}).get("meta") or {}
        counts = meta.get("results") if isinstance(meta.get("results"), dict) else meta
        if isinstance(counts, dict):
            try:
                errors = int(counts.get("errorCount") or 0)
            except (TypeError, ValueError):
                errors = 0
            if errors and errors >= n_items:
                return (f"HTTP 200 but provider reported errorCount={errors} "
                        f"for all {n_items} requested records")
        return None

    @staticmethod
    def _addr_key(street: str, zip_code: str) -> str:
        return f"{' '.join((street or '').upper().split())}|{(zip_code or '')[:5]}"
