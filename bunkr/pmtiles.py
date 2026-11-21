"""Cut a regional map out of a remote PMTiles v3 archive (e.g. the daily Protomaps world build).

Pure standard library. Reads the header and only the directories and tiles that
cover the requested areas (HTTP Range requests), then writes a new, clustered
PMTiles archive. Spec: https://github.com/protomaps/PMTiles/blob/main/spec/v3/spec.md
"""

import gzip
import io
import json
import math
import struct
import urllib.request

HEADER_LEN = 127
ROOT_MAX = 16384 - HEADER_LEN
COMPRESSION = {1: None, 2: "gzip"}
USER_AGENT = "BUNKR/0.3 (self-hosted preparedness app)"


# ---------- tile ids (Hilbert curve per zoom level) ----------
def _rotate(n, x, y, rx, ry):
    if ry == 0:
        if rx == 1:
            x, y = n - 1 - x, n - 1 - y
        x, y = y, x
    return x, y


def zxy_to_id(z, x, y):
    acc = ((1 << (2 * z)) - 1) // 3  # tiles in all lower zooms
    n = 1 << z
    d = 0
    s = n >> 1
    while s > 0:
        rx = 1 if x & s else 0
        ry = 1 if y & s else 0
        d += s * s * ((3 * rx) ^ ry)
        x, y = _rotate(n, x, y, rx, ry)
        s >>= 1
    return acc + d


