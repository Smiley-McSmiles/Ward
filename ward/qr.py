"""A small QR code encoder (byte mode, error correction level M), so QR codes need no extra package.

It follows ISO/IEC 18004 the way Project Nayuki's MIT-licensed "QR Code generator" library does: pick the smallest
version that fits, add Reed-Solomon error correction per block, interleave, place the modules, and choose the
mask with the lowest penalty.  qr_matrix(text) returns rows of booleans (True = dark), without the quiet zone."""

from __future__ import annotations

# error correction level M, indexed by version (1-40)
_ECC_PER_BLOCK = (-1, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26, 30, 22, 22, 24, 24, 28, 28, 26, 26, 26, 26, 28, 28,
                  28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28)
_BLOCKS = (-1, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5, 5, 8, 9, 9, 10, 10, 11, 13, 14, 16, 17, 17, 18, 20, 21, 23, 25, 26, 28,
           29, 31, 33, 35, 37, 38, 40, 43, 45, 47, 49)
_FORMAT_M = 0       # the two error correction bits of level M


class QrError(ValueError):
    pass


def _raw_modules(ver: int) -> int:
    """Modules available for data and error correction (everything but function patterns)."""
    result = (16 * ver + 128) * ver + 64
    if ver >= 2:
        n = ver // 7 + 2
        result -= (25 * n - 10) * n - 55
        if ver >= 7:
            result -= 36
    return result


def _data_codewords(ver: int) -> int:
    return _raw_modules(ver) // 8 - _ECC_PER_BLOCK[ver] * _BLOCKS[ver]


# ------------------------------------------------------------------ Reed-Solomon over GF(256), polynomial 0x11D
def _multiply(x: int, y: int) -> int:
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _divisor(degree: int) -> list[int]:
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _multiply(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _multiply(root, 0x02)
    return result


def _remainder(data: list[int], divisor: list[int]) -> list[int]:
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _multiply(coef, factor)
    return result


# ------------------------------------------------------------------ encoding
def _codewords(data: bytes) -> tuple[int, list[int]]:
    for ver in range(1, 41):
        cap = _data_codewords(ver) * 8
        count_bits = 8 if ver <= 9 else 16
        if 4 + count_bits + len(data) * 8 <= cap:
            break
    else:
        raise QrError("too much data for a QR code")
    bits: list[int] = []

    def put(value: int, n: int) -> None:
        bits.extend((value >> i) & 1 for i in reversed(range(n)))
    put(0b0100, 4)                 # byte mode
    put(len(data), count_bits)
    for b in data:
        put(b, 8)
    put(0, min(4, cap - len(bits)))
    put(0, -len(bits) % 8)
    pad = 0xEC
    while len(bits) < cap:
        put(pad, 8)
        pad ^= 0xEC ^ 0x11
    words = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]

    # split into blocks, add error correction, interleave
    nblocks, ecc_len = _BLOCKS[ver], _ECC_PER_BLOCK[ver]
    raw = _raw_modules(ver) // 8
    short_blocks = nblocks - raw % nblocks
    short_len = raw // nblocks
    div = _divisor(ecc_len)
    blocks = []
    k = 0
    for i in range(nblocks):
        dat = words[k:k + short_len - ecc_len + (0 if i < short_blocks else 1)]
        k += len(dat)
        ecc = _remainder(dat, div)
        if i < short_blocks:
            dat = [*dat, -1]        # placeholder, so all blocks line up when interleaving
        blocks.append(dat + ecc)
    out = []
    for i in range(len(blocks[0])):
        for j, block in enumerate(blocks):
            if i != short_len - ecc_len or j >= short_blocks:
                out.append(block[i])
    return ver, out


def _alignment_positions(ver: int) -> list[int]:
    if ver == 1:
        return []
    n = ver // 7 + 2
    step = 26 if ver == 32 else (ver * 4 + n * 2 + 1) // (n * 2 - 2) * 2
    size = ver * 4 + 17
    return [6, *[size - 7 - i * step for i in range(n - 1)][::-1]]


