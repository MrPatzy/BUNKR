"""A small MeshCore companion-radio client (standard library only), written from MeshCore's published companion
protocol (docs/companion_protocol.md, MIT) and its serial framing.

A companion radio is a MeshCore board (Heltec V3, T114, RAK4631, ...) running companion firmware. BUNKR talks to it:
- over USB serial (a Pi with the board plugged in: /dev/ttyACM0 or /dev/ttyUSB0, 115200 baud), or
- over TCP (a Heltec V3 on Wi-Fi companion firmware, port 5000), so the hub's container needs no USB passthrough.

Both carry the same frames: computer -> radio '<' + length (2 bytes, little-endian) + command;
radio -> computer '>' + length + response. Integers are little-endian, text is UTF-8.
"""

import os
import select
import socket
import struct
import time

# commands (computer -> radio)
CMD_APP_START = 0x01
CMD_SEND_CHANNEL_TXT = 0x03
CMD_GET_CONTACTS = 0x04
CMD_GET_MESSAGE = 0x0A
CMD_GET_BATTERY = 0x14
CMD_DEVICE_QUERY = 0x16
CMD_GET_CHANNEL = 0x1F
CMD_SET_CHANNEL = 0x20
# responses and pushes (radio -> computer)
OK, ERROR, SELF_INFO, MSG_SENT = 0x00, 0x01, 0x05, 0x06
CONTACT_MSG, CHANNEL_MSG, NO_MORE_MSGS, BATTERY, DEVICE_INFO = 0x07, 0x08, 0x0A, 0x0C, 0x0D
CONTACT_MSG_V3, CHANNEL_MSG_V3, CHANNEL_INFO = 0x10, 0x11, 0x12
MESSAGES_WAITING = 0x83
LOG_RX = 0x88       # a copy of every packet the radio hears: SNR, RSSI and the packet as it came off the air
# the radio's contact list: every node whose advert it has heard (firmware MyMesh.cpp, ContactInfo.h)
CONTACTS_START, CONTACT, END_OF_CONTACTS = 0x02, 0x03, 0x04
PUSH_ADVERT, PUSH_PATH_UPDATED, PUSH_NEW_ADVERT, PUSH_CONTACT_DELETED = 0x80, 0x81, 0x8A, 0x8F
NODE_TYPES = {0: "unknown", 1: "companion", 2: "repeater", 3: "room server", 4: "sensor"}  # ADV_TYPE_*
MAX_TEXT = 133      # characters MeshCore allows in one message
MAX_FRAME = 300


class RadioError(Exception):
    pass


def frame(payload):
    """Wrap one command for the radio."""
    return b"<" + struct.pack("<H", len(payload)) + payload


class Deframer:
    """Pulls '>'-framed responses out of a byte stream (bytes may arrive in any chunks)."""

    def __init__(self):
        self.buf = b""

    def feed(self, data):
        self.buf += data
        out = []
        while True:
            start = self.buf.find(b">")
            if start < 0:
                self.buf = b""
                return out
            if len(self.buf) - start < 3:
                self.buf = self.buf[start:]
                return out
            n = struct.unpack("<H", self.buf[start + 1:start + 3])[0]
            if n == 0 or n > MAX_FRAME:  # not a real header: skip this byte
                self.buf = self.buf[start + 1:]
                continue
            if len(self.buf) - start - 3 < n:
                self.buf = self.buf[start:]
                return out
            out.append(self.buf[start + 3:start + 3 + n])
            self.buf = self.buf[start + 3 + n:]


# ---------- commands ----------
def app_start(name="BUNKR"):
    return bytes([CMD_APP_START]) + bytes(7) + name.encode()


def device_query():
    return bytes([CMD_DEVICE_QUERY, 0x03])


def get_channel(idx):
    return bytes([CMD_GET_CHANNEL, idx])


