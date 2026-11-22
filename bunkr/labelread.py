"""Read calories off a photo of a nutrition label, on the hub, with no account or key.

The phone sends the photo to the hub, which reads it with Tesseract (Ubuntu's free `tesseract-ocr` package, run as
a program like 7-Zip is). Nothing leaves your network. If Tesseract isn't installed, label reading says so and
everything else works as before. The person always checks the numbers before saving.
"""

import re
import shutil
import subprocess

MAX_BYTES = 8_000_000
TYPES = {b"\xff\xd8\xff": "jpeg", b"\x89PNG": "png"}


def ready():
    return bool(shutil.which("tesseract"))


def ocr(image, timeout=40):
    if not any(image.startswith(sig) for sig in TYPES):
        raise ValueError("send a JPEG or PNG photo")
    if len(image) > MAX_BYTES:
        raise ValueError("that photo is too big")
    if not ready():
        raise RuntimeError("label reading isn't set up on the hub: an admin installs tesseract-ocr")
    r = subprocess.run(["tesseract", "stdin", "stdout", "--psm", "6", "-l", "eng"], input=image, capture_output=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError("couldn't read that photo")
    return r.stdout.decode("utf-8", "replace")


def _fix(t):
    """Common misreads inside numbers: O for 0, l or I for 1, S for 5."""
    t = re.sub(r"(?<=\d)[Oo]|[Oo](?=\d)", "0", t)
    t = re.sub(r"(?<=\d)[lI]|[lI](?=\d)", "1", t)
    return re.sub(r"(?<=\d)S|S(?=\d)", "5", t)


def parse(text):
    """Calories per serving, servings per container and the serving size, from the label's text."""
    t = _fix(re.sub(r"[ \t]+", " ", str(text or "")))
    low = t.lower()
    out = {"kcal": 0, "servings": 0.0, "serving": ""}
    m = re.search(r"calories(?! from)\s*[:.]?\s*(\d{1,4})\b", low) or re.search(r"calories(?! from)[^\d\n]{0,25}\n\s*(\d{1,4})\b", low)
    if m:
        out["kcal"] = int(m.group(1))
    m = (re.search(r"(?:about|approx\.?|abt\.?)?\s*(\d+(?:\.\d+)?)\s*servings?\s*per\s*(?:container|package|can|pack|box|bag)", low)
         or re.search(r"servings?\s*per\s*(?:container|package|can|pack|box|bag)\s*[:.]?\s*(?:about|approx\.?|abt\.?)?\s*(\d+(?:\.\d+)?)", low))
    if m:
        out["servings"] = float(m.group(1))
    m = re.search(r"serving size\s*[:.]?\s*([^\n]{1,40})", t, re.I)
    if m:
        out["serving"] = m.group(1).strip(" .:")
    if out["servings"] > 200 or out["kcal"] > 3000:   # a misread, not a label
        out.update(servings=0.0) if out["servings"] > 200 else out.update(kcal=0)
    out["total"] = round(out["kcal"] * out["servings"]) if out["kcal"] and out["servings"] else 0
    return out


def read(image):
    return parse(ocr(image))