def id_to_zxy(tile_id):
    z, acc = 0, 0
    while True:
        count = 1 << (2 * z)
        if tile_id < acc + count:
            break
        acc += count
        z += 1
    d = tile_id - acc
    n = 1 << z
    x = y = 0
    s = 1
    t = d
    while s < n:
        rx = 1 & (t // 2)
        ry = 1 & (t ^ rx)
        x, y = _rotate(s, x, y, rx, ry)
        x += s * rx
        y += s * ry
        t //= 4
        s *= 2
    return z, x, y


def lonlat_to_tile(lon, lat, z):
    lat = max(-85.0511, min(85.0511, lat))
    n = 1 << z
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
    return min(n - 1, max(0, x)), min(n - 1, max(0, y))


def bbox_around(lat, lon, km):
    dlat = km / 111.0
    dlon = km / (111.0 * max(0.1, math.cos(math.radians(lat))))
    return (lon - dlon, lat - dlat, lon + dlon, lat + dlat)


def tile_ids(areas):
    """areas: [(bbox(minlon,minlat,maxlon,maxlat), minzoom, maxzoom)] -> sorted unique tile ids."""
    ids = set()
    for (w, s, e, n), z0, z1 in areas:
        for z in range(z0, z1 + 1):
            x0, y0 = lonlat_to_tile(w, n, z)
            x1, y1 = lonlat_to_tile(e, s, z)
            for x in range(x0, x1 + 1):
                for y in range(y0, y1 + 1):
                    ids.add(zxy_to_id(z, x, y))
    return sorted(ids)


# ---------- varints and directories ----------
def _read_varint(buf, pos):
    shift = result = 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7


def _write_varint(out, v):
    while v >= 0x80:
        out.append((v & 0x7F) | 0x80)
        v >>= 7
    out.append(v)


def decode_directory(data):
    n, pos = _read_varint(data, 0)
    entries = [[0, 0, 0, 0] for _ in range(n)]  # tile_id, offset, length, run_length
    last = 0
    for e in entries:
        v, pos = _read_varint(data, pos)
        last += v
        e[0] = last
    for e in entries:
        e[3], pos = _read_varint(data, pos)
    for e in entries:
        e[2], pos = _read_varint(data, pos)
    for i, e in enumerate(entries):
        v, pos = _read_varint(data, pos)
        e[1] = entries[i - 1][1] + entries[i - 1][2] if v == 0 and i > 0 else v - 1
    return entries


def encode_directory(entries):
    out = bytearray()
    _write_varint(out, len(entries))
    last = 0
    for e in entries:
        _write_varint(out, e[0] - last)
        last = e[0]
    for e in entries:
        _write_varint(out, e[3])
    for e in entries:
        _write_varint(out, e[2])
    for i, e in enumerate(entries):
        if i > 0 and e[1] == entries[i - 1][1] + entries[i - 1][2]:
            _write_varint(out, 0)
        else:
            _write_varint(out, e[1] + 1)
    return bytes(out)


def _decompress(data, kind):
    if kind == 2:
        return gzip.decompress(data)
    if kind in (0, 1):
        return data
    raise ValueError(f"unsupported internal compression {kind}")


def parse_header(b):
    if b[:7] != b"PMTiles" or b[7] != 3:
        raise ValueError("not a PMTiles v3 archive")
    q = struct.unpack_from("<QQQQQQQQQQQ", b, 8)
    keys = ("root_offset", "root_length", "metadata_offset", "metadata_length", "leaf_offset", "leaf_length",
            "data_offset", "data_length", "addressed_tiles", "tile_entries", "tile_contents")
    h = dict(zip(keys, q))
    h["clustered"], h["internal_compression"], h["tile_compression"], h["tile_type"], h["min_zoom"], h["max_zoom"] = b[96:102]
    h["min_lon"], h["min_lat"], h["max_lon"], h["max_lat"] = [v / 1e7 for v in struct.unpack_from("<iiii", b, 102)]
    h["center_zoom"] = b[118]
    h["center_lon"], h["center_lat"] = [v / 1e7 for v in struct.unpack_from("<ii", b, 119)]
    return h


def build_header(h):
    b = bytearray(HEADER_LEN)
    b[:7] = b"PMTiles"
    b[7] = 3
    struct.pack_into("<QQQQQQQQQQQ", b, 8, h["root_offset"], h["root_length"], h["metadata_offset"],
                     h["metadata_length"], h["leaf_offset"], h["leaf_length"], h["data_offset"], h["data_length"],
                     h["addressed_tiles"], h["tile_entries"], h["tile_contents"])
    b[96:102] = bytes([h["clustered"], h["internal_compression"], h["tile_compression"], h["tile_type"],
                       h["min_zoom"], h["max_zoom"]])
    struct.pack_into("<iiii", b, 102, *[int(round(v * 1e7)) for v in (h["min_lon"], h["min_lat"], h["max_lon"], h["max_lat"])])
    b[118] = h["center_zoom"]
    struct.pack_into("<ii", b, 119, int(round(h["center_lon"] * 1e7)), int(round(h["center_lat"] * 1e7)))
    return bytes(b)


# ---------- sources ----------
class HttpSource:
    def __init__(self, url, timeout=60):
        self.url, self.timeout = url, timeout
        self.requests = 0

    def read(self, offset, length):
        req = urllib.request.Request(self.url, headers={"User-Agent": USER_AGENT,
                                                        "Range": f"bytes={offset}-{offset + length - 1}"})
        self.requests += 1
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            if r.status != 206 and offset:
                raise IOError("the map server ignored the byte range")
            data = r.read()
        if len(data) != length:
            raise IOError(f"short read: {len(data)} of {length} bytes")
        return data


class FileSource:
    def __init__(self, path):
        self.f = open(path, "rb")
        self.requests = 0

    def read(self, offset, length):
        self.requests += 1
        self.f.seek(offset)
        return self.f.read(length)


class Reader:
    def __init__(self, source):
        self.src = source
        head = source.read(0, 16384)
        self.h = parse_header(head)
        h = self.h
        if h["root_offset"] + h["root_length"] <= len(head):
            raw = head[h["root_offset"]:h["root_offset"] + h["root_length"]]
        else:
            raw = source.read(h["root_offset"], h["root_length"])
        self.root = decode_directory(_decompress(raw, h["internal_compression"]))
        self._leaves = {}

    def metadata(self):
        h = self.h
        if not h["metadata_length"]:
            return {}
        return json.loads(_decompress(self.src.read(h["metadata_offset"], h["metadata_length"]), h["internal_compression"]))

    def _leaf(self, offset, length):
        key = (offset, length)
        if key not in self._leaves:
            raw = self.src.read(self.h["leaf_offset"] + offset, length)
            self._leaves[key] = decode_directory(_decompress(raw, self.h["internal_compression"]))
        return self._leaves[key]

    def locate(self, wanted, on_progress=None):
        """{tile_id: (offset in data section, length)} for the wanted ids that exist."""
        import bisect
        found = {}

        def walk(entries, ids):
            if not ids:
                return
            for i, e in enumerate(entries):
                lo = e[0]
                hi = entries[i + 1][0] if i + 1 < len(entries) else float("inf")
                a = bisect.bisect_left(ids, lo)
                b = bisect.bisect_left(ids, hi) if hi != float("inf") else len(ids)
                if a == b:
                    continue
                if e[3] == 0:  # leaf directory
                    walk(self._leaf(e[1], e[2]), ids[a:b])
                    if on_progress:
                        on_progress(len(found))
                else:
                    end = e[0] + e[3]
                    for t in ids[a:b]:
                        if t < end:
                            found[t] = (e[1], e[2])
        walk(self.root, sorted(set(wanted)))
        return found


# ---------- writing ----------
def _dir_bytes(entries, compression):
    raw = encode_directory(entries)
    return gzip.compress(raw, mtime=0) if compression == 2 else raw


def extract(source, areas, dest, progress=None, cancelled=lambda: False, max_gap=256 * 1024, max_request=16 * 1024 * 1024):
    """Write a PMTiles archive at `dest` with every tile of `areas` that exists in `source`.

    areas: [(bbox, minzoom, maxzoom)]; returns stats {tiles, unique, bytes, requests}.
    Tile data is streamed to disk in source order, so memory stays small for big areas.
    """
    reader = Reader(source)
    h = reader.h
    wanted = tile_ids([(b, max(z0, h["min_zoom"]), min(z1, h["max_zoom"])) for b, z0, z1 in areas])
    if progress:
        progress("Reading the map index", 0, 0)
    found = reader.locate(wanted)
    uniq = sorted(set(found.values()))  # unique tile contents, by source offset
    ranges = []
    for off, ln in uniq:
        if ranges and off - (ranges[-1][0] + ranges[-1][1]) <= max_gap and off + ln - ranges[-1][0] <= max_request:
            ranges[-1][1] = max(ranges[-1][1], off + ln - ranges[-1][0])
        else:
            ranges.append([off, ln])
    total = sum(ln for _, ln in uniq)
    to_fetch = sum(l for _, l in ranges)
    tmp_data = dest.with_name(dest.name + ".data")
    new_offset = {}
    written = fetched = 0
    u = 0
    with open(tmp_data, "wb") as td:
        for start, length in ranges:
            if cancelled():
                raise InterruptedError("cancelled")
            blob = source.read(h["data_offset"] + start, length)
            while u < len(uniq) and uniq[u][0] < start + length:
                off, ln = uniq[u]
                new_offset[(off, ln)] = written
                td.write(blob[off - start:off - start + ln])
                written += ln
                u += 1
            fetched += length
            if progress:
                progress("Downloading map tiles", fetched, to_fetch)
    # directory entries with run-length for consecutive ids sharing content
    entries = []
    for tid, key in sorted(found.items()):
        off = new_offset[key]
        if entries and entries[-1][1] == off and entries[-1][0] + entries[-1][3] == tid:
            entries[-1][3] += 1
        else:
            entries.append([tid, off, key[1], 1])
    comp = h["internal_compression"] if h["internal_compression"] in (1, 2) else 2
    root = _dir_bytes(entries, comp)
    leaves = b""
    if len(root) > ROOT_MAX:
        size = 4096
        while True:
            leaf_blobs, root_entries, pos = [], [], 0
            for i in range(0, len(entries), size):
                chunk = entries[i:i + size]
                blob = _dir_bytes(chunk, comp)
                root_entries.append([chunk[0][0], pos, len(blob), 0])
                leaf_blobs.append(blob)
                pos += len(blob)
            root = _dir_bytes(root_entries, comp)
            if len(root) <= ROOT_MAX:
                leaves = b"".join(leaf_blobs)
                break
            size *= 2
    meta = reader.metadata()
    meta_raw = json.dumps(meta).encode()
    meta_bytes = gzip.compress(meta_raw, mtime=0) if comp == 2 else meta_raw
    w = min(b[0][0] for b in areas)
    s_ = min(b[0][1] for b in areas)
    e = max(b[0][2] for b in areas)
    n = max(b[0][3] for b in areas)
    out_h = dict(h)
    out_h.update(
        root_offset=HEADER_LEN, root_length=len(root),
        metadata_offset=HEADER_LEN + len(root), metadata_length=len(meta_bytes),
        leaf_offset=HEADER_LEN + len(root) + len(meta_bytes), leaf_length=len(leaves),
        data_offset=HEADER_LEN + len(root) + len(meta_bytes) + len(leaves), data_length=written,
        addressed_tiles=len(found), tile_entries=len(entries), tile_contents=len(new_offset),
        clustered=int(all(entries[i][1] <= entries[i + 1][1] for i in range(len(entries) - 1))),
        internal_compression=comp,
        min_zoom=max(h["min_zoom"], min(a[1] for a in areas)), max_zoom=min(h["max_zoom"], max(a[2] for a in areas)),
        min_lon=w, min_lat=s_, max_lon=e, max_lat=n,
        center_lon=(w + e) / 2, center_lat=(s_ + n) / 2, center_zoom=min(10, max(a[2] for a in areas)))
    part = dest.with_name(dest.name + ".part")
    with open(part, "wb") as f:
        f.write(build_header(out_h))
        f.write(root)
        f.write(meta_bytes)
        f.write(leaves)
        with open(tmp_data, "rb") as td:
            while True:
                chunk = td.read(1 << 22)
                if not chunk:
                    break
                f.write(chunk)
    tmp_data.unlink()
    part.replace(dest)
    return {"tiles": len(found), "unique": len(new_offset), "bytes": dest.stat().st_size,
            "requests": source.requests, "tile_bytes": total}


def estimate(source, areas):
    """Size of the tiles for these areas, from the index only (no tile downloads)."""
    reader = Reader(source)
    h = reader.h
    wanted = tile_ids([(b, max(z0, h["min_zoom"]), min(z1, h["max_zoom"])) for b, z0, z1 in areas])
    found = reader.locate(wanted)
    return {"tiles": len(found), "bytes": sum(ln for _, ln in set(found.values())), "requests": source.requests}
