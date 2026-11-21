"""Master channel plan: same slot everywhere, per-radio rules, CHIRP and ADMS-12 output."""

import csv
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bunkr import radioplan as rp  # noqa: E402
from bunkr.db import Store  # noqa: E402
from tests import test_phase3 as p3  # noqa: E402


def ch(slot, name, rx, duplex="", offset=0.0, tx="", rxt="", **kw):
    service = "gmrs" if 462 <= rx <= 468 else ("noaa" if 162 <= rx <= 163 else "ham")
    return {"slot": slot, "name": name, "rx_mhz": rx, "duplex": duplex, "offset_mhz": offset, "tx_tone": tx,
            "rx_tone": rxt, "service": service, "mode": "FM", "scan": 1, "radios": [], **kw}


PLAN = [ch(15, "TAC-1", 462.550, tx="D114N", rxt="D114N"),
        ch(3, "GMRS 3 SAR", 462.6125, short="CH3SAR"),
        ch(23, "GMRS 15R", 462.550, "+", 5.0),
        ch(33, "Mesta 725", 462.725, "+", 5.0, "D145N", "D036N", short="MESTA", watch=1),
        ch(61, "CC Denver/Boulder", 145.310, "-", 0.6, "88.5", "123.0", short="CC1"),
        ch(91, "HOME-1", 147.480, "split", 146.460, "114.8", "114.8"),
        ch(95, "SHTF-1", 445.875, status="hold"),
        ch(121, "NOAA WX1", 162.550, "off", rx_only=1, scan=0),
        ch(130, "DRC-3", 224.380, "-", 1.6, "100.0"),
        ch(150, "OUT OF BAND", 147.250, "split", 143.250)]
RADIO = lambda prof: {"id": 1, "name": f"Test {prof}", "profile": prof}


class ToneTests(unittest.TestCase):
    def test_parse_and_normalise(self):
        self.assertEqual(rp.norm_tone("88.5"), "88.5")
        self.assertEqual(rp.norm_tone("d36n"), "D036N")
        self.assertEqual(rp.norm_tone(""), "")
        for bad in ("D36", "X145N", "300.0", "D14N"):
            with self.assertRaises(ValueError):
                rp.norm_tone(bad)

    def test_chirp_cross_modes(self):
        self.assertEqual(rp.chirp_tone("D145N", "D036N")["CrossMode"], "DTCS->DTCS")
        t = rp.chirp_tone("88.5", "123.0")
        self.assertEqual((t["Tone"], t["CrossMode"], t["rToneFreq"], t["cToneFreq"]), ("Cross", "Tone->Tone", "88.5", "123.0"))
        self.assertEqual(rp.chirp_tone("141.3", "141.3")["Tone"], "TSQL")
        self.assertEqual(rp.chirp_tone("67.0", "")["Tone"], "Tone")
        self.assertEqual(rp.chirp_tone("D114N", "D114N")["Tone"], "DTCS")


