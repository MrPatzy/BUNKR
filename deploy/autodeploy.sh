#!/bin/bash
# BUNKR auto-deploy for the hub. Run by bunkr-deploy.timer every 2 minutes.
#
# When origin/main moves (a pull request was merged): test the new commit as the bunkr user, back up the database,
# fast-forward the live checkout, restart BUNKR and health-check it. If anything fails it rolls back code and
# database and says so on the pull request. It never deploys over uncommitted or unpushed work in the live checkout.
#
# Installed as a root-owned copy (/usr/local/sbin/bunkr-autodeploy) so a merged change to this file in the repo
# does not run as root until someone installs it on purpose.
set -uo pipefail

REPO=${BUNKR_REPO:-/opt/bunkr}
DB=${BUNKR_DB:-/var/lib/bunkr/bunkr.db}
BACKUPS=${BUNKR_BACKUPS:-/var/lib/bunkr/db-backups}
STATE=/var/lib/bunkr-deploy
TESTDIR=/var/lib/bunkr-deploy/test
SERVICE=${BUNKR_SERVICE:-bunkr}
HEALTH_URL=${BUNKR_HEALTH_URL:-http://127.0.0.1:8765/login.html}
HEALTH_HOST=${BUNKR_HEALTH_HOST:-192.168.1.40}
GH_REPO=${BUNKR_GH_REPO:-OWNER/BUNKR}
KEEP_BACKUPS=10

mkdir -p "$STATE" && chmod 755 "$STATE"
exec 9>"$STATE/lock"
flock -n 9 || exit 0

log() { echo "$(date -u +%FT%TZ) $*" | tee -a "$STATE/deploy.log"; }
once() { # log a skip reason only once per commit
	local key="$1"; shift
	[ "$(cat "$STATE/last_note" 2>/dev/null)" = "$key" ] && return
	echo "$key" > "$STATE/last_note"; log "$*"
}
pr_comment() { # comment on the merged pull request that brought this commit, if there is one
	command -v gh >/dev/null && id builder >/dev/null 2>&1 || return 0
	local n
	n=$(cd /home/builder && sudo -u builder gh pr list -R "$GH_REPO" --state merged --search "$1" --json number --jq '.[0].number' 2>/dev/null)
	[ -n "$n" ] && [ "$n" != "null" ] && (cd /home/builder && sudo -u builder gh pr comment "$n" -R "$GH_REPO" --body "$2" >/dev/null 2>&1)
	return 0
}
healthy() {
	for _ in $(seq 1 15); do
		sleep 2
		systemctl is-active -q "$SERVICE" || continue
		[ "$(curl -s -o /dev/null -w '%{http_code}' -H "Host: $HEALTH_HOST" "$HEALTH_URL")" = 200 ] && return 0
	done
	return 1
}

cd "$REPO" || exit 1
git fetch -q origin main || { log "fetch failed"; exit 1; }
old=$(git rev-parse HEAD)
new=$(git rev-parse origin/main)
[ "$old" = "$new" ] && exit 0
short=${new:0:7}

if ! git merge-base --is-ancestor HEAD origin/main; then
	once "ahead-$old-$new" "skipped $short: the live checkout has commits that aren't on GitHub; push or reset them first"
	exit 0
fi
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
	once "dirty-$new" "waiting on $short: the live checkout has uncommitted changes"
	exit 0
fi
if [ "$(cat "$STATE/failed" 2>/dev/null)" = "$new" ]; then
	exit 0
fi

log "deploying $short (was ${old:0:7})"

# 1. test the new commit in a scratch copy, as the unprivileged bunkr user
rm -rf -- /var/lib/bunkr-deploy/test
mkdir -p "$TESTDIR"
git archive "$new" | tar -x -C "$TESTDIR"
chown -R bunkr:bunkr "$TESTDIR"
if ! out=$(cd "$TESTDIR" && runuser -u bunkr -- timeout 900 python3 -m unittest discover -s tests 2>&1); then
	echo "$new" > "$STATE/failed"
	log "tests failed for $short; not deployed"
	echo "$out" | tail -40 >> "$STATE/deploy.log"
	pr_comment "$new" "Auto-deploy: tests failed on the hub for ${short}, so it was **not deployed**. The hub is still on ${old:0:7}. Last lines:
\`\`\`
$(echo "$out" | grep -E '^(FAIL|ERROR|Ran|FAILED|OK)' | tail -15)
\`\`\`"
	exit 1
fi
summary=$(echo "$out" | grep -E '^Ran ' | tail -1)

# 2. back up the database (sqlite online backup, so it is consistent while BUNKR runs)
mkdir -p "$BACKUPS"
backup="$BACKUPS/bunkr-before-deploy-$short-$(date -u +%Y%m%d-%H%M).db"
if ! runuser -u bunkr -- python3 -I -c 'import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()' "$DB" "$backup"; then
	log "database backup failed; not deployed"
	exit 1
fi
# keep the newest $KEEP_BACKUPS deploy backups
python3 -I -c 'import pathlib,sys; fs=sorted(pathlib.Path(sys.argv[1]).glob("bunkr-before-deploy-*.db"), key=lambda p: p.stat().st_mtime); [f.unlink() for f in fs[:-int(sys.argv[2])]]' "$BACKUPS" "$KEEP_BACKUPS"

# 3. switch the live code and restart
git merge --ff-only -q origin/main || { log "fast-forward failed"; exit 1; }
systemctl restart "$SERVICE"
if healthy; then
	rm -f "$STATE/failed"
	log "deployed $short ($summary); backup $backup"
	pr_comment "$new" "Auto-deploy: ${short} is live on the hub. Tests passed (${summary}). Database backed up first to \`$(basename "$backup")\`."
	exit 0
fi

# 4. roll back code and database
log "health check failed for $short; rolling back to ${old:0:7}"
journalctl -u "$SERVICE" -n 30 --no-pager -o cat >> "$STATE/deploy.log" 2>&1
systemctl stop "$SERVICE"
git reset -q --hard "$old"
install -o bunkr -g bunkr -m 640 "$backup" "$DB"
rm -f -- "$DB-wal" "$DB-shm"
systemctl start "$SERVICE"
echo "$new" > "$STATE/failed"
if healthy; then
	log "rolled back to ${old:0:7}; BUNKR is up"
	pr_comment "$new" "Auto-deploy: ${short} passed its tests but BUNKR didn't come back up after restarting, so the hub was **rolled back** to ${old:0:7} (code and database). Details are in /var/lib/bunkr-deploy/deploy.log on the hub."
else
	log "ROLLBACK ALSO UNHEALTHY; BUNKR needs attention"
	pr_comment "$new" "Auto-deploy: ${short} broke BUNKR and the rollback didn't bring it back. BUNKR needs attention on the hub."
fi
exit 1
