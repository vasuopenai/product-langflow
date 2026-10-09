"""Barcode normalization so Kroger, USDA and Open Food Facts codes can be joined.

Sources write the same product differently:
  USDA FDC   gtin_upc "00011110417008" (GTIN-14, with check digit)
  OFF        code     "0011110417008" or "11110417008"
  Kroger API upc      "0001111041700" (13 digits, zero padded, reportedly
                      WITHOUT the check digit)

Canonical key: digits only, leading zeros stripped, check digit included.
"""


def digits(code):
    return "".join(ch for ch in str(code or "") if ch.isdigit())


def check_digit(body):
    """GS1 check digit for a code without its check digit."""
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return str((10 - total % 10) % 10)


def has_valid_check(code):
    d = digits(code)
    return len(d) >= 8 and check_digit(d[:-1]) == d[-1]


def key(code):
    """Canonical key for a code that already carries its check digit (USDA, OFF)."""
    return digits(code).lstrip("0") or None


def to_kroger_id(code):
    """Kroger product id for a barcode that carries its check digit (USDA, OFF):
    the code without the check digit, zero-padded to 13 digits."""
    d = digits(code).lstrip("0")
    if len(d) < 6:
        return None
    # A code whose last digit isn't a valid check digit was stored without one.
    return (d[:-1] if has_valid_check(d) else d).zfill(13)


def kroger_keys(upc):
    """Candidate keys for a Kroger UPC, most likely first.

    Kroger usually omits the check digit, so append a computed one. A code
    whose last digit happens to be a valid check digit is ambiguous, so
    the as-is reading is also returned as a fallback.
    """
    d = digits(upc).lstrip("0")
    if not d:
        return []
    keys = [d + check_digit(d)]
    if has_valid_check(d):
        keys.append(d)
    return keys
