#!/bin/bash
# Prove a BUNKR database backup actually restores: newest backup (or the one given) -> a throwaway folder ->
# integrity check -> upgrade to this code's schema -> start BUNKR on a spare port -> compare its contents with the
# backup -> clean up. Touches nothing live. Run it every few months, or after changing backups.
#   deploy/restore-test.sh [backup.db]
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
backup=${1:-$(ls -t /var/lib/bunkr/db-backups/*.db 2>/dev/null | head -1)}
[ -n "$backup" ] && [ -f "$backup" ] || { echo "No backup found."; exit 1; }
work=$(mktemp -d /tmp/bunkr-restore-XXXXXX)
port=$((20000 + RANDOM % 20000))
cleanup() { [ -n "${pid:-}" ] && kill "$pid" 2>/dev/null; rm -rf -- "$work"; }
trap cleanup EXIT
echo "Backup: $backup ($(du -h "$backup" | cut -f1), $(date -r "$backup" '+%Y-%m-%d %H:%M'))"
cp "$backup" "$work/bunkr.db"
python3 -I - "$work/bunkr.db" <<'EOF'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
ok = c.execute("PRAGMA integrity_check").fetchone()[0]
print("Integrity:", ok)
if ok != "ok":
    sys.exit(1)
print("Schema in backup:", c.execute("PRAGMA user_version").fetchone()[0])
EOF
counts() {
	python3 -I - "$1" <<'EOF'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
have = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
print(" ".join(f"{t}={c.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]}" for t in ("items", "locations", "people", "sites", "contacts", "channels") if t in have))
EOF
}
before=$(counts "$work/bunkr.db")
(cd "$here" && exec python3 -m bunkr --db "$work/bunkr.db" --bind 127.0.0.1 --port "$port" --no-browser --no-brief) >"$work/log" 2>&1 &
pid=$!
for _ in $(seq 1 30); do
	sleep 1
	curl -s -o /dev/null "http://127.0.0.1:$port/login.html" && break
done
curl -s -H "X-Bunkr: 1" "http://127.0.0.1:$port/api/state" \
	| python3 -I -c 'import json,sys; s=json.load(sys.stdin); print("BUNKR answered:", len(s["items"]), "items,", len(s["locations"]), "locations, readiness", str(s["analysis"]["overall"]) + "%")' \
	|| { echo "BUNKR did not start on the restored copy:"; tail -20 "$work/log"; exit 1; }
kill "$pid"; wait "$pid" 2>/dev/null || true; pid=""
after=$(counts "$work/bunkr.db")
echo "Rows in backup:   $before"
echo "Rows after start: $after"
python3 -I -c 'import sqlite3,sys; print("Schema now:", sqlite3.connect(sys.argv[1]).execute("PRAGMA user_version").fetchone()[0])' "$work/bunkr.db"
[ "$before" = "$after" ] && echo "RESTORE TEST PASSED" || { echo "RESTORE TEST: row counts changed during the upgrade; look closer"; exit 1; }
