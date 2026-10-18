"""Ward's own QR encoder: the Reed-Solomon example from the standard, and every code read back module by module."""

import random

import pytest

from ward import qr


def read(matrix: list[list[bool]]) -> bytes:
    """Undo the mask (from the format bits), collect the codewords, check each block's error correction and
    decode the byte-mode payload."""
    ver = (len(matrix) - 17) // 4
    m = qr._Matrix(ver)
    function = m.function
    for mask in range(8):
        probe = qr._Matrix(ver)
        probe.format_bits(mask)
        if all(probe.dark[8][x] == matrix[8][x] for x in range(9)):
            break
    else:
        raise AssertionError("no format bits match")
    m.dark = [list(r) for r in matrix]
    m.apply_mask(mask)
    s = m.size
    bits = []
    right = s - 1
    while right >= 1:
        if right == 6:
            right = 5
        for vert in range(s):
            for j in range(2):
                x = right - j
                y = s - 1 - vert if ((right + 1) & 2) == 0 else vert
                if not function[y][x]:
                    bits.append(int(m.dark[y][x]))
        right -= 2
    words = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits) - 7, 8)]
    nb, el = qr._BLOCKS[ver], qr._ECC_PER_BLOCK[ver]
    raw = qr._raw_modules(ver) // 8
    short = nb - raw % nb
    lens = [raw // nb - el + (0 if i < short else 1) for i in range(nb)]
    blocks: list[list[int]] = [[] for _ in range(nb)]
    k = 0
    for i in range(max(lens)):
        for j in range(nb):
            if i < lens[j]:
                blocks[j].append(words[k])
                k += 1
    for j in range(nb):
        ecc = words[k + j:k + nb * el:nb]
        assert qr._remainder(blocks[j], qr._divisor(el)) == ecc
    data = "".join(format(x, "08b") for b in blocks for x in b)
    assert data[:4] == "0100"
    cb = 8 if ver <= 9 else 16
    n = int(data[4:4 + cb], 2)
    return bytes(int(data[4 + cb + 8 * i:12 + cb + 8 * i], 2) for i in range(n))


def test_reed_solomon_example_from_the_standard():
    data = [32, 91, 11, 120, 209, 114, 220, 77, 67, 64, 236, 17, 236, 17, 236, 17]
    assert qr._remainder(data, qr._divisor(10)) == [196, 35, 39, 119, 235, 215, 231, 226, 93, 23]


@pytest.mark.parametrize("length", [1, 14, 15, 60, 200, 450, 1000, 2000])
def test_codes_read_back(length):
    rng = random.Random(length)
    text = "".join(rng.choice("xmpp:alice@example.org?omemo-sid-1=ab12é") for _ in range(length))
    matrix = qr.qr_matrix(text)
    assert len(matrix) == len(matrix[0]) and (len(matrix) - 17) % 4 == 0
    assert read(matrix) == text.encode()
    for mask in (0, 5):
        assert read(qr.qr_matrix(text, mask)) == text.encode()


def test_too_long():
    with pytest.raises(qr.QrError):
        qr.qr_matrix("x" * 3000)
