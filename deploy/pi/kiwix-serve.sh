#!/usr/bin/env bash
# Serve every ZIM library in the BUNKR store with kiwix-serve (run by bunkr-kiwix.service as user bunkr).
#
# Each start rebuilds library.xml from the *.zim files present, skipping any that kiwix-manage cannot
# read (for example a half-copied file). With no usable ZIM files it exits cleanly (status 0), so systemd
# does not restart it in a loop. bunkr-kiwix.path starts it again when files appear in the library folder.
set -euo pipefail

LIB_DIR="${BUNKR_LIBRARY_DIR:-/var/lib/bunkr/store/library}"
STATE_DIR="${BUNKR_KIWIX_STATE:-/var/lib/bunkr/kiwix}"
PORT="${BUNKR_KIWIX_PORT:-8080}"

mkdir -p "$STATE_DIR"
# Built next to library.xml so the relative ZIM paths kiwix-manage writes stay valid after the move.
new="$STATE_DIR/library.xml.new"
rm -f "$new"

count=0
shopt -s nullglob
for zim in "$LIB_DIR"/*.zim; do
	if kiwix-manage "$new" add "$zim" >/dev/null 2>&1; then
		count=$((count + 1))
	else
		echo "Skipping a ZIM file Kiwix cannot read (still copying, or damaged): $zim" >&2
	fi
done

if [ "$count" -eq 0 ]; then
	rm -f "$new"
	echo "No offline libraries in $LIB_DIR yet. Kiwix will start when one is added."
	exit 0
fi

mv -f "$new" "$STATE_DIR/library.xml"
echo "Serving $count offline libraries on port $PORT."
# Under /kiwix so BUNKR can pass it through at the same path (https and the login); phones on the field
# Wi-Fi open http://<node>:8080/kiwix/ directly.
exec kiwix-serve --library --monitorLibrary --urlRootLocation /kiwix --port "$PORT" "$STATE_DIR/library.xml"