def set_channel(idx, name, secret):
    if len(secret) != 16 or secret == bytes(16):
        raise ValueError("a channel secret is 16 random bytes")
    raw = name.encode()[:31]
    return bytes([CMD_SET_CHANNEL, idx]) + raw + bytes(32 - len(raw)) + secret


def send_channel_text(idx, text, ts=None):
    return bytes([CMD_SEND_CHANNEL_TXT, 0x00, idx]) + struct.pack("<I", int(ts or time.time())) + fit(text).encode()


def get_contacts(since=0):
    """The contact list, only those changed after `since` (the radio's own clock, from the last END_OF_CONTACTS)."""
    return bytes([CMD_GET_CONTACTS]) + struct.pack("<I", since) if since else bytes([CMD_GET_CONTACTS])


def get_message():
    return bytes([CMD_GET_MESSAGE])


def get_battery():
    return bytes([CMD_GET_BATTERY])


def fit(text, limit=MAX_TEXT):
    """One message, trimmed to what MeshCore carries (whole characters)."""
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


# ---------- responses ----------
def _cstr(b):
    return b.split(b"\0", 1)[0].decode("utf-8", "replace").strip()


def parse(data):
    """One response frame -> dict with "type" and its fields (unknown types come back with just their type)."""
    if not data:
        return {"type": None}
    t = data[0]
    out = {"type": t}
    try:
        if t == ERROR:
            out["code"] = data[1] if len(data) > 1 else 0
        elif t == SELF_INFO and len(data) >= 58:
            out.update(public_key=data[4:36].hex(), freq_mhz=struct.unpack("<I", data[48:52])[0] / 1000.0,
                       bw_khz=struct.unpack("<I", data[52:56])[0] / 1000.0, sf=data[56], cr=data[57],
                       name=data[58:].decode("utf-8", "replace").rstrip("\0").strip())
        elif t == DEVICE_INFO and len(data) >= 2:
            out["fw"] = data[1]
            if data[1] >= 3 and len(data) >= 80:
                out.update(max_channels=data[3], model=_cstr(data[20:60]), version=_cstr(data[60:80]))
        elif t == CHANNEL_INFO and len(data) >= 50:
            out.update(index=data[1], name=_cstr(data[2:34]), secret=data[34:50])
        elif t in (CHANNEL_MSG, CHANNEL_MSG_V3):
            o = 1
            if t == CHANNEL_MSG_V3:
                out["snr"] = (data[1] if data[1] < 128 else data[1] - 256) / 4.0
                o = 4
            out.update(kind="channel", channel=data[o], path_len=data[o + 1], ts=struct.unpack("<I", data[o + 3:o + 7])[0],
                       text=data[o + 7:].decode("utf-8", "replace"))
        elif t in (CONTACT_MSG, CONTACT_MSG_V3):
            o = 1
            if t == CONTACT_MSG_V3:
                out["snr"] = (data[1] if data[1] < 128 else data[1] - 256) / 4.0
                o = 4
            txt_type = data[o + 7]
            body = o + 12 + (4 if txt_type == 2 else 0)
            out.update(kind="direct", key_prefix=data[o:o + 6].hex(), path_len=data[o + 6], txt_type=txt_type,
                       ts=struct.unpack("<I", data[o + 8:o + 12])[0], text=data[body:].decode("utf-8", "replace"))
        elif t in (CONTACT, PUSH_NEW_ADVERT) and len(data) >= 148:
            # pub_key 32, type, flags, out_path_len, out_path[64], name[32], last_advert u32, lat i32, lon i32, lastmod u32
            p = len(data) - 84  # the out_path field (MAX_PATH_SIZE, 64 today)
            last_advert, lat, lon, lastmod = struct.unpack("<IiiI", data[68 + p:84 + p])
            out.update(public_key=data[1:33].hex(), node_type=data[33], flags=data[34],
                       hops=None if data[35] == 0xFF else data[35], name=_cstr(data[36 + p:68 + p]), last_advert=last_advert,
                       lat=lat / 1e6 if lat else None, lon=lon / 1e6 if lon else None, lastmod=lastmod)
        elif t in (CONTACTS_START, END_OF_CONTACTS) and len(data) >= 5:
            out["count" if t == CONTACTS_START else "lastmod"] = struct.unpack("<I", data[1:5])[0]
        elif t in (PUSH_ADVERT, PUSH_PATH_UPDATED, PUSH_CONTACT_DELETED) and len(data) >= 33:
            out["public_key"] = data[1:33].hex()
        elif t == LOG_RX and len(data) >= 4:
            out.update(snr=(data[1] if data[1] < 128 else data[1] - 256) / 4.0, rssi=data[2] if data[2] < 128 else data[2] - 256,
                       raw=bytes(data[3:]))
        elif t == BATTERY and len(data) >= 3:
            out["battery_mv"] = struct.unpack("<H", data[1:3])[0]
    except (IndexError, struct.error):
        out["bad"] = True
    return out


