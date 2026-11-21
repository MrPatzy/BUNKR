"""Phases 5-7: maps extraction, firmware/app releases, radio pack, family readiness, scenarios (offline)."""

import gzip
import io
import json
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
import zipfile
from datetime import date
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bunkr import builder, family, gaps, library, nodes, pmtiles, radio, releases, scenarios, server  # noqa: E402
from bunkr.db import DEFAULT_SETTINGS, Store  # noqa: E402
from bunkr.depot import Depot  # noqa: E402


def make_source(path, maxzoom=4, leaves=True):
    """A PMTiles file with every tile up to maxzoom; tile bytes = b'z/x/y'. Optionally with leaf dirs."""
    tiles = []
    for z in range(maxzoom + 1):
        for x in range(1 << z):
            for y in range(1 << z):
                tiles.append((pmtiles.zxy_to_id(z, x, y), f"{z}/{x}/{y}".encode()))
    tiles.sort()
    data, entries, off = bytearray(), [], 0
    for tid, b in tiles:
        entries.append([tid, off, len(b), 1])
        data += b
        off += len(b)
    if leaves:
        half = len(entries) // 2
        l1 = gzip.compress(pmtiles.encode_directory(entries[:half]))
        l2 = gzip.compress(pmtiles.encode_directory(entries[half:]))
        root = gzip.compress(pmtiles.encode_directory([[entries[0][0], 0, len(l1), 0], [entries[half][0], len(l1), len(l2), 0]]))
        leaf_blob = l1 + l2
    else:
        root, leaf_blob = gzip.compress(pmtiles.encode_directory(entries)), b""
    meta = gzip.compress(json.dumps({"vector_layers": [{"id": "roads"}]}).encode())
    h = {"root_offset": 127, "root_length": len(root), "metadata_offset": 127 + len(root), "metadata_length": len(meta),
         "leaf_offset": 127 + len(root) + len(meta), "leaf_length": len(leaf_blob),
         "data_offset": 127 + len(root) + len(meta) + len(leaf_blob), "data_length": len(data),
         "addressed_tiles": len(tiles), "tile_entries": len(entries), "tile_contents": len(tiles),
         "clustered": 1, "internal_compression": 2, "tile_compression": 1, "tile_type": 1, "min_zoom": 0,
         "max_zoom": maxzoom, "min_lon": -180, "min_lat": -85, "max_lon": 180, "max_lat": 85,
         "center_zoom": 0, "center_lon": 0, "center_lat": 0}
    Path(path).write_bytes(pmtiles.build_header(h) + root + meta + leaf_blob + bytes(data))


