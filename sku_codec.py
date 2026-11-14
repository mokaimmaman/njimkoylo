#!/usr/bin/env python3
"""
sku_codec.py — Product-ID का छोटा, readable, reversible SKU code
=================================================================

প্রোডাক্ট আইডি (যেমন ``1078479825767``) থেকে ছোট SKU বানায়, আর সেই SKU
থেকে আবার হুবহু আইডি ফেরত আনে।

কীভাবে কাজ করে (গণিতটা সহজ, তাই "বেশি কঠিন নয়"):

1. আইডিকে একটা বড় সংখ্যা ধরা হয়: ``x``
2. key দিয়ে একটা affine transform: ``y = (x * A + B) mod M``
3. ``y``-কে base-36 (0-9, A-Z) দিয়ে লেখা হয় → ৮ অক্ষরের কোড

কারণ ``A`` আর ``M`` সহ-মৌলিক (coprime), তাই modular inverse দিয়ে
উল্টোটা করা যায়: ``x = ((y - B) * A⁻¹) mod M``।

এখানে ``M = 36^8 = 2821109907456``, যা ১৩ ডিজিটের সব 1688 আইডির চেয়ে
বড় — তাই কোনো তথ্য হারায় না এবং কোড কখনোই ৮ অক্ষরের বেশি হয় না।

ব্যবহার::

    python3 sku_codec.py encode 1078479825767
    python3 sku_codec.py decode K3F9A2Q1
"""

from __future__ import annotations

import argparse
import re

# ---------------------------------------------------------------- settings
ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"  # base-36
BASE = len(ALPHABET)                                # 36
WIDTH = 8                                           # SKU length
MODULUS = BASE ** WIDTH                             # 36^8 = 2821109907456

# key (এই দুটো সংখ্যাই আসল "key")
KEY_A = 48271        # মৌলিক, তাই 36-এর সাথে সহ-মৌলিক
KEY_B = 104729       # offset

_KEY_A_INV = pow(KEY_A, -1, MODULUS)   # Python 3.8+


# ---------------------------------------------------------------- helpers
def _to_base36(value: int, width: int = WIDTH) -> str:
    """সংখ্যা → base-36 স্ট্রিং (বাঁ দিকে শূন্য দিয়ে width পর্যন্ত)।"""
    if value < 0 or value >= MODULUS:
        raise ValueError(f"value out of range: {value}")
    digits = []
    n = value
    while n:
        n, r = divmod(n, BASE)
        digits.append(ALPHABET[r])
    if not digits:
        digits = ["0"]
    return "".join(reversed(digits)).rjust(width, "0")


def _from_base36(text: str) -> int:
    """base-36 স্ট্রিং → সংখ্যা।"""
    value = 0
    for ch in text.upper():
        idx = ALPHABET.find(ch)
        if idx < 0:
            raise ValueError(f"invalid base-36 character: {ch!r}")
        value = value * BASE + idx
    return value


# ---------------------------------------------------------------- public API
def encode(product_id: str | int) -> str:
    """1688 প্রোডাক্ট আইডি → ৮ অক্ষরের SKU কোড (0-9A-Z)।"""
    digits = re.sub(r"\D", "", str(product_id))
    if not digits:
        raise ValueError(f"product id has no digits: {product_id!r}")
    x = int(digits)
    if x >= MODULUS:
        raise ValueError(f"product id too large for {WIDTH}-char code: {x}")
    return _to_base36((x * KEY_A + KEY_B) % MODULUS)


def decode(sku: str) -> str:
    """SKU কোড → আসল প্রোডাক্ট আইডি (স্ট্রিং, ডিজিট فقط)।"""
    code = re.sub(r"[^0-9A-Za-z]", "", str(sku))
    if not code:
        raise ValueError("empty sku")
    y = _from_base36(code)
    x = ((y - KEY_B) * _KEY_A_INV) % MODULUS
    return str(x)


def is_sku(text: str) -> bool:
    """দেখতে SKU-এর মতো কি না (৮ অক্ষর, শুধু 0-9A-Z)।"""
    return bool(re.fullmatch(r"[0-9A-Z]{8}", str(text or "").strip()))


# ---------------------------------------------------------------- CLI
def main() -> None:
    p = argparse.ArgumentParser(
        description="Encode 1688 product IDs into short readable SKUs and decode them back."
    )
    p.add_argument("action", choices=["encode", "decode"])
    p.add_argument("values", nargs="+", help="product ids (encode) or sku codes (decode)")
    args = p.parse_args()

    for value in args.values:
        if args.action == "encode":
            print(f"{value} -> {encode(value)}")
        else:
            print(f"{value} -> {decode(value)}")


if __name__ == "__main__":
    main()
