#!/usr/bin/env bash
# Refresh the parcel-data GitHub Release mirror from TxGIO.
#
# WHY: CloudFront in front of data.geographic.texas.gov blocks GitHub Actions
# datacenter IPs, so the pipeline downloads parcel zips from this repo's
# release assets instead. Parcel data changes slowly (TxGIO publishes a new
# StratMap vintage roughly annually) — refresh quarterly, or whenever a new
# vintage lands.
#
# RUN FROM: a residential/office network (NOT a datacenter — TxGIO will 403).
# REQUIRES: gh CLI logged in with access to the repo; curl.
#
# Usage: bash scripts/refresh_parcel_mirror.sh
set -euo pipefail

REPO="PropertyPete1/LDR-Seller-Finder"
TAG="parcel-data-2025"   # bump when a new StratMap vintage is adopted (e.g. parcel-data-2026)
API="https://api.tnris.org/api/v1"
UA="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"

# county => TxGIO area_type_name
declare -A COUNTIES=( [bexar]="Bexar" [comal]="Comal" )

# Find the newest Land Parcels collection
CID=$(curl -s -A "$UA" "$API/collections?search=land%20parcels" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);r=[c for c in d['results'] if c['name']=='Land Parcels'];r.sort(key=lambda c:c.get('acquisition_date') or '',reverse=True);print(r[0]['collection_id'])")
echo "Newest Land Parcels collection: $CID"

WORKDIR=$(mktemp -d)
trap 'rm -rf "$WORKDIR"' EXIT

for county in "${!COUNTIES[@]}"; do
  area="${COUNTIES[$county]}"
  url=$(curl -s -A "$UA" "$API/resources?collection_id=$CID&area_type_name=$area" \
    | python3 -c "import sys,json;print(json.load(sys.stdin)['results'][0]['resource'])")
  echo "Downloading $county: $url"
  curl -L -A "$UA" --fail -o "$WORKDIR/${county}_parcels.zip" "$url"
  unzip -tq "$WORKDIR/${county}_parcels.zip" >/dev/null && echo "$county zip OK"
done

echo "Uploading to release $TAG (replacing existing assets)…"
gh release upload "$TAG" "$WORKDIR"/*.zip --repo "$REPO" --clobber
gh release view "$TAG" --repo "$REPO" --json assets \
  --jq '.assets[] | "\(.name)  \(.size) bytes  updated \(.updatedAt)"'
echo "Done. Next weekly run will use the refreshed data."
