#!/bin/bash
set -euo pipefail

# Create any missing labels from the configured friendly-label mapping.
# Existing labels are preserved so cards and custom workflows are not damaged.
# Required environment: NEXTCLOUD_DECK_BASE_URL, NEXTCLOUD_DECK_USERNAME,
# NEXTCLOUD_DECK_APP_PASSWORD, DECK_BOARD_ID. Optional: DECK_CONFIG.

: "${NEXTCLOUD_DECK_BASE_URL:?Set NEXTCLOUD_DECK_BASE_URL}"
: "${NEXTCLOUD_DECK_USERNAME:?Set NEXTCLOUD_DECK_USERNAME}"
: "${NEXTCLOUD_DECK_APP_PASSWORD:?Set NEXTCLOUD_DECK_APP_PASSWORD}"
: "${DECK_BOARD_ID:?Set DECK_BOARD_ID}"

BASE_URL="${NEXTCLOUD_DECK_BASE_URL%/}"
BASE_URL="${BASE_URL%/index.php}"
case "$BASE_URL" in
  https://*) ;;
  *) echo "Refusing non-HTTPS Nextcloud URL." >&2; exit 2 ;;
esac
API="$BASE_URL/index.php/apps/deck/api/v1.0"
CONFIG="${DECK_CONFIG:-}"

# Keep the app password out of process arguments; curl reads it from a private
# temporary config file that is removed when this script exits.
CURL_CONFIG="$(mktemp)"
RESPONSE="$(mktemp)"
umask 077
chmod 600 "$CURL_CONFIG" "$RESPONSE"
trap 'rm -f "$CURL_CONFIG" "$RESPONSE"' EXIT
CREDENTIALS="${NEXTCLOUD_DECK_USERNAME}:${NEXTCLOUD_DECK_APP_PASSWORD}"
CREDENTIALS="${CREDENTIALS//\\/\\\\}"
CREDENTIALS="${CREDENTIALS//\"/\\\"}"
printf 'user = "%s"\n' "$CREDENTIALS" > "$CURL_CONFIG"

curl_request() {
  curl --config "$CURL_CONFIG" --silent --show-error --fail "$@"
}

read_mapping() {
  if [ -n "$CONFIG" ] && [ -f "$CONFIG" ]; then
    python3 - "$CONFIG" <<'PYEOF'
import re
import sys

mapping = []
key = None
with open(sys.argv[1], encoding="utf-8") as stream:
    in_block = False
    for line in stream:
        stripped = line.rstrip()
        if re.match(r"^\s*label_mapping:\s*$", stripped):
            in_block = True
            continue
        if not in_block:
            continue
        match = re.match(r"^\s+([a-z]+:[a-z]+):\s*$", stripped)
        if match:
            key = match.group(1)
            continue
        match = re.match(r'^\s+title:\s*"(.+)"\s*$', stripped)
        if match and key:
            mapping.append([key, match.group(1), None])
            continue
        match = re.match(r'^\s+color:\s*"?([0-9A-Fa-f]{6})"?\s*$', stripped)
        if match and mapping and mapping[-1][2] is None:
            mapping[-1][2] = match.group(1)
            continue
        if stripped and not stripped.startswith(" "):
            in_block = False
for entry in mapping:
    if entry[2]:
        print("|".join(entry))
PYEOF
  else
    cat <<'MAPPING'
phase:plan|💡 Planung|FAD7A0
phase:execute|🚀 In Umsetzung|AED6F1
approval:required|⌛ Freigabe nötig|FCF3CF
approval:approved|✔️ Freigabe erteilt|D4EFDF
risk:low|🟢 Risiko: Gering|A9DFBF
risk:high|⚠️ Risiko: Hoch|FADBD8
type:implementation|🛠️ Umsetzungsaufgabe|D6EAF8
type:documentation|📚 Dokumentation|D7BDE2
MAPPING
  fi
}

read_labels() {
  python3 - "$1" <<'PYEOF'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    data = json.load(stream)
for label in data.get("labels", []):
    print(f"{label['id']}|{label['title']}|{label.get('color', '')}")
PYEOF
}

label_exists() {
  python3 - "$1" "$2" <<'PYEOF'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    data = json.load(stream)
raise SystemExit(0 if any(label.get("title") == sys.argv[2] for label in data.get("labels", [])) else 1)
PYEOF
}

MAPPING="$(read_mapping)"
echo "=== Mapping (${CONFIG:-defaults}) ==="
printf '%s\n' "$MAPPING"
echo
echo "=== Existing board labels ==="
curl_request -o "$RESPONSE" "$API/boards/$DECK_BOARD_ID"
read_labels "$RESPONSE"
echo
echo "=== Create missing labels (existing labels are preserved) ==="
while IFS='|' read -r key title color; do
  [ -n "$key" ] || continue
  if label_exists "$RESPONSE" "$title"; then
    printf "Label '%s' already exists; leaving it unchanged.\n" "$title"
    continue
  fi
  PAYLOAD="$(python3 -c "import json,sys; print(json.dumps({'title': sys.argv[1], 'color': sys.argv[2]}))" "$title" "$color")"
  STATUS="$(curl_request -X POST "$API/boards/$DECK_BOARD_ID/labels" \
    -H 'Content-Type: application/json' \
    -d "$PAYLOAD" \
    -o /dev/null -w '%{http_code}')"
  printf "Label '%s' created: HTTP %s\n" "$title" "$STATUS"
done <<< "$MAPPING"
echo
echo "=== Result ==="
curl_request -o "$RESPONSE" "$API/boards/$DECK_BOARD_ID"
read_labels "$RESPONSE"
