"""Check every news outlet's feed the way BUNKR reads it: does it answer, parse, and have recent headlines?

    python3 tools/check_feeds.py            # all outlets; exits 1 if any look dead
    python3 tools/check_feeds.py ap cbc     # just these

Run it before a release, and when adding outlets (only ones that pass go in bunkr/brief.py OUTLETS). A dead feed gets
its address emptied there (kept, so stories it already supplied keep its name), not deleted. Hubs also keep their own
record of each feed (Household, "News sources" flags ones that stopped), but this catches it for everyone at once.
"""

import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bunkr import brief  # noqa: E402

RECENT_HOURS = 48


def check(key):
    o = brief.OUTLET[key]
    try:
        req = urllib.request.Request(o["feed"], headers={"User-Agent": brief.USER_AGENT, "Accept": "application/rss+xml, application/xml, */*"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read(brief.MAX_FEED + 1)
        items = brief.parse_feed(raw)
    except Exception as exc:
        return key, False, f"{type(exc).__name__}: {exc}"[:120]
    cutoff = datetime.now(timezone.utc) - timedelta(hours=RECENT_HOURS)
    dated = [i[2] for i in items if i[2]]
    recent = [i for i in items if i[2] is None or i[2] >= cutoff]
    if not recent:
        return key, False, f"nothing in {RECENT_HOURS} h (newest {max(dated).date() if dated else 'undated'}, {len(items)} items)"
    return key, True, f"{len(recent)} recent of {len(items)}"


def main(keys):
    keys = keys or [k for k, o in brief.OUTLET.items() if o["feed"]]
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(check, keys))
    bad = [r for r in results if not r[1]]
    for k, ok, msg in sorted(results, key=lambda r: (r[1], r[0])):
        print(f"{'ok  ' if ok else 'DEAD'} {k:16} {brief.OUTLET[k]['name'][:34]:34} {msg}")
    print(f"\n{len(results) - len(bad)} of {len(results)} feeds work.")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
