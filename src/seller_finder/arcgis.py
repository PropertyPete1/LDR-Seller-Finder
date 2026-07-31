"""Shared ArcGIS REST client discipline.

ArcGIS Server does NOT use HTTP status codes to report query failures. A bad
field name, an expired service, a rebuilt layer, or a server-side exception all
come back as **HTTP 200** with an error object in the body:

    {"error": {"code": 400, "message": "Unable to complete operation",
               "details": ["Invalid field: ADDRESS"]}}

`resp.raise_for_status()` is happy with that, and `data.get("features", [])`
turns it into an empty list — which is indistinguishable from "this county
genuinely has no foreclosure notices this month" or "no parcel carries an
exemption". Both of those readings are wrong and both fail silently:

  * preforeclosure: the whole signal disappears for that county and the run
    still reports success.
  * exemptions: an empty pull is diffed against the previous snapshot, which
    is exactly the mass-homestead-removed scenario the truncation guard exists
    to prevent.

This is the same defect class as the BatchData "403 became 64 cached
no-matches" incident, one layer out: a failed lookup must never be recorded as
a successful negative result. Every ArcGIS query in this repo goes through
`query()` so the check cannot be forgotten at a new call site.
"""
import logging
import time

import requests

LOGGER = logging.getLogger("arcgis")

UA = {"User-Agent": "LDR-Seller-Finder/1.0 (public records research)"}


class ArcGISError(RuntimeError):
    """An ArcGIS query failed — transport error, HTTP error, or a 200 whose
    body carries an `error` object. Never means "zero results"."""


def body_error(data) -> str | None:
    """Return a description of the error reported inside a 200 body, else None.

    Conservative on purpose: a healthy response with zero features has no
    `error` key and passes straight through, so genuine empty results are
    still allowed to mean zero.
    """
    if not isinstance(data, dict):
        return f"expected a JSON object, got {type(data).__name__}"
    err = data.get("error")
    if isinstance(err, dict):
        details = err.get("details") or []
        detail_txt = f" ({'; '.join(str(d) for d in details)})" if details else ""
        return (f"ArcGIS error code={err.get('code')} "
                f"message={err.get('message')!r}{detail_txt}")
    if isinstance(err, str) and err:
        return f"ArcGIS error: {err}"
    # Some deployments answer with {"status": "error", "messages": [...]}.
    if str(data.get("status", "")).lower() == "error":
        return f"ArcGIS status=error messages={data.get('messages')}"
    return None


def query(session: requests.Session | None, url: str, params: dict,
          timeout: int = 120, attempts: int = 1,
          backoff: float = 5.0) -> dict:
    """Run one ArcGIS query and return the parsed body, or raise ArcGISError.

    attempts > 1 retries transport/HTTP failures with linear backoff. A
    body-level error is NOT retried: it means the query itself is wrong (bad
    field, missing layer), so retrying only burns time.
    """
    get = (session or requests).get
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            resp = get(url, params=params, headers=UA, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001 — transport/HTTP/JSON
            last = exc
            if attempt < attempts:
                LOGGER.warning("ArcGIS query retry %d/%d for %s: %s",
                               attempt, attempts, url, exc)
                time.sleep(backoff * attempt)
                continue
            raise ArcGISError(f"ArcGIS request failed for {url}: {exc}") from exc

        err = body_error(data)
        if err:
            # HTTP 200 with a failure inside. Do not retry, do not return [].
            raise ArcGISError(f"{url}: {err}")
        return data
    raise ArcGISError(f"ArcGIS request failed for {url}: {last}")  # unreachable