class PmtilesTests(unittest.TestCase):
    def test_hilbert_ids(self):
        self.assertEqual([pmtiles.zxy_to_id(1, x, y) for x, y in ((0, 0), (0, 1), (1, 1), (1, 0))], [1, 2, 3, 4])
        for z, x, y in ((5, 3, 17), (12, 800, 1500), (0, 0, 0)):
            self.assertEqual(pmtiles.id_to_zxy(pmtiles.zxy_to_id(z, x, y)), (z, x, y))

    def test_directory_roundtrip(self):
        e = [[0, 0, 10, 1], [1, 10, 5, 3], [5, 40, 7, 1], [9, 15, 5, 0]]
        self.assertEqual(pmtiles.decode_directory(pmtiles.encode_directory(e)), e)

    def test_extract_subset_through_leaf_directories(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "world.pmtiles"
            make_source(src)
            areas = [((-10, -10, 10, 10), 0, 4)]
            out = Path(d) / "cut.pmtiles"
            stats = pmtiles.extract(pmtiles.FileSource(src), areas, out)
            r = pmtiles.Reader(pmtiles.FileSource(out))
            want = pmtiles.tile_ids(areas)
            got = r.locate(want)
            self.assertEqual(len(got), len(want))
            self.assertEqual(stats["tiles"], len(want))
            for tid, (off, ln) in got.items():
                z, x, y = pmtiles.id_to_zxy(tid)
                self.assertEqual(pmtiles.FileSource(out).read(r.h["data_offset"] + off, ln), f"{z}/{x}/{y}".encode())
            self.assertEqual(r.metadata()["vector_layers"][0]["id"], "roads")
            self.assertLess(out.stat().st_size, src.stat().st_size)
            self.assertEqual((r.h["min_zoom"], r.h["max_zoom"]), (0, 4))

    def test_large_extract_splits_root_into_leaves(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "world.pmtiles"
            make_source(src, maxzoom=8, leaves=False)
            out = Path(d) / "cut.pmtiles"
            old = pmtiles.ROOT_MAX
            pmtiles.ROOT_MAX = 300  # force the leaf-directory path (real world builds need it at country scale)
            try:
                pmtiles.extract(pmtiles.FileSource(src), [((-180, -85, 180, 85), 0, 8)], out)
            finally:
                pmtiles.ROOT_MAX = old
            r = pmtiles.Reader(pmtiles.FileSource(out))
            self.assertGreater(r.h["leaf_length"], 0)
            self.assertLessEqual(r.h["root_length"], pmtiles.ROOT_MAX)
            self.assertEqual(len(r.locate(pmtiles.tile_ids([((-180, -85, 180, 85), 8, 8)]))), 256 * 256)


class ReleaseTests(unittest.TestCase):
    def test_versions(self):
        e = {"tag_prefix": "companion-v"}
        self.assertEqual(releases.version_of(e, "companion-v1.18.0"), "1.18.0")
        self.assertEqual(releases.version_of({"tag_suffix": "-android"}, "2026.11.11-39-android"), "2026.11.11-39")
        self.assertTrue(releases.newer("1.18.0", "1.9.0"))
        self.assertFalse(releases.newer("1.18.0", "1.18.0"))

    def test_latest_skips_prereleases_and_other_types(self):
        entry = releases.BY_ID["meshcore-repeater-rak4631"]
        fake = {"meshcore-dev/MeshCore": [
            {"tag_name": "repeater-v1.19.0", "prerelease": True, "assets": []},
            {"tag_name": "companion-v1.18.0", "prerelease": False, "assets": [
                {"name": "RAK_4631_companion_radio_ble-v1.18.0-abc.uf2", "browser_download_url": "u", "size": 1}]},
            {"tag_name": "repeater-v1.18.0", "prerelease": False, "published_at": "2026-10-15", "assets": [
                {"name": "RAK_4631_repeater-v1.18.0-abc.uf2", "browser_download_url": "u1", "size": 5, "digest": "sha256:aa"},
                {"name": "RAK_4631_repeater-v1.18.0-abc.zip", "browser_download_url": "u2", "size": 6, "digest": "sha256:bb"},
                {"name": "Heltec_v3_repeater-v1.18.0-abc.bin", "browser_download_url": "u3", "size": 7}]}]}
        rel = releases.latest(entry, fake)
        self.assertEqual(rel["version"], "1.18.0")
        self.assertEqual(sorted(a["name"] for a in rel["assets"]), ["RAK_4631_repeater-v1.18.0-abc.uf2", "RAK_4631_repeater-v1.18.0-abc.zip"])
        self.assertEqual(rel["assets"][0]["sha256"], "aa")

    def test_meshtastic_zip_keeps_only_this_board(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            st = Store(d / "r.db")
            dep = Depot(st, d / "store")
            dep.ensure()
            zpath = d / "firmware-nrf52840-2.7.26.x.zip"
            with zipfile.ZipFile(zpath, "w") as z:
                for n in ("firmware-rak4631-2.7.26.x.uf2", "firmware-rak4631-2.7.26.x-ota.zip",
                          "firmware-rak4631_eink-2.7.26.x.uf2", "firmware-t-echo-2.7.26.x.uf2", "device-install.sh"):
                    z.writestr(n, n)
            entry = dict(releases.BY_ID["meshtastic-rak4631"])
            entry["release"] = {"version": "2.7.26.x", "assets": [{"name": zpath.name, "url": "file", "size": zpath.stat().st_size, "sha256": ""}]}
            orig = library.download
            library.download = lambda url, dest, *a, **k: dest.write_bytes(zpath.read_bytes())
            try:
                releases.fetch(dep, entry)
            finally:
                library.download = orig
            names = sorted(r["path"].rsplit("/", 1)[-1] for r in dep.rows())
            self.assertEqual(names, ["device-install.sh", "firmware-rak4631-2.7.26.x-ota.zip", "firmware-rak4631-2.7.26.x.uf2"])


class RadioTests(unittest.TestCase):
    def test_standard_set_and_chirp(self):
        chans = radio.standard_channels()
        self.assertEqual(len(chans), 22 + 8 + 7 + 2)
        rpt = next(c for c in chans if c["name"] == "GMRS 20R")
        full = {"tone_mode": "Tone", "tone_hz": 141.3, "dtcs": 23, **rpt}
        self.assertAlmostEqual(radio.tx_mhz(full), 467.675)
        rows = list(__import__("csv").DictReader(io.StringIO(radio.chirp_csv([full]))))
        self.assertEqual((rows[0]["Duplex"], rows[0]["Offset"], rows[0]["rToneFreq"]), ("+", "5.000000", "141.3"))
        wx = next(c for c in chans if c["name"] == "NOAA WX1")
        self.assertIsNone(radio.tx_mhz({"offset_mhz": 0, **wx}))

    def test_repeaterbook_import(self):
        text = ("Output Freq,Input Freq,Uplink Tone,Call,Location Nearest City,Mode\n"
                "147.225,147.825,100.0,W0ABC,Denver,FM\n145.310,144.710,,K0XYZ,Golden,FM\nnot a number,,,,,\n")
        got = radio.parse_import(text)
        self.assertEqual([(c["name"], c["duplex"], round(c["offset_mhz"], 3), c["tone_mode"]) for c in got],
                         [("W0ABC", "+", 0.6, "Tone"), ("K0XYZ", "-", 0.6, "")])

    def test_default_assignment(self):
        chans = [{"id": i, **c} for i, c in enumerate(radio.standard_channels())]
        gm = radio.default_channels_for({"kind": "handheld", "service": "gmrs"}, chans)
        ham = radio.default_channels_for({"kind": "mobile", "service": "ham"}, chans)
        self.assertEqual(len(gm), 37)
        self.assertEqual(len(ham), 9)
        self.assertEqual(radio.default_channels_for({"kind": "hf", "service": "ham"}, chans), [])


class FamilyTests(unittest.TestCase):
    def test_due_dates(self):
        t = {"every_days": 30, "yearly": "", "last_done": "2026-01-01", "active": 1}
        self.assertEqual(family.next_due(t, date(2026, 1, 10)), date(2026, 1, 31))
        self.assertEqual(family.next_due({**t, "last_done": ""}, date(2026, 5, 1)), date(2026, 5, 1))
        y = {"every_days": 0, "yearly": "10-15", "last_done": "", "active": 1}
        self.assertEqual(family.next_due(y, date(2026, 11, 18)), date(2027, 10, 15))
        self.assertEqual(family.next_due(y, date(2026, 3, 1)), date(2026, 10, 15))
        self.assertEqual(family.next_due({**y, "last_done": "2026-10-16"}, date(2026, 10, 20)), date(2027, 10, 15))

    def test_calendar_and_ics(self):
        tasks = [{"id": 1, "title": "Rotate water", "every_days": 182, "yearly": "", "last_done": "", "active": 1},
                 {"id": 2, "title": "Off", "every_days": 5, "yearly": "", "last_done": "", "active": 0}]
        items = [{"id": 9, "name": "Bars", "location": "Pantry", "expires": "2026-12-01"}]
        cal = family.calendar(tasks, items, [], date(2026, 11, 18))
        self.assertEqual([c["kind"] for c in cal], ["task", "use"])
        ics = family.ics(cal)
        self.assertTrue(ics.startswith("BEGIN:VCALENDAR") and ics.count("BEGIN:VEVENT") == 2 and "\r\n" in ics)

    def test_phone_status_and_plan(self):
        p = {"name": "Kid", "phone": "iphone", "phone_checks": json.dumps({"ice": True, "mesh_ios": True, "kiwix": True}), "phone_checked_at": "2026-11-01"}
        st = family.phone_status(p)
        self.assertEqual((st["done"], st["total"]), (1, 4))  # optional and retired items don't count
        self.assertEqual(len(st["items"]), 5)
        people = [{"name": "Dad", "role": "adult", "phone": "android", "call_sign": "K0DAD", "gmrs_call": "",
                   "blood_type": "O+", "allergies": "penicillin", "medications": "", "conditions": "", "doctor": ""}]
        contacts = [{"name": "Aunt May", "relation": "aunt", "phone": "555-0100", "email": "", "address": "", "out_of_area": 1, "notes": ""}]
        sites = [{"kind": "meet_near", "name": "Oak tree", "address": "corner park", "lat": None, "lon": None, "notes": ""}]
        chans = [{"name": "GMRS 20", "rx_mhz": 462.675, "in_plan": 1, "notes": "family"}]
        page = family.plan_html(people, contacts, sites, chans, {"mesh_channels": "#fam"})
        for text in ("penicillin", "Aunt May", "out of area", "Oak tree", "462.6750", "K0DAD", "#fam", "wallet"):
            self.assertIn(text, page)


class ScenarioTests(unittest.TestCase):
    def test_wildfire_smoke_pulls_items_from_inventory(self):
        s = dict(DEFAULT_SETTINGS, adults=2)
        items = [{"id": 1, "name": "3M N95 box", "category": "medical", "quantity": 4, "unit": "masks", "per_unit": 0,
                  "essential": "masks", "location": "Hall closet", "location_id": 1, "counts_home": 1, "expires": "", "notes": ""},
                 {"id": 2, "name": "Expired masks", "category": "medical", "quantity": 9, "unit": "", "per_unit": 0,
                  "essential": "masks", "location": "Garage", "location_id": 2, "counts_home": 1, "expires": "2000-01-01", "notes": ""}]
        profile = {"ratings": {"WFIR": "Very High"}}
        a = gaps.analyze(s, items, [], hazard_profile=profile)
        smoke = next(x for x in scenarios.overview(items, a, [], profile, []) if x["id"] == "wildfire_smoke")
        masks = next(x for x in smoke["supplies"] if x["id"] == "masks")
        self.assertEqual(masks["status"], "ok")
        self.assertEqual([(i["name"], i["location"]) for i in masks["items"]], [("3M N95 box", "Hall closet")])
        filt = next(x for x in smoke["supplies"] if x["id"] == "smoke_filter")
        self.assertEqual(filt["status"], "missing")
        top = [x for x in scenarios.overview(items, a, [], profile, []) if x["level"] == 5]
        self.assertEqual({x["id"] for x in top}, {"wildfire_smoke", "evacuation"})  # wildfire rated Very High here

    def test_hazard_gated_supplies(self):
        s = dict(DEFAULT_SETTINGS)
        a = gaps.analyze(s, [], [], hazard_profile={"ratings": {"HWAV": "Very Low"}})
        po = next(x for x in scenarios.overview([], a, [], {"ratings": {"HWAV": "Very Low"}}, []) if x["id"] == "power_outage")
        self.assertNotIn("cooling", [x["id"] for x in po["supplies"]])


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        d = Path(cls.tmp.name)
        server.store = Store(d / "t.db")
        server.depot = Depot(server.store, d / "store")
        server.depot.ensure()
        family.ensure_tasks(server.store)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.tmp.cleanup()

    def call(self, method, path, body=None, raw=False):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/{path}", method=method,
                                     headers={"X-Bunkr": "1", "Content-Type": "application/json"},
                                     data=json.dumps(body).encode() if body is not None else None)
        with urllib.request.urlopen(req) as resp:
            data = resp.read()
            return data.decode() if raw else json.loads(data)

    def test_offline_marks(self):
        """The service worker keeps only what the hub marks, and is told to take firearms out."""
        def heads(path):
            req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/{path}", headers={"X-Bunkr": "1"})
            with urllib.request.urlopen(req) as resp:
                return resp.headers
        h = heads("state")
        self.assertEqual(h.get("X-Bunkr-Keep"), "1")
        self.assertIn("firearms", h.get("X-Bunkr-Drop"))
        self.assertEqual(heads("board").get("X-Bunkr-Keep"), "1")
        self.assertIsNone(heads("audit").get("X-Bunkr-Keep"))
        self.assertIsNone(heads("radio").get("X-Bunkr-Keep"))

    def test_radio_flow_and_publishing(self):
        self.assertEqual(self.call("POST", "channels/standard")["added"], 39)
        self.assertEqual(self.call("POST", "channels/standard")["added"], 0)
        rid = self.call("POST", "radios", {"name": "Mom UV-15", "model": "Baofeng UV-15 Pro", "service": "gmrs"})["id"]
        view = self.call("GET", "radio")
        r = next(x for x in view["radios"] if x["id"] == rid)
        self.assertEqual(len(r["channel_ids"]), 37)
        csv_text = self.call("GET", f"radios/{rid}/chirp.csv", raw=True)
        self.assertEqual(csv_text.count("\n"), 38)
        fam = next(c for c in view["channels"] if c["name"] == "GMRS 20")
        self.call("POST", f"channels/{fam['id']}", {**fam, "in_plan": True})
        self.call("POST", f"radios/{rid}/channels", {"channel_ids": [fam["id"]]})
        self.assertIn("462.6750", self.call("GET", f"radios/{rid}/card.html", raw=True))
        self.assertIn("GMRS", self.call("GET", "radio/reference.html", raw=True))
        server.publish_generated()
        paths = {f["path"] for f in server.depot.rows()}
        self.assertIn("radio/reference.html", paths)
        self.assertIn("plan/family-plan.html", paths)
        self.assertTrue(any(p.endswith("-chirp.csv") for p in paths))
        before = {f["path"]: f["fetched_at"] for f in server.depot.rows()}
        server.publish_generated()  # unchanged content isn't rewritten
        self.assertEqual(before, {f["path"]: f["fetched_at"] for f in server.depot.rows()})
        pi = nodes.save(server.store, {"name": "Pi", "role": "pi"})
        planned = {f["path"] for f in nodes.plan_files(server.node_plan(nodes.get(server.store, pi)))}
        self.assertIn("plan/family-plan.html", planned)
        drive = nodes.save(server.store, {"name": "Stick", "role": "drive"})
        self.assertNotIn("plan/family-plan.html", {f["path"] for f in nodes.plan_files(server.node_plan(nodes.get(server.store, drive)))})

    def test_family_and_scenarios(self):
        pid = self.call("POST", "people", {"name": "Dad", "phone": "android", "allergies": "bees"})["id"]
        self.call("POST", f"people/{pid}/phone", {"checks": {"ice": True, "bogus": True}, "mark_checked": True})
        fam = self.call("GET", "family")
        p = next(x for x in fam["people"] if x["id"] == pid)
        self.assertEqual(p["phone_status"]["done"], 1)
        self.assertEqual(p["phone_status"]["checked_at"], date.today().isoformat())
        self.call("POST", "contacts", {"name": "Aunt", "out_of_area": True})
        self.assertIn("bees", self.call("GET", "family-plan.html", raw=True))
        task = next(t for t in fam["tasks"] if t["key"] == "rotate_water")
        self.call("POST", f"tasks/{task['id']}/done")
        fam = self.call("GET", "family")
        self.assertEqual(next(t for t in fam["tasks"] if t["id"] == task["id"])["last_done"], date.today().isoformat())
        self.assertIn("BEGIN:VEVENT", self.call("GET", "calendar.ics", raw=True))
        with self.assertRaises(urllib.error.HTTPError):
            self.call("DELETE", f"tasks/{task['id']}")  # built-ins can only be turned off
        with self.assertRaises(urllib.error.HTTPError):
            self.call("POST", "tasks", {"title": "x", "yearly": "13-40"})
        self.call("POST", "scenarios/power_outage/start")
        self.call("POST", "scenarios/power_outage/check", {"step": "during-0", "done": True})
        st = self.call("GET", "state")
        run = next(x for x in st["scenarios"] if x["id"] == "power_outage")["run"]
        self.assertIn("during-0", run["checked"])
        self.assertEqual(st["scenarios"][0]["id"], "power_outage")  # running scenarios come first
        self.call("POST", "scenarios/power_outage/end")
        self.assertIsNone(next(x for x in self.call("GET", "state")["scenarios"] if x["id"] == "power_outage")["run"])


class ArchivePlanTests(unittest.TestCase):
    def test_family_plan_inside_encrypted_archive(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            doc = d / "id.txt"
            doc.write_text("passport")
            out = d / "out"
            out.mkdir()
            names = builder._encrypt_documents(builder.Job(), [{"path": str(doc), "label": "ID", "doc_type": "identity"}],
                                               "correct horse battery", out, split=False, extra={"Family plan.html": "<p>plan</p>"})
            listing = subprocess.run(["7z", "l", "-pcorrect horse battery", str(out / names[0])], capture_output=True, text=True).stdout
            self.assertIn("Family plan.html", listing)


if __name__ == "__main__":
    unittest.main()