# ---------- packets as heard on the air (firmware src/Packet.cpp) ----------
GRP_TXT = 0x05


def hops(path_len):
    """A message's path length byte -> how many repeaters it came through (None for a direct message, 0xFF).
    Newer firmware keeps the size of each repeater's ID in the top two bits."""
    if path_len is None or path_len == 0xFF:
        return None
    return path_len & 63


def packet(raw):
    """A raw packet -> {"route", "type", "hash_size", "path": [hex IDs of the repeaters, first one first], "payload"},
    or None when it doesn't add up. Each repeater a flood message passes through adds the start of its public key."""
    try:
        header = raw[0]
        i = 1
        route = header & 0x03
        if route in (0, 3):  # transport codes
            i += 4
        pl = raw[i]
        i += 1
        size, count = (pl >> 6) + 1, pl & 63
        if size == 4 or i + size * count >= len(raw):
            return None
        path = [raw[i + k * size:i + (k + 1) * size].hex() for k in range(count)]
        i += size * count
        return {"route": route, "type": (header >> 2) & 0x0F, "hash_size": size, "path": path, "payload": bytes(raw[i:])}
    except IndexError:
        return None


def channel_hash(secret):
    """The byte a group message carries to say which channel it's on: the start of SHA-256 of the channel's key."""
    import hashlib
    return hashlib.sha256(bytes(secret)).digest()[0]


# ---------- transports ----------
class Connection:
    """A serial port or TCP socket to one companion radio, with command/response helpers."""

    def __init__(self, where, timeout=5.0):
        self.where, self.timeout = where, timeout
        self.fd = self.sock = None
        self.deframer = Deframer()
        self.pending = []      # frames read while waiting for something else (messages, pushes)
        if where.startswith("tcp:"):
            host, _, port = where[4:].rpartition(":")
            self.sock = socket.create_connection((host or where[4:], int(port or 5000)), timeout=timeout)
            self.sock.setblocking(False)
        else:
            self.fd = _open_serial(where)

    def close(self):
        try:
            if self.sock:
                self.sock.close()
            elif self.fd is not None:
                os.close(self.fd)
        except OSError:
            pass

    def _read(self, wait):
        src = self.sock if self.sock else self.fd
        r, _, _ = select.select([src], [], [], max(0.0, wait))
        if not r:
            return []
        data = self.sock.recv(4096) if self.sock else os.read(self.fd, 4096)
        if not data:
            raise RadioError("the radio closed the connection")
        return self.deframer.feed(data)

    def send(self, payload):
        data = frame(payload)
        if self.sock:
            self.sock.setblocking(True)
            try:
                self.sock.sendall(data)
            finally:
                self.sock.setblocking(False)
        else:
            os.write(self.fd, data)

    def request(self, payload, want, timeout=None):
        """Send a command and wait for one of the response types in `want`; other frames are kept for later."""
        self.send(payload)
        end = time.monotonic() + (timeout or self.timeout)
        while time.monotonic() < end:
            frames = [parse(raw) for raw in self._read(end - time.monotonic())]
            for i, p in enumerate(frames):
                if p["type"] in want or p["type"] == ERROR:
                    self.pending += frames[i + 1:]  # what came in the same read is kept, not dropped
                    if p["type"] == ERROR:
                        raise RadioError(f"the radio refused it (error {p.get('code')})")
                    return p
                self.pending.append(p)
        raise RadioError("the radio didn't answer")

    def poll(self, wait=0.0):
        """Frames that arrived on their own (pushes), plus any kept from earlier."""
        out, self.pending = self.pending, []
        out += [parse(raw) for raw in self._read(wait)]
        return out


