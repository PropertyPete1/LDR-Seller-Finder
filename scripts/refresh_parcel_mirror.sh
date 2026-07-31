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
# REQUIRES: gh CLI logged in with access to the repo; curl; unzip; python3.
#
# Usage:
#   bash scripts/refresh_parcel_mirror.sh              # all enabled counties
#   bash scripts/refresh_parcel_mirror.sh bexar comal  # just these
#
# PORTABILITY: this runs on macOS. macOS ships bash 3.2 (2007), which has no
# associative arrays — `declare -A` fails outright there with "invalid option".
# The county list is a plain space-separated "name:AreaName" list so the script
# works on the machine the README actually tells you to run it from.
set -euo pipefail

REPO="PropertyPete1/LDR-Seller-Finder"
TAG="parcel-data-2025"   # bump when a new StratMap vintage is adopted (e.g. parcel-data-2026)
API="https://api.tnris.org/api/v1"
UA="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"

# county:TxGIO area_type_name — must match `counties` in config/settings.yaml.
# Add the scaffolded ones here when you enable them in settings.yaml:
#   dallas:Dallas tarrant:Tarrant harris:Harris
COUNTIES="bexar:Bexar comal:Comal travis:Travis"

# Optional CLI filter: refresh only the named counties.
if [ "$#" -gt 0 ]; then
  filtered=""
  for want in "$@"; do
    for pair in $COUNTIES; do
      case "$pair" in
        "$want":*) filtered="$filtered $pair" ;;
      esac
    done
  done
  if [ -z "$filtered" ]; then
    echo "No known county matched: $*" >&2
    echo "Known: $COUNTIES" >&2
    exit 1
  fi
  COUNTIES="$filtered"
fi

for tool in curl unzip python3 gh; do
  command -v "$tool" >/dev/null 2>&1 || { echo "Missing required tool: $tool" >&2; exit 1; }
done

# Find the newest Land Parcels collection
CID=$(curl -s -A "$UA" "$API/collections?search=land%20parcels" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);r=[c for c in d['results'] if c['name']=='Land Parcels'];r.sort(key=lambda c:c.get('acquisition_date') or '',reverse=True);print(r[0]['collection_id'])")
echo "Newest Land Parcels collection: $CID"

WORKDIR=$(mktemp -d)
trap 'rm -rf "$WORKDIR"' EXIT

for pair in $COUNTIES; do
  county="${pair%%:*}"
  area="${pair#*:}"
  url=$(curl -s -A "$UA" "$API/resources?collection_id=$CID&area_type_name=$area" \
    | python3 -c "import sys,json;print(json.load(sys.stdin)['results'][0]['resource'])")
  echo "Downloading $county: $url"
  curl -L -A "$UA" --fail -o "$WORKDIR/${county}_parcels.zip" "$url"
  # Verify before uploading: a truncated or HTML-error-page "zip" replacing a
  # good mirror asset would break every run until the next manual refresh.
  unzip -tq "$WORKDIR/${county}_parcels.zip" >/dev/null
  echo "$county zip OK ($(wc -c < "$WORKDIR/${county}_parcels.zip") bytes)"
done

echo "Uploading to release $TAG (replacing existing assets)…"
gh release upload "$TAG" "$WORKDIR"/*.zip --repo "$REPO" --clobber
gh release view "$TAG" --repo "$REPO" --json assets \
  --jq '.assets[] | "\(.name)  \(.size) bytes  updated \(.updatedAt)"'
echo
echo "Done. The next run picks up the new data automatically: the asset id+size"
echo "changes, so parcel_snapshot_meta sees a new snapshot and the daily light"
echo "sync re-runs owner-change bookkeeping instead of skipping it."
