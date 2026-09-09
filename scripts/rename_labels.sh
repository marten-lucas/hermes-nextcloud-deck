#!/bin/bash
# Synchronisiert die Board-Labels mit dem Friendly-Label-Mapping aus der
# Hermes-Config (config.yaml, platforms.deck.extra.label_mapping).
#
# Das Script benennt/erstellt die Board-Labels entsprechend dem Mapping.
# Die REIHENFOLGE im Mapping bestimmt die Ablage — Deck sortiert Labels im
# Board-UI nach ID, daher werden Labels in Mapping-Reihenfolge neu angelegt
# (alte werden vorher gelöscht), wenn die Reihenfolge weichen soll.
#
# Verwendung:
#   ./rename_labels.sh <NC_USERNAME> <NC_APP_PASSWORD>
#   DECK_CONFIG=/pfad/zur/config.yaml ./rename_labels.sh <user> <pass>
#
# App-Password erstellen: Nextcloud -> Einstellungen -> Sicherheit ->
# Geräte & Sitzungen -> "Neues App-Passwort erstellen".

U="${1:?Usage: $0 <username> <app_password>}"
P="${2:?Usage: $0 <username> <app_password>}"
B=https://next.cloud.kiga-gramschatz.de
API="$B/index.php/apps/deck/api/v1.0"
BOARD=12

CONFIG="${DECK_CONFIG:-}"

read_mapping() {
  if [ -n "$CONFIG" ] && [ -f "$CONFIG" ]; then
    python3 - "$CONFIG" << 'PYEOF'
import sys, re
mapping = []
key = None
with open(sys.argv[1], encoding="utf-8") as f:
    in_block = False
    for line in f:
        stripped = line.rstrip()
        if re.match(r"^\s*label_mapping:\s*$", stripped):
            in_block = True
            continue
        if in_block:
            m = re.match(r"^\s+([a-z]+:[a-z]+):\s*$", stripped)
            if m:
                key = m.group(1)
                continue
            m = re.match(r'^\s+title:\s*"(.+)"\s*$', stripped)
            if m and key:
                mapping.append([key, m.group(1), None])
                continue
            m = re.match(r'^\s+color:\s*"?([0-9A-Fa-f]{6})"?\s*$', stripped)
            if m and mapping and mapping[-1][2] is None:
                mapping[-1][2] = m.group(1)
                continue
            if stripped and not stripped.startswith(" "):
                in_block = False
for entry in mapping:
    if entry[2]:
        print(f"{entry[0]}|{entry[1]}|{entry[2]}")
PYEOF
  else
    # Default-Mapping (Status -> Freigaben -> Risiko -> Typ)
    cat << 'MAPPING'
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

echo "=== Mapping aus ${CONFIG:-Default} ==="
MAPPING=$(read_mapping)
echo "$MAPPING"
echo ""

echo "=== Aktuelle Board-Labels ==="
CURRENT=$(curl -s -u "$U:$P" "$API/boards/$BOARD" | python3 -c "
import json,sys
d=json.load(sys.stdin)
for l in d.get('labels', []):
    print(f\"{l['id']}|{l['title']}|{l.get('color','')}\")
")
echo "$CURRENT"
echo ""

# Schritt 1: Bestehende Labels löschen (sauberer Neuaufbau in Ziel-Reihenfolge;
# Deck sortiert Labels nach ID — nur so ist die Reihenfolge im Board garantiert).
echo "=== Schritt 1: Bestehende Labels löschen ==="
echo "$CURRENT" | while IFS='|' read -r id title color; do
  [ -z "$id" ] && continue
  curl -s -u "$U:$P" -X DELETE "$API/boards/$BOARD/labels/$id" \
    -o /dev/null -w "Label $id ('$title') gelöscht: HTTP %{http_code}\n"
done

# Schritt 2: Labels in Mapping-Reihenfolge neu anlegen.
echo ""
echo "=== Schritt 2: Labels in Mapping-Reihenfolge anlegen ==="
echo "$MAPPING" | while IFS='|' read -r key title color; do
  [ -z "$key" ] && continue
  PAYLOAD=$(python3 -c "import json,sys; print(json.dumps({'title': sys.argv[1], 'color': sys.argv[2]}))" "$title" "$color")
  curl -s -u "$U:$P" -X POST "$API/boards/$BOARD/labels" \
    -H "Content-Type: application/json" \
    -d "$PAYLOAD" \
    -o /dev/null -w "Label '$title' ($color) angelegt: HTTP %{http_code}\n"
done

echo ""
echo "=== Ergebnis ==="
curl -s -u "$U:$P" "$API/boards/$BOARD" | python3 -c "
import json,sys
d=json.load(sys.stdin)
for l in d.get('labels', []):
    print(l['id'], '|', l['title'], '|', l.get('color'))
"
