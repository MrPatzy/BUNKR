"""Careful downloads of web addresses an admin typed (offline packs, packs.py).

- https only, also after every redirect (at most 5), no user:password@ in the address.
- The name is looked up once and the connection goes to that address, so a second lookup can't point it somewhere else.
  Every address the name gives must be public: loopback, link-local (169.254.x.x, where cloud metadata lives),
  multicast and reserved addresses are always refused; private home-network addresses (10.x, 192.168.x, fd00::...)
  only when an admin turned on "allow home-network addresses".
- A size cap: refused up front when the server says it's bigger, and stopped as soon as it goes over.
- The file is written 600 while it downloads (to a staging name), checked against the SHA-256 when one is given,
  and only then made 644. Nothing downloaded is ever run.
"""

import hashlib
import http.client
import ipaddress
import os
import socket
import ssl
import urllib.parse

USER_AGENT = "BUNKR (offline packs)"
MAX_REDIRECTS = 5
TIMEOUT = 60
NAT64 = ipaddress.ip_network("64:ff9b::/96")   # 64:ff9b::10.0.0.1 reaches 10.0.0.1 on a NAT64 network


class Blocked(ValueError):
    """The address isn't allowed (not https, a private address, too many redirects...)."""


class TooBig(IOError):
    pass


class ChecksumMismatch(IOError):
    pass


def check_url(url, allow_lan=False):
    """Parse and check an address without looking it up. Returns the parsed address or raises Blocked."""
    url = str(url or "").strip()
    if not url or len(url) > 2000:
        raise Blocked("enter a web address")
    if any(ord(c) < 33 or ord(c) > 126 for c in url):
        raise Blocked("the address has spaces or unusual characters")
    try:
        u = urllib.parse.urlsplit(url)
        port = u.port or 443
    except ValueError:
        raise Blocked("that isn't a web address")
    if u.scheme.lower() != "https":
        raise Blocked("only https:// addresses are allowed")
    if u.username is not None or u.password is not None or "@" in u.netloc:
        raise Blocked("addresses with a user name or password aren't allowed")
    host = (u.hostname or "").rstrip(".")
    if not host:
        raise Blocked("the address has no host name")
    if host == "localhost" or host.endswith(".localhost") or \
            (host.endswith((".local", ".internal", ".home.arpa", ".lan")) and not allow_lan):
        raise Blocked("that address is on this machine or the home network")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        check_ip(ip, allow_lan)
    elif not all(part and len(part) < 64 for part in host.split(".")):
        raise Blocked("that host name isn't valid")
    return u, host, port


def check_ip(ip, allow_lan=False):
    ip = ipaddress.ip_address(ip)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    elif isinstance(ip, ipaddress.IPv6Address) and ip in NAT64:
        ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
        raise Blocked(f"{ip} is not an internet address")
    if not ip.is_global and not allow_lan:
        raise Blocked(f"{ip} is a home-network or private address (turn on home-network addresses to allow it)")
    return ip


def resolve(host, port, allow_lan=False, getaddrinfo=socket.getaddrinfo):
    """The one address to connect to. Every address the name gives must pass, or none is used."""
    try:
        infos = getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError) as exc:
        raise IOError(f"can't find {host} ({exc})")
    ips = []
    for info in infos:
        ip = info[4][0].split("%", 1)[0]
        check_ip(ip, allow_lan)
        ips.append(ip)
    if not ips:
        raise IOError(f"can't find {host}")
    return ips[0]


class _PinnedHTTPS(http.client.HTTPSConnection):
    """HTTPS to a fixed IP address, with the certificate checked for the host name."""

    def __init__(self, host, ip, port, timeout):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._ip = ip

    def connect(self):
        sock = socket.create_connection((self._ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def _connect(host, ip, port, timeout):
    return _PinnedHTTPS(host, ip, port, timeout)


class Getter:
    """Opens addresses safely. Tests swap `connect` (to a local plain-HTTP server) and `getaddrinfo`."""

    def __init__(self, allow_lan=False, connect=_connect, getaddrinfo=socket.getaddrinfo, timeout=TIMEOUT):
        self.allow_lan, self.connect, self.getaddrinfo, self.timeout = allow_lan, connect, getaddrinfo, timeout

    def open(self, url, headers=None):
        """GET, following https redirects. Returns (response, connection, final url); close the connection after."""
        for _ in range(MAX_REDIRECTS + 1):
            u, host, port = check_url(url, self.allow_lan)
            ip = resolve(host, port, self.allow_lan, self.getaddrinfo)
            conn = self.connect(host, ip, port, self.timeout)
            path = (u.path or "/") + (f"?{u.query}" if u.query else "")
            try:
                conn.request("GET", path, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity",
                                                   **(headers or {})})
                resp = conn.getresponse()
            except Exception:
                conn.close()
                raise
            if resp.status in (301, 302, 303, 307, 308):
                loc = resp.getheader("Location") or ""
                resp.close()
                conn.close()
                if not loc:
                    raise IOError("redirect without an address")
                url = urllib.parse.urljoin(url, loc)
                continue
            return resp, conn, url
        raise Blocked("too many redirects")

    def get_small(self, url, cap=1 << 20):
        """A small file (a checksum list, a .meta4) into memory."""
        resp, conn, _ = self.open(url)
        try:
            if resp.status != 200:
                raise IOError(f"the server said {resp.status} {resp.reason}")
            data = resp.read(cap + 1)
            if len(data) > cap:
                raise TooBig("bigger than expected")
            return data
        finally:
            conn.close()

    def download(self, url, dest, cap, sha256=None, headers=None, progress=None, cancelled=lambda: False):
        """Download url into dest (a staging file). Returns {"status", "size", "sha256", "etag", "last_modified"};
        status 304 means not changed (nothing written). Raises TooBig, ChecksumMismatch, Blocked or IOError; on any
        failure dest is removed."""
        resp, conn, _ = self.open(url, headers)
        dest = str(dest)
        try:
            meta = {"etag": (resp.getheader("ETag") or "")[:200], "last_modified": (resp.getheader("Last-Modified") or "")[:64]}
            if resp.status == 304:
                return {"status": 304, **meta}
            if resp.status != 200:
                raise IOError(f"the server said {resp.status} {resp.reason}")
            length = resp.getheader("Content-Length")
            if length and length.isdigit() and int(length) > cap:
                raise TooBig(f"it's {int(length) / 1e9:.2f} GB, over the {cap / 1e9:.2f} GB limit")
            h, done = hashlib.sha256(), 0
            fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
            try:
                with os.fdopen(fd, "wb") as out:
                    while True:
                        if cancelled():
                            raise InterruptedError("cancelled")
                        chunk = resp.read(1 << 20)
                        if not chunk:
                            break
                        done += len(chunk)
                        if done > cap:
                            raise TooBig(f"went over the {cap / 1e9:.2f} GB limit")
                        out.write(chunk)
                        h.update(chunk)
                        if progress:
                            progress(done)
                if length and length.isdigit() and done != int(length):
                    raise IOError(f"download cut short ({done} of {length} bytes)")
                got = h.hexdigest()
                if sha256 and got != sha256.lower():
                    raise ChecksumMismatch(f"checksum doesn't match (got {got[:12]}…, expected {sha256[:12]}…)")
            except BaseException:
                try:
                    os.unlink(dest)
                except OSError:
                    pass
                raise
            os.chmod(dest, 0o644)
            return {"status": 200, "size": done, "sha256": got, **meta}
        finally:
            conn.close()
