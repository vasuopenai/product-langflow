"""Barcode normalization for what phone cameras read (UPC-A, UPC-E, EAN-13, EAN-8)."""

import re


def digits(code):
    return re.sub(r"\D", "", code or "")


def check_digit(body):
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return str((10 - total % 10) % 10)


def upce_to_upca(code):
    """Expand an 8-digit UPC-E (number system 0/1) to its 12-digit UPC-A."""
    d = digits(code)
    if len(d) != 8 or d[0] not in "01":
        return None
    ns, m, check = d[0], d[1:7], d[7]
    last = m[5]
    if last in "012":
        body = ns + m[0:2] + last + "0000" + m[2:5]
    elif last == "3":
        body = ns + m[0:3] + "00000" + m[3:5]
    elif last == "4":
        body = ns + m[0:4] + "00000" + m[4]
    else:
        body = ns + m[0:5] + "0000" + last
    return body + check if check_digit(body) == check else None


def key(code):
    """Canonical key: digits, UPC-E expanded, leading zeros stripped."""
    d = digits(code)
    if len(d) == 8:
        d = upce_to_upca(d) or d
    return d.lstrip("0") or None


def store_variants(code):
    """The spellings the product store may use for this barcode (codes are USDA GTINs,
    zero-padded to at least 12 digits)."""
    k = key(code)
    if not k:
        return []
    return list(dict.fromkeys([k.zfill(n) for n in (12, 13, 14) if len(k) <= n] + [k]))
