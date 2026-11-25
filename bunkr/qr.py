"""QR codes for bin labels, with no outside packages: byte mode, error correction level M, versions 1-10 (up to 213
bytes, plenty for a web address). `matrix(text)` gives rows of 0/1; `svg(text)` a crisp, scalable picture to print.

Follows ISO/IEC 18004: Reed-Solomon over GF(256) (polynomial 0x11D), interleaved blocks, the eight masks scored by
the standard's four penalty rules, BCH-coded format and version information.
"""

# version: (ECC codewords per block, [(blocks, data codewords per block), ...]) for level M
BLOCKS = {1: (10, [(1, 16)]), 2: (16, [(1, 28)]), 3: (26, [(1, 44)]), 4: (18, [(2, 32)]), 5: (24, [(2, 43)]),
          6: (16, [(4, 27)]), 7: (18, [(4, 31)]), 8: (22, [(2, 38), (2, 39)]), 9: (22, [(3, 36), (2, 37)]),
          10: (26, [(4, 43), (1, 44)])}
ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46],
         10: [6, 28, 50]}
EC_M = 0b00

# GF(256) tables
EXP, LOG = [0] * 512, [0] * 256
_x = 1
for _i in range(255):
    EXP[_i] = _x
    LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    EXP[_i] = EXP[_i - 255]


def _mul(a, b):
    return 0 if a == 0 or b == 0 else EXP[LOG[a] + LOG[b]]


def _rs(data, n):
    """The n error-correction codewords for one block."""
    gen = [1]
    for i in range(n):
        nxt = [0] * (len(gen) + 1)
        for j, g in enumerate(gen):
            nxt[j] ^= g
            nxt[j + 1] ^= _mul(g, EXP[i])
        gen = nxt
    rem = list(data) + [0] * n
    for i in range(len(data)):
        c = rem[i]
        if c:
            for j in range(1, len(gen)):
                rem[i + j] ^= _mul(gen[j], c)
    return rem[len(data):]


def _capacity(v):
    return sum(b * d for b, d in BLOCKS[v][1])


def _codewords(data, v):
    cap = _capacity(v)
    bits = [0, 1, 0, 0] + [int(b) for b in format(len(data), "08b" if v < 10 else "016b")]
    for byte in data:
        bits += [int(b) for b in format(byte, "08b")]
    bits += [0] * min(4, cap * 8 - len(bits))
    bits += [0] * (-len(bits) % 8)
    words = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = (0xEC, 0x11)
    words += [pad[i % 2] for i in range(cap - len(words))]
    ecn, groups = BLOCKS[v]
    blocks, pos = [], 0
    for count, size in groups:
        for _ in range(count):
            blocks.append(words[pos:pos + size])
            pos += size
    eccs = [_rs(b, ecn) for b in blocks]
    out = []
    for i in range(max(len(b) for b in blocks)):
        out += [b[i] for b in blocks if i < len(b)]
    for i in range(ecn):
        out += [e[i] for e in eccs]
    return out


def _bch(value, poly, bits):
    v = value << bits
    for i in range(v.bit_length() - 1, bits - 1, -1):
        if v >> i & 1:
            v ^= poly << (i - bits)
    return (value << bits) | v


