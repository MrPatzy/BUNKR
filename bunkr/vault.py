"""Node lock: a Pi or laptop keeps the family's private data encrypted, so a stolen one gives away nothing.

What is private on a node: its database (inventory including firearms counts, where things are, people, phone
numbers, check-ins with shared locations, family calls, the brief) and the family plan and medical cards from the
hub. Maps, the library, radio files and apps are public and stay as they are, so a locked node still serves them.

How it works:
- The hub has one vault key (`vault.key` next to its database, mode 600). It seals the family plan and medical
  cards for nodes (`plan/*.html.sealed`), and hands the key to a paired node that asks with its sync token.
- A locked node keeps its database only as `vault/bunkr.db.sealed`. When unlocked it opens it into memory (sqlite
  deserialize), works as usual, and seals it back to disk every couple of minutes after changes and when it stops.
- Unlocking: at home the node gets the key from the hub on its own. Away from home (or with the hub down) someone
  types the family's node-lock passphrase on the node's page; the hub keeps a copy of the key wrapped with that
  passphrase (scrypt) on every node for exactly this.
- A node marked lost or stolen on the hub is cut off: its token stops working, so it can't fetch the key again.
  What protects a stolen node from then on is the strength of the passphrase.

AES-256-GCM via python3-cryptography; scrypt from the standard library.
"""

import base64
import hashlib
import json
import os
import secrets
from pathlib import Path

MAGIC = b"BKV1"
SCRYPT = {"n": 2 ** 15, "r": 8, "p": 1}     # ~32 MB and a fraction of a second on a Pi 5
MIN_PASSPHRASE = 12
SUFFIX = ".sealed"


class VaultError(Exception):
    pass


def _b64(b):
    return base64.b64encode(b).decode()


def seal(key, data, aad=b""):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = secrets.token_bytes(12)
    return MAGIC + nonce + AESGCM(key).encrypt(nonce, data, aad)


def unseal(key, blob, aad=b""):
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if not blob.startswith(MAGIC):
        raise VaultError("not a BUNKR vault file")
    try:
        return AESGCM(key).decrypt(blob[4:16], blob[16:], aad)
    except InvalidTag:
        raise VaultError("wrong key, or the file was changed")


def _kek(passphrase, salt, n, r, p):
    return hashlib.scrypt(passphrase.encode(), salt=salt, n=n, r=r, p=p, dklen=32, maxmem=128 * 1024 * 1024)


def wrap(key, passphrase):
    """The vault key, encrypted with a passphrase, as a small JSON-able dict."""
    if len(passphrase or "") < MIN_PASSPHRASE:
        raise VaultError(f"use a passphrase of at least {MIN_PASSPHRASE} characters (a few words is best)")
    salt = secrets.token_bytes(16)
    return {"v": 1, "salt": _b64(salt), **SCRYPT, "blob": _b64(seal(_kek(passphrase, salt, **SCRYPT), key, b"bunkr-vault-key"))}


def unwrap(wrapped, passphrase):
    try:
        kek = _kek(passphrase or "", base64.b64decode(wrapped["salt"]), int(wrapped["n"]), int(wrapped["r"]), int(wrapped["p"]))
        return unseal(kek, base64.b64decode(wrapped["blob"]), b"bunkr-vault-key")
    except VaultError:
        raise VaultError("that passphrase isn't right")
    except (KeyError, ValueError, TypeError):
        raise VaultError("the saved key is damaged; unlock at home instead")


def hub_key(folder):
    """The hub's vault key (created the first time)."""
    path = Path(folder) / "vault.key"
    if not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(secrets.token_bytes(32))
    key = path.read_bytes()
    if len(key) != 32:
        raise VaultError("vault.key is damaged")
    return key


def seal_file(key, src, dest):
    """Seal a generated file for nodes; the file name is bound in, so one can't be swapped for another."""
    _write(Path(dest), seal(key, Path(src).read_bytes(), Path(src).name.encode()))


def open_sealed(key, path):
    p = Path(path)
    return unseal(key, p.read_bytes(), p.name.removesuffix(SUFFIX).encode())


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


class NodeVault:
    """The node side: vault/bunkr.db.sealed and vault/wrapped.json next to the database path."""

    def __init__(self, db_path):
        self.db_path = Path(db_path)
        self.dir = self.db_path.parent / "vault"
        self.sealed = self.dir / "bunkr.db.sealed"
        self.wrapped_file = self.dir / "wrapped.json"

    def exists(self):
        return self.sealed.exists()

    def load(self, key):
        return unseal(key, self.sealed.read_bytes(), b"bunkr-node-db")

    def save(self, key, data):
        _write(self.sealed, seal(key, data, b"bunkr-node-db"))

    def wrapped(self):
        try:
            return json.loads(self.wrapped_file.read_text())
        except (OSError, ValueError):
            return None

    def save_wrapped(self, w):
        if w and w != self.wrapped():
            _write(self.wrapped_file, json.dumps(w).encode())

    def plain_files(self):
        """The unencrypted database and its copies, to remove once the vault holds everything."""
        p = self.db_path
        out = [x for x in (p, p.with_name(p.name + "-wal"), p.with_name(p.name + "-shm"), p.with_name(p.name + "-journal")) if x.exists()]
        backups = p.parent / "db-backups"
        if backups.is_dir():
            out += sorted(backups.glob("*.db"))
        return out

    @staticmethod
    def remove(paths):
        """Overwrite, then delete. (On flash storage the old blocks may linger until reused; the lock protects
        everything written from now on.)"""
        for f in paths:
            try:
                size = f.stat().st_size
                with open(f, "r+b") as fh:
                    fh.write(b"\0" * size)
                    fh.flush()
                    os.fsync(fh.fileno())
                f.unlink()
            except OSError:
                pass
