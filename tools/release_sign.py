"""The BUNKR release key: make it once, sign each release's SHA256SUMS with it.

    python3 tools/release_sign.py keygen [--dir DIR]           # once, on the maintainer's machine
    python3 tools/release_sign.py sign dist/SHA256SUMS [--key FILE]   # each release: writes dist/SHA256SUMS.sig
    python3 tools/release_sign.py verify dist/SHA256SUMS [--pub bunkr/release_key.pub]

The key is Ed25519 (python3-cryptography). keygen writes the private key to DIR/release.key (default
~/.config/bunkr-release/, folder 700, file 600; it never goes in a repository) and the public key to
bunkr/release_key.pub in this repository, which is what every BUNKR checks upgrades against. Commit that public
file. Signing is over the exact bytes of SHA256SUMS; the .sig is the base64 signature on one line.
Back the private key up somewhere offline: without it no hub will accept a new release from the Upgrade button,
and replacing it means shipping a release with the new public key that admins install by hand.
"""

import argparse
import base64
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
DEFAULT_DIR = Path.home() / ".config" / "bunkr-release"
PUB = HERE / "bunkr" / "release_key.pub"


def keygen(folder, pub_out):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    folder = Path(folder)
    key_file = folder / "release.key"
    if key_file.exists():
        raise SystemExit(f"{key_file} already exists; a release key is made once")
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(pem)
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    text = ("# BUNKR release signing key (Ed25519, base64). Upgrades are only installed if SHA256SUMS is signed by it.\n"
            + base64.b64encode(raw).decode() + "\n")
    Path(pub_out).write_text(text)
    (folder / "release_key.pub").write_text(text)
    return key_file, Path(pub_out)


def sign(path, key_file):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key_file = Path(key_file)
    if key_file.stat().st_mode & 0o077:
        raise SystemExit(f"{key_file} is readable by others; chmod 600 it first")
    key = serialization.load_pem_private_key(key_file.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise SystemExit(f"{key_file} isn't an Ed25519 key")
    sig = Path(str(path) + ".sig")
    sig.write_text(base64.b64encode(key.sign(Path(path).read_bytes())).decode() + "\n")
    return sig


def verify(path, pub):
    sys.path.insert(0, str(HERE))
    from bunkr import upgrader
    key = upgrader.load_key(pub)
    if not key:
        raise SystemExit(f"no public key in {pub}")
    return upgrader.verify_signature(Path(path).read_bytes(), Path(str(path) + ".sig").read_bytes(), key)


def main(argv=None):
    ap = argparse.ArgumentParser(description="BUNKR release key: keygen, sign, verify")
    sub = ap.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("keygen")
    k.add_argument("--dir", default=str(DEFAULT_DIR))
    k.add_argument("--pub-out", default=str(PUB), help="where the public key goes (default bunkr/release_key.pub)")
    s = sub.add_parser("sign")
    s.add_argument("file")
    s.add_argument("--key", default=str(DEFAULT_DIR / "release.key"))
    v = sub.add_parser("verify")
    v.add_argument("file")
    v.add_argument("--pub", default=str(PUB))
    a = ap.parse_args(argv)
    if a.cmd == "keygen":
        key_file, pub = keygen(a.dir, a.pub_out)
        print(f"private key: {key_file} (mode 600; back it up offline, never commit it)\npublic key: {pub} (commit this)")
    elif a.cmd == "sign":
        sig = sign(a.file, a.key)
        if not verify(a.file, PUB):
            raise SystemExit(f"signed, but {PUB} doesn't match this key: is the right public key committed?")
        print(f"wrote {sig}")
    else:
        ok = verify(a.file, a.pub)
        print("signature OK" if ok else "BAD signature")
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