class BuildTests(unittest.TestCase):
    def build(self, prof, plan=PLAN):
        return rp.build(RADIO(prof), plan)

    def test_gm15pro_rules(self):
        mems, issues = self.build("gm15pro")
        by = {m["slot"]: m for m in mems}
        self.assertFalse(by[15]["rx_only"])           # TAC on a fixed simplex channel
        self.assertFalse(by[33]["rx_only"])           # GMRS repeater: may transmit
        self.assertTrue(by[61]["rx_only"])            # 55 and up: listen only
        self.assertNotIn(121, by)                     # has its own weather key
        self.assertNotIn(130, by)                     # 220 MHz: can't tune it
        self.assertEqual(by[3]["name"], "CH3SAR")     # 6-character display
        self.assertTrue(any("SHTF-1" in i["text"] and "hold" in i["text"] for i in issues))
        # a wrong frequency in a locked slot is an error, and the slot is left out
        mems, issues = self.build("gm15pro", [ch(23, "Wrong", 462.600, "+", 5.0), ch(40, "Ham rpt", 145.31, "-", 0.6)])
        self.assertEqual(mems, [])
        self.assertEqual(sum(i["level"] == "error" for i in issues), 2)

    def test_ham_radios_listen_only_on_gmrs_and_out_of_band(self):
        mems, _ = self.build("uv25x4")
        by = {m["slot"]: m for m in mems}
        self.assertTrue(by[33]["rx_only"] and "not GMRS-certified" in by[33]["why_rx_only"])
        self.assertFalse(by[61]["rx_only"])
        self.assertIn(130, by)                       # the UV-25X4 does 220
        self.assertIn(121, by)                       # no weather key: keeps NOAA, skipped in scan
        self.assertFalse(by[121]["scan"])
        self.assertTrue(by[150]["rx_only"] and "outside" in by[150]["why_rx_only"])
        text = rp.chirp_csv(mems)
        row = {r["Location"]: r for r in csv.DictReader(io.StringIO(text))}
        self.assertEqual((row["33"]["Duplex"], row["33"]["RxDtcsCode"]), ("off", "036"))
        self.assertEqual((row["61"]["Duplex"], row["61"]["Offset"], row["61"]["Tone"]), ("-", "0.600000", "Cross"))
        self.assertEqual((row["91"]["Duplex"], row["91"]["Offset"]), ("split", "146.460000"))
        self.assertEqual(row["121"]["Skip"], "S")

    def test_uv5r_memory_limit(self):
        mems, issues = self.build("uv5r")
        self.assertTrue(all(m["slot"] <= 127 for m in mems))
        self.assertTrue(any("beyond this radio's memory" in i["text"] for i in issues))

    def test_slot_collision_and_name_clash(self):
        mems, issues = rp.build(RADIO("gm15pro"), [ch(16, "GMRS 16 A", 462.575), ch(16, "GMRS 16 B", 462.575),
                                                   ch(17, "GMRS 17 A", 462.600)])
        self.assertTrue(any("already used" in i["text"] for i in issues))
        self.assertTrue(any("name 'GMRS 1'" in i["text"] for i in issues))

    def test_adms_csv_matches_the_ftm300_layout(self):
        mems, _ = self.build("ftm300")
        lines = rp.adms_csv(mems).split("\r\n")
        self.assertEqual(len([l for l in lines if l]), 999)
        rows = {int(r[0]): r for r in csv.reader(l for l in lines if l)}
        self.assertTrue(all(len(r) == 22 for r in rows.values()))
        self.assertEqual(rows[2], ["2"] + [""] * 20 + ["0"])
        self.assertEqual(rows[61][1:5], ["145.31", "144.71", "0.6", "-RPT"])
        self.assertEqual((rows[61][8], rows[61][9]), ("TONE", "88.5 Hz"))
        self.assertEqual((rows[33][8], rows[33][10]), ("DCS", "36"))   # listens on the repeater's output code
        self.assertEqual((rows[91][2], rows[91][4]), ("146.46", "-/+"))
        self.assertEqual(rows[15][17], "12.5KHz")
        self.assertNotIn(121, [m["slot"] for m in mems])               # weather key


class StoreTests(unittest.TestCase):
    def test_old_tones_carry_over(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "b.db"
            st = Store(db)
            st._conn.execute("PRAGMA user_version=8")
            st._conn.execute("INSERT INTO channels(name, rx_mhz, tone_mode, tone_hz, dtcs) VALUES('A', 146.52, 'TSQL', 100.0, 23)")
            st._conn.execute("INSERT INTO channels(name, rx_mhz, tone_mode, tone_hz, dtcs) VALUES('B', 462.55, 'DTCS', 88.5, 114)")
            st._conn.commit()
            st._conn.close()
            st = Store(db)
            got = {r["name"]: (r["tx_tone"], r["rx_tone"]) for r in st._rows("SELECT * FROM channels")}
            self.assertEqual(got, {"A": ("100.0", "100.0"), "B": ("D114N", "D114N")})


class ApiTests(unittest.TestCase):
    req = p3.LoginHttpTests.req

    def setUp(self):
        self.hub = p3.Hub(require_login=False)
        import urllib.request
        self.opener = urllib.request.build_opener(p3.NoRedirect)

    def tearDown(self):
        self.hub.close()

    def test_plan_radio_file_and_validation(self):
        self.assertEqual(self.req("POST", "/api/channels", {"name": "X", "rx_mhz": 462.55, "tx_tone": "D14N"})[0], 400)
        st, _, b = self.req("POST", "/api/channels", {"name": "Mesta 725", "rx_mhz": 462.725, "duplex": "+", "offset_mhz": 5,
                                                       "tx_tone": "D145N", "rx_tone": "d036n", "slot": 33, "status": "verified"})
        self.assertEqual(st, 200)
        row = self.hub.store._rows("SELECT * FROM channels")[0]
        self.assertEqual((row["slot"], row["rx_tone"], row["tone_mode"], row["dtcs"]), (33, "D036N", "DTCS", 145))
        st, _, b = self.req("POST", "/api/radios", {"name": "Car", "kind": "mobile", "profile": "ftm300"})
        rid = json.loads(b)["id"]
        st, hd, body = self.req("GET", f"/api/radios/{rid}/file")
        self.assertEqual(st, 200)
        self.assertIn("car-adms12.csv", hd["Content-Disposition"])
        self.assertTrue(body.decode().startswith("1,,,"))
        plan = json.loads(self.req("GET", f"/api/radios/{rid}/plan")[2])
        self.assertEqual([m["slot"] for m in plan["memories"]], [33])
        # the plan's radio files travel to the nodes
        from bunkr import server
        server.publish_generated()
        self.assertTrue((self.hub.depot.root / f"radio/car-{rid}-adms12.csv").is_file())


if __name__ == "__main__":
    unittest.main()