MASKS = [lambda r, c: (r + c) % 2 == 0, lambda r, c: r % 2 == 0, lambda r, c: c % 3 == 0, lambda r, c: (r + c) % 3 == 0,
         lambda r, c: (r // 2 + c // 3) % 2 == 0, lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
         lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0, lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0]


def _base(v):
    """The fixed patterns, and which cells are reserved for them (or for format/version info)."""
    n = 17 + 4 * v
    m = [[0] * n for _ in range(n)]
    fixed = [[False] * n for _ in range(n)]

    def put(r, c, val):
        m[r][c], fixed[r][c] = val, True

    for r0, c0 in ((0, 0), (0, n - 7), (n - 7, 0)):
        for r in range(-1, 8):
            for c in range(-1, 8):
                rr, cc = r0 + r, c0 + c
                if 0 <= rr < n and 0 <= cc < n:
                    on = 0 <= r <= 6 and 0 <= c <= 6 and (r in (0, 6) or c in (0, 6) or (2 <= r <= 4 and 2 <= c <= 4))
                    put(rr, cc, int(on))
    for i in range(8, n - 8):
        put(6, i, int(i % 2 == 0))
        put(i, 6, int(i % 2 == 0))
    pos = ALIGN[v]
    corners = {(pos[0], pos[0]), (pos[0], pos[-1]), (pos[-1], pos[0])} if pos else set()
    for r in pos:
        for c in pos:
            if (r, c) in corners:   # those three sit on the finders
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    put(r + dr, c + dc, int(max(abs(dr), abs(dc)) != 1))
    put(n - 8, 8, 1)  # the dark module
    for i in range(9):  # format info areas
        for r, c in ((8, i), (i, 8)):
            if not fixed[r][c]:
                fixed[r][c] = True
    for i in range(8):
        fixed[8][n - 1 - i] = True
        fixed[n - 1 - i][8] = True
    if v >= 7:
        for i in range(6):
            for j in range(3):
                fixed[i][n - 11 + j] = fixed[n - 11 + j][i] = True
    return m, fixed


def _place(m, fixed, words):
    n = len(m)
    bits = [w >> (7 - i) & 1 for w in words for i in range(8)]
    k, up, c = 0, True, n - 1
    while c > 0:
        if c == 6:
            c -= 1
        for i in range(n):
            r = n - 1 - i if up else i
            for cc in (c, c - 1):
                if not fixed[r][cc]:
                    m[r][cc] = bits[k] if k < len(bits) else 0
                    k += 1
        up = not up
        c -= 2


def _info(m, v, mask):
    n = len(m)
    f = _bch(EC_M << 3 | mask, 0x537, 10) ^ 0x5412
    fb = [f >> i & 1 for i in range(15)]           # bit 0 is the least significant
    for i in range(6):                             # around the top-left finder
        m[i][8] = fb[i]
    m[7][8], m[8][8], m[8][7] = fb[6], fb[7], fb[8]
    for i in range(9, 15):
        m[8][14 - i] = fb[i]
    for i in range(8):                             # the second copy: top-right, then bottom-left
        m[8][n - 1 - i] = fb[i]
    for i in range(8, 15):
        m[n - 15 + i][8] = fb[i]
    m[n - 8][8] = 1
    if v >= 7:
        vb = _bch(v, 0x1F25, 12)
        for i in range(18):
            bit = vb >> i & 1
            m[i // 3][n - 11 + i % 3] = bit
            m[n - 11 + i % 3][i // 3] = bit


def _penalty(m):
    n, score = len(m), 0
    for rows in (m, [list(col) for col in zip(*m)]):
        for row in rows:
            run, last = 0, None
            for x in row:
                if x == last:
                    run += 1
                else:
                    if run >= 5:
                        score += run - 2
                    run, last = 1, x
            if run >= 5:
                score += run - 2
            s = "".join(map(str, row))
            score += 40 * (s.count("10111010000") + s.count("00001011101"))
    for r in range(n - 1):
        for c in range(n - 1):
            if m[r][c] == m[r][c + 1] == m[r + 1][c] == m[r + 1][c + 1]:
                score += 3
    dark = sum(map(sum, m))
    score += 10 * (abs(dark * 100 // (n * n) - 50) // 5)
    return score


def matrix(text, mask=None, version=None):
    """Rows of 0/1 for `text` (UTF-8), the smallest version that fits, the mask with the lowest penalty."""
    data = text.encode("utf-8") if isinstance(text, str) else bytes(text)
    v = version or next((v for v in BLOCKS if _capacity(v) * 8 >= 4 + (8 if v < 10 else 16) + 8 * len(data)), None)
    if v is None:
        raise ValueError("too long for a QR code here (213 bytes at most)")
    words = _codewords(data, v)
    best = None
    for k in ([mask] if mask is not None else range(8)):
        m, fixed = _base(v)
        _place(m, fixed, words)
        for r in range(len(m)):
            for c in range(len(m)):
                if not fixed[r][c] and MASKS[k](r, c):
                    m[r][c] ^= 1
        _info(m, v, k)
        p = _penalty(m)
        if best is None or p < best[0]:
            best = (p, m)
    return best[1]


def svg(text, module=4, quiet=4):
    """An SVG of the code, `module` px per module with the standard 4-module quiet zone."""
    m = matrix(text)
    n = len(m) + 2 * quiet
    path = "".join(f"M{c + quiet},{r + quiet}h1v1h-1z" for r, row in enumerate(m) for c, x in enumerate(row) if x)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n} {n}" width="{n * module}" height="{n * module}" '
            f'shape-rendering="crispEdges"><rect width="{n}" height="{n}" fill="#fff"/><path d="{path}" fill="#000"/></svg>')