class _Matrix:
    def __init__(self, ver: int):
        self.ver = ver
        self.size = ver * 4 + 17
        self.dark = [[False] * self.size for _ in range(self.size)]
        self.function = [[False] * self.size for _ in range(self.size)]
        self._function_patterns()

    def set(self, x: int, y: int, dark: bool) -> None:
        self.dark[y][x] = dark
        self.function[y][x] = True

    def _function_patterns(self) -> None:
        s = self.size
        for i in range(s):
            self.set(6, i, i % 2 == 0)
            self.set(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (s - 4, 3), (3, s - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < s and 0 <= y < s:
                        self.set(x, y, max(abs(dx), abs(dy)) not in (2, 4))
        pos = _alignment_positions(self.ver)
        for i, a in enumerate(pos):
            for j, b in enumerate(pos):
                if (i, j) in ((0, 0), (0, len(pos) - 1), (len(pos) - 1, 0)):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self.set(a + dx, b + dy, max(abs(dx), abs(dy)) != 1)
        self.format_bits(0)         # reserve; drawn for real once the mask is chosen
        if self.ver >= 7:
            rem = self.ver
            for _ in range(12):
                rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
            bits = self.ver << 12 | rem
            for i in range(18):
                bit = (bits >> i) & 1 == 1
                a, b = s - 11 + i % 3, i // 3
                self.set(a, b, bit)
                self.set(b, a, bit)

    def format_bits(self, mask: int) -> None:
        data = _FORMAT_M << 3 | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412
        s = self.size

        def bit(i: int) -> bool:
            return (bits >> i) & 1 == 1
        for i in range(6):
            self.set(8, i, bit(i))
        self.set(8, 7, bit(6))
        self.set(8, 8, bit(7))
        self.set(7, 8, bit(8))
        for i in range(9, 15):
            self.set(14 - i, 8, bit(i))
        for i in range(8):
            self.set(s - 1 - i, 8, bit(i))
        for i in range(8, 15):
            self.set(8, s - 15 + i, bit(i))
        self.set(8, s - 8, True)    # the dark module

    def place(self, words: list[int]) -> None:
        s = self.size
        i = 0
        total = len(words) * 8
        right = s - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(s):
                for j in range(2):
                    x = right - j
                    upward = ((right + 1) & 2) == 0
                    y = s - 1 - vert if upward else vert
                    if not self.function[y][x] and i < total:
                        self.dark[y][x] = (words[i >> 3] >> (7 - (i & 7))) & 1 == 1
                        i += 1
            right -= 2

    def apply_mask(self, mask: int) -> None:
        tests = (lambda x, y: (x + y) % 2 == 0, lambda x, y: y % 2 == 0, lambda x, y: x % 3 == 0,
                 lambda x, y: (x + y) % 3 == 0, lambda x, y: (x // 3 + y // 2) % 2 == 0,
                 lambda x, y: x * y % 2 + x * y % 3 == 0, lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
                 lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0)
        test = tests[mask]
        for y in range(self.size):
            for x in range(self.size):
                if not self.function[y][x] and test(x, y):
                    self.dark[y][x] = not self.dark[y][x]

    def penalty(self) -> int:
        s = self.size
        score = 0
        for lines in (self.dark, [list(c) for c in zip(*self.dark, strict=True)]):
            for line in lines:
                run_color, run = False, 0
                history = [0] * 7
                for dark in line:
                    if dark == run_color:
                        run += 1
                        if run == 5:
                            score += 3
                        elif run > 5:
                            score += 1
                    else:
                        _push(history, run, s)
                        if not run_color:
                            score += _finder_like(history) * 40
                        run_color, run = dark, 1
                score += _terminate(history, run_color, run, s) * 40
        for y in range(s - 1):
            for x in range(s - 1):
                c = self.dark[y][x]
                if c == self.dark[y][x + 1] == self.dark[y + 1][x] == self.dark[y + 1][x + 1]:
                    score += 3
        dark = sum(sum(r) for r in self.dark)
        total = s * s
        k = (abs(dark * 20 - total * 10) + total - 1) // total - 1
        score += k * 10
        return score


def _push(history: list[int], run: int, size: int) -> None:
    if history[0] == 0:
        run += size         # the light border before the first module
    history.pop()
    history.insert(0, run)


def _finder_like(h: list[int]) -> int:
    n = h[1]
    core = n > 0 and h[2] == h[4] == h[5] == n and h[3] == n * 3
    return (1 if core and h[0] >= n * 4 and h[6] >= n else 0) + (1 if core and h[6] >= n * 4 and h[0] >= n else 0)


def _terminate(history: list[int], color: bool, run: int, size: int) -> int:
    if color:
        _push(history, run, size)
        run = 0
    run += size
    _push(history, run, size)
    return _finder_like(history)


def qr_matrix(text: str, mask: int | None = None) -> list[list[bool]]:
    """mask: force one of the eight masks (for tests); by default the one with the lowest penalty."""
    ver, words = _codewords(text.encode("utf-8"))
    best, best_score = None, None
    for candidate in range(8) if mask is None else [mask]:
        m = _Matrix(ver)
        m.place(words)
        m.apply_mask(candidate)
        m.format_bits(candidate)
        score = m.penalty()
        if best_score is None or score < best_score:
            best, best_score = m, score
    assert best is not None
    return best.dark