def _open_serial(path, baud=115200):
    import termios
    import tty
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        tty.setraw(fd)
        attrs = termios.tcgetattr(fd)
        speed = getattr(termios, f"B{baud}")
        attrs[4] = attrs[5] = speed
        attrs[2] |= termios.CLOCAL | termios.CREAD
        attrs[2] &= ~termios.HUPCL  # don't reset the board when we close
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
    except Exception:
        os.close(fd)
        raise
    return fd


class Radio:
    """The handful of things BUNKR does with a companion radio."""

    def __init__(self, conn):
        self.conn = conn
        self.info = {}

    def start(self):
        me = self.conn.request(app_start(), {SELF_INFO})
        dev = self.conn.request(device_query(), {DEVICE_INFO})
        self.info = {"name": me.get("name", ""), "public_key": me.get("public_key", ""), "freq_mhz": me.get("freq_mhz"),
                     "bw_khz": me.get("bw_khz"), "sf": me.get("sf"), "model": dev.get("model", ""), "version": dev.get("version", ""),
                     "max_channels": dev.get("max_channels") or 8}
        return self.info

    def channel(self, idx):
        return self.conn.request(get_channel(idx), {CHANNEL_INFO})

    def ensure_channel(self, idx, name, secret):
        """Make sure slot idx holds our private channel; set it if it doesn't. True if it was changed."""
        if idx >= self.info.get("max_channels", 8):
            raise RadioError(f"this radio has only {self.info.get('max_channels')} channel slots")
        have = self.channel(idx)
        if have.get("name") == name[:31] and have.get("secret") == secret:
            return False
        self.conn.request(set_channel(idx, name, secret), {OK}, timeout=8)
        return True

    def send_text(self, idx, text):
        self.conn.request(send_channel_text(idx, text), {MSG_SENT, OK}, timeout=8)

    def messages(self):
        """Every queued message on the radio, oldest first."""
        out = []
        for _ in range(100):
            p = self.conn.request(get_message(), {CHANNEL_MSG, CHANNEL_MSG_V3, CONTACT_MSG, CONTACT_MSG_V3, NO_MORE_MSGS, 0x1B})
            if p["type"] == NO_MORE_MSGS:
                break
            if p.get("text") is not None:
                out.append(p)
        return out

    def contacts(self, since=0, timeout=20):
        """(nodes the radio knows, its "since" for next time). Other frames that arrive meanwhile are kept."""
        before = len(self.conn.pending)
        self.conn.request(get_contacts(since), {CONTACTS_START})
        early, self.conn.pending = self.conn.pending[before:], self.conn.pending[:before]  # arrived with the start frame
        out, end = [], time.monotonic() + timeout
        while time.monotonic() < end:
            frames, early = early + [parse(raw) for raw in self.conn._read(0 if early else end - time.monotonic())], []
            for i, p in enumerate(frames):
                if p["type"] == CONTACT and not p.get("bad"):
                    out.append(p)
                elif p["type"] == END_OF_CONTACTS:
                    self.conn.pending += frames[i + 1:]
                    return out, p.get("lastmod", 0)
                else:
                    self.conn.pending.append(p)
        raise RadioError("the radio's contact list didn't finish")

    def battery(self):
        return self.conn.request(get_battery(), {BATTERY}).get("battery_mv")
