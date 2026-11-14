#!/usr/bin/env python3
"""
build_products_csv.py — product.html-এর জন্য পরিষ্কার প্রোডাক্ট CSV
=====================================================================

নতুন `data/products.csv` বানায় যা সরাসরি product.html পড়তে পারে।

কী কী বাদ যায়
--------------
* কলাম: ``Group``, ``Also curated?``, ``Photo Status``, ``Variant Source``,
  ``Colours seen in photo``, ``Photo Note``, ``1688 Links``, ``Notes``
  এবং product.html-এ দরকার নেই এমন সব কলাম
  (``Photos (n)``, ``Review Status``, ``Price Note``, ``Dropped Offer Prices``,
  ``Price Confidence``, ``Variant Note``, ``Feature Note``, ``MOQ``, ``MOQ Note``)
* টেক্সট থেকে: `1688`, ``অবাঞ্জিত``/``অবাঞ্ছিত``, ``[from photo]`` জাতীয়
  আন্তর্জাতিক মার্কার, ``check the 1688 link`` বাক্য।

SKU
---
পুরনো (আগের CSV-র ``SKU Codes``) কোড বাদ। প্রতিটি সারির **প্রথম product id**
কে `sku_codec.py` দিয়ে ৮ অক্ষরের readable code-এ encrypt করা হয় — সেটাই নতুন SKU।
STICKY_SKU_OVERRIDES দিয়ে হাতে করা ব্যতিক্রম রাখা যায়।

ব্যবহার::

    python3 build_products_csv.py            # লিখে দেয় data/products.csv
    python3 build_products_csv.py --check    # কিছু না লিখে সারাংশ দেখায়
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

from sku_codec import decode, encode

# ---------------------------------------------------------------- paths
ROOT = Path(__file__).resolve().parent
SRC_CSV = ROOT / "data" / "three_groups_products.csv"
OUT_CSV = ROOT / "data" / "products.csv"

IMG_COLS = ["Image 1", "Image 2", "Image 3", "Image 4", "Image 5"]

# ---------------------------------------------------------------- rules

#: যে কলামগুলো product.html-এ একেবারেই লাগে না
DROP_COLUMNS = {
    "Group",
    "Also curated?",
    "Photo Status",
    "Variant Source",
    "Colours seen in photo",
    "Photo Note",
    "1688 Links",
    "Notes",
    "Photos (n)",
    "Review Status",
    "Price Note",
    "Dropped Offer Prices",
    "Price Confidence",
    "Variant Note",
    "Feature Note",
    "MOQ",
    "MOQ Note",
}

#: পুরনো SKU কোড বাদ — নতুন SKU encode করে তৈরি হয়
DROP_COLUMNS |= {"SKU Codes"}

#: হাতে ঠিক করা SKU (product id -> sku) — দরকার হলে এখানে যোগ করুন
STICKY_SKU_OVERRIDES: dict[str, str] = {}

#: যে শব্দ/বাক্য টেক্সট থেকে মুছে যাবে
NOISE_PATTERNS = [
    # "check the 1688 link" বাক্যের বাকি অংশ
    r"\s*(?:—|-|–|,)?\s*check the\s+(?:1688\s*)?(?:link|listing)[^.;]*",
    # ক্রেতা যেন সোর্স দাম না দেখে
    r"\bSource price\s*\$?\d[\d.,]*(?:\s*(?:to|–|-)\s*\$?\d[\d.,]*)?\.?",
    r"The page shows a CNY[^.]*\.",
    r"[^.]*\bSource Price\b[^.]*\.",
    r"This offer is from the same supplier as \d+\.[^.]*",
    r"[^.]*\bCNY\b[^.]*\.",
    r"[^.]*cross-checked in the original MHT[^.]*\.",
    r"\bcatalog USD[^.]*\.",
    r"[^.]*original MHT[^.]*\.",
    # রিপোর্টের boilerplate বাক্য
    r"Only what the seller printed[^.]*\.",
    r"performance figures the seller did not state are not invented\.",
    r"Compare the selected variant and included components with the original listing before purchase\.",
    r"\b\d+\s+variants? on the page\.",
    # 1688 কোথাও থাকবে না
    r"1688",
    # [from photo], [from photo + specs], [from specs + title] ...
    r"\[\s*from[^\]]*\]",
    # অবাঞ্জিত / অবাঞ্ছিত (উভয় বানান + জোড়া শব্দ)
    r"অবাঞ্জিত|অবাঞ্ছিত",
    # ইংরেজি সমতুল্য
    r"\(?\s*unwanted\s*\)?",
    # চাইনিজ অক্ষর (ব্র্যান্ডের চাইনিজ নাম) — ইংরেজি নাম তো পাশেই থাকে
    r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+",
    # চাইনিজ যতিচিহ্ন / অসমর্থিত চিহ্ন
    r"[\u3000-\u303f\uff00-\uffef\u00a5]",
]

#: শুধু price অংশ বাদ (variant-এর নাম রাখা হয়)
PRICE_ONLY_PATTERNS = [
    r"\(\s*price not stated\s*\)",
    r"\(\s*price not available\s*\)",
    r"\$\s?\d[\d.,]*(?:\s*(?:to|–|-)\s*\$?\s?\d[\d.,]*)?",
    r"\(\s*\)",
]

#: পুরো অংশটাই বাদ (মূল্য/অফার সংখ্যা — ক্রেতার দেখার দরকার নেই)
JUNK_PART_PATTERNS = [
    r"^other offers\b",
    r"^no spec in title\b",
    r"^no variants?\b",
    r"^variant not identifiable\b",
    r"^specifications? not\b",
]

#: অটো-জেনারেটেড ভরাট টেক্সট (এগুলো বাদ দিলে বর্ণনা খালি হয়ে যায়)
FILLER_SENTENCES = [
    r"Auto-generated from the (?:1688 )?listing title \(product page not checked\)\.",
    r"source price \$?\d[\d.,]*\s*(?:to|–|-)\s*\$?\d[\d.,]*\.?",
    r"No specification stated in the source listing title\b[^.]*",
    r"Category:\s*[^.]*\.",
    r"Filed under\s+[^.\u2014]*[.\u2014]?",
    r"No specification stated in the source listing title\.?",
    r"Exact dimensions, material and technical ratings were not stated in the visible listing text; confirm the selected SKU with the seller\.?",
    r"The accessible listing does not state exact dimensions, material or technical ratings; confirm these for the selected SKU before ordering\.",
    r"confirm the selected SKU with the seller\.?",
    r"confirm these for the selected SKU before ordering\.?",
    r"No feature text is printed on the listing photos and the specification lists nothing beyond colour, so this row carries no feature list[^.]*\.",
    r"ask the supplier for a spec sheet before writing this page\.?",
    r"No feature text on the listing photos or in the title[^.]*\.",
    r"\b\d+\s+variant\(s\);?",
    r"All listed variants\.",
    r"Colour\(s\) shown:[^.]*\.",
    r"Read off the listing photo:",
    r"Listed on the page:",
    r"The visible listing gives these specifications/materials:[^.]*\.",
    r"The listing specifically highlights[^.]*\.",
    r"Visible page specifications:",
    r"The saved (?:offer )?page (?:for offer \d+ )?(?:identifies|names|lists|shows)[^.]*\.",
    r"The saved offer page identifies[^.]*\.",
    r"Compare the selected variant[^.]*\.",
    r"Before buying,[^.]*\.",
    r"Confirm [^.]*before ordering\.",
    r"Confirm [^.]*for the intended[^.]*\.",
    r"A closely matching public-web listing also notes:[^.]*\.",
]

#: বাদ দেওয়া পণ্য (Product Name-এর substring, case-insensitive)
REMOVED_PRODUCTS = [
    "Hot-Selling Product Mystical Soft Toy Box 5-Piece Toy Set",
    "Dumpling-shaped Squeeze Toy Halloween Dumpling Squeeze Soft Cute Toy",
    "Best-Selling New Highland Cow Soft Squeeze Toy",
]

#: যে পণ্যের নির্দিষ্ট ছবি বাদ যাবে  ->  {product name substring: [বাদ যাওয়া image নম্বর]}
REMOVED_IMAGES: dict[str, list[int]] = {
    "Bestseller Nested Box Toy 3D Printed Creative Surprise": [3],
    "Children's Water Gun With Continuous": [1],
    "3D Printed Virus Fidget Toy": [2],
}

#: দামের হিসাব: ছোট পণ্যে $1.00 যোগ, বড় পণ্যে ২০%
PRICE_SMALL_LIMIT = 15.0
PRICE_SMALL_ADD = 1.0
PRICE_MARKUP = 0.20

#: পাবলিক CSV-তে যে কলামগুলো থাকবে।
#: খেয়াল করুন — ``Product ID`` আর ``Source Price`` ইচ্ছাকৃতভাবে বাদ:
#: এই ফাইল GitHub-এ পাবলিক, তাই কাঁচা আইডি বা সাপ্লায়ারের দাম কেউ যেন
#: সরাসরি পড়তে না পারে। আইডি দরকার হলে SKU থেকে ``sku_codec.py`` দিয়ে decode।
OUT_FIELDS = [
    "SKU",
    "Product Name",
    "Category",
    "Model",
    "Price (USD)",
    "Description",
    "Specifications",
    "Features",
    "Variants",
    "Variant Count",
    "Offers (n)",
    "Image 1",
    "Image 2",
    "Image 3",
    "Image 4",
    "Image 5",
    "Image Count",
]


# ---------------------------------------------------------------- helpers
def norm_name(text: str) -> str:
    """তুলনার জন্য নাম normalize।"""
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def clean_text(text: str) -> str:
    """টেক্সট থেকে 1688 / অবাঞ্জিত / [from photo] ইত্যাদি সরায়।"""
    if not text:
        return ""
    out = str(text)
    for pattern in NOISE_PATTERNS:
        out = re.sub(pattern, " ", out, flags=re.IGNORECASE)
    # মার্কার সরানোর পর পড়ে থাকা ফাঁকা বন্ধনী, ডাবল স্পেস, ঝুলে থাকা যতিচিহ্ন
    out = re.sub(r"\(\s*\)|\[\s*\]", " ", out)
    out = re.sub(r"\s*/\s*(?=[,;.]|$)", "", out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"\s+([,;.])", r"\1", out)
    out = re.sub(r"([;,:])(?:\s*[,;:])+", r"\1", out)
    out = re.sub(r"\.{2,}", ".", out)
    out = re.sub(r"^[\s,;.:\-–]+", "", out)
    out = re.sub(r"[\s,;:]+$", "", out)
    return out.strip()


def clean_multiline(text: str) -> str:
    """`;` দিয়ে ভাগ করা লিস্ট — ফাঁকা/অর্থহীন অংশ বাদ দিয়ে আবার জোড়া দেয়।"""
    skip = [
        r"^no specification",
        r"^exact dimensions",
        r"^no feature",
        r"^confirm ",
        r"^ask the supplier",
    ]
    parts = []
    for part in re.split(r"\s*;\s*", text or ""):
        item = clean_text(part)
        if not item:
            continue
        if any(re.search(pat, item, re.I) for pat in skip):
            continue
        if len(item) < 3:
            continue
        parts.append(item)
    # একই অংশ দুইবার এলে একবারই রাখি
    seen, uniq = set(), []
    for p in parts:
        key = p.lower()
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    return "; ".join(uniq)


def clean_variants(text: str) -> str:
    """Variant লিস্ট: দাম/অফার-সংখ্যা বাদ, শুধু নামগুলো থাকে।"""
    parts = []
    for part in re.split(r"\s*\|\s*", text or ""):
        item = part.strip()
        if not item:
            continue
        if any(re.search(pat, item, re.I) for pat in JUNK_PART_PATTERNS):
            continue
        item = clean_text(item)
        for pat in PRICE_ONLY_PATTERNS:
            item = re.sub(pat, "", item, flags=re.I)
        item = re.sub(r"\s*[–\-]\s*$", "", item).strip(" ,;")
        if item:
            parts.append(item)
    return " | ".join(parts)


def tidy_sentences(text: str) -> str:
    """বাক্য-ভিত্তিক পরিষ্কার: ফাঁকা/অর্থহীন বাক্য বাদ, ডাবল স্পেস ঠিক।"""
    text = re.sub(r"\s{2,}", " ", text or "").strip()
    text = re.sub(r"\s+([.,;])", r"\1", text)
    text = re.sub(r"(?:\.\s*){2,}", ". ", text)
    text = re.sub(r"([.;,])\s*([.;,])+", r"\1", text)
    text = re.sub(r"^[\s.;,]+", "", text)
    return text.strip()


def build_description(raw_desc: str, name: str) -> str:
    """বর্ণনা: অটো-জেনারেটেড ভরাট বাক্য বাদ; কিছু না থাকলে টাইটেলই বর্ণনা।"""
    out = raw_desc or ""
    for filler in FILLER_SENTENCES:
        out = re.sub(filler, " ", out, flags=re.I)
    out = clean_text(out)
    out = tidy_sentences(out)
    # প্রথম বাক্যটা যদি টাইটেলেরই পুনরাবৃত্তি হয়, বাদ
    first, _, rest = out.partition(". ")
    if norm_name(first)[:60] == norm_name(name)[:60] and rest:
        out = rest
    out = tidy_sentences(out)
    # খুব ছোট/অসম্পূর্ণ অংশ থেকে গেলে টাইটেলই সবচেয়ে ভালো বর্ণনা
    words = re.findall(r"[A-Za-z]{3,}", out)
    if len(out) < 40 or len(words) < 6 or norm_name(out)[:60] == norm_name(name)[:60]:
        return name
    return out


def first_id(raw_ids: str) -> str:
    """``Product IDs (all variants)`` থেকে প্রথম আইডি।"""
    for part in str(raw_ids or "").split(","):
        digits = re.sub(r"\D", "", part)
        if digits:
            return digits
    return ""


def selling_price(source_price: float) -> float:
    """বিক্রয় মূল্য: ছোট পণ্যে $1, বড় পণ্যে ২০% (পুরনো product.html-এর মতোই)।"""
    if source_price < PRICE_SMALL_LIMIT:
        return round(source_price + PRICE_SMALL_ADD, 2)
    return round(source_price * (1 + PRICE_MARKUP), 2)


def matches(name: str, needle: str) -> bool:
    return norm_name(needle) in norm_name(name)


# ---------------------------------------------------------------- build
def build_rows(src_csv: Path = SRC_CSV) -> tuple[list[dict], dict]:
    with src_csv.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        source_rows = list(reader)
        source_fields = list(reader.fieldnames or [])

    kept_columns = [c for c in source_fields if c not in DROP_COLUMNS]

    stats = {
        "source_rows": len(source_rows),
        "removed_products": [],
        "removed_images": [],
        "kept_columns": kept_columns,
        "dropped_columns": [c for c in source_fields if c in DROP_COLUMNS],
    }

    rows: list[dict] = []
    seen_ids: set[str] = set()

    for raw in source_rows:
        name = clean_text(raw.get("Product Name", ""))

        if any(matches(name, needle) for needle in REMOVED_PRODUCTS):
            stats["removed_products"].append(name)
            continue

        pid = first_id(raw.get("Product IDs (all variants)", ""))
        if not pid:
            stats["removed_products"].append(f"{name} (no product id)")
            continue
        if pid in seen_ids:
            stats["removed_products"].append(f"{name} (duplicate id {pid})")
            continue
        seen_ids.add(pid)

        sku = STICKY_SKU_OVERRIDES.get(pid) or encode(pid)

        # ---- ছবি: বাদ দেওয়া ইনডেক্স সরিয়ে বাকিগুলো টেনে আনা হয়
        images = [(raw.get(col) or "").strip() for col in IMG_COLS]
        drop_idx: set[int] = set()
        for needle, indexes in REMOVED_IMAGES.items():
            if matches(name, needle):
                drop_idx.update(i - 1 for i in indexes)
        images = [u for i, u in enumerate(images) if i not in drop_idx and u]
        if drop_idx:
            stats["removed_images"].append((name, sorted(i + 1 for i in drop_idx)))

        src_min = float(raw.get("Source Price Min (USD)") or 0)
        src_max = float(raw.get("Source Price Max (USD)") or 0) or src_min

        row = {
            # অভ্যন্তরীণ ফিল্ড (CSV-তে যায় না) — সারি সাজানো ও যাচাইয়ের জন্য
            "_id": pid,
            "SKU": sku,
            "Product Name": name,
            "Category": clean_text(raw.get("Category", "")),
            "Model": clean_text(raw.get("Model (different product)", "")),
            "Price (USD)": f"{selling_price(src_min):.2f}",
            "Description": build_description(raw.get("Description", ""), name),
            "Specifications": clean_multiline(raw.get("Specifications", "")),
            "Features": clean_multiline(raw.get("Features", "")),
            "Variants": clean_variants(raw.get("Variants (name – price)", "")),
            "Variant Count": (raw.get("Variant Count") or "").strip(),
            "Offers (n)": (raw.get("Offers (n)") or "").strip(),
            "Image 1": images[0] if len(images) > 0 else "",
            "Image 2": images[1] if len(images) > 1 else "",
            "Image 3": images[2] if len(images) > 2 else "",
            "Image 4": images[3] if len(images) > 3 else "",
            "Image 5": images[4] if len(images) > 4 else "",
            "Image Count": str(len(images)),
        }
        rows.append(row)

    # SKU যেন ইউনিক থাকে
    dupes = {r["SKU"] for r in rows if [x["SKU"] for x in rows].count(r["SKU"]) > 1}
    if dupes:
        raise SystemExit(f"duplicate SKUs generated: {sorted(dupes)}")

    rows.sort(key=lambda r: int(r["_id"]))
    stats["rows"] = len(rows)
    return rows, stats


def write_csv(rows: list[dict], out_csv: Path = OUT_CSV) -> None:
    """শুধু OUT_FIELDS গুলোই লেখা হয় — ``_`` দিয়ে শুরু হওয়া ফিল্ড বাদ পড়ে।"""
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(
            fh, fieldnames=OUT_FIELDS, quoting=csv.QUOTE_MINIMAL, extrasaction="ignore"
        )
        writer.writeheader()
        writer.writerows(rows)


def report(rows: list[dict], stats: dict) -> None:
    print("=" * 68)
    print(f"source rows            : {stats['source_rows']}")
    print(f"removed products       : {len(stats['removed_products'])}")
    for name in stats["removed_products"]:
        print(f"    - {name}")
    print(f"images dropped         : {len(stats['removed_images'])}")
    for name, idx in stats["removed_images"]:
        print(f"    - {name[:52]:<52} images {idx}")
    print(f"kept rows              : {len(rows)}")
    print(f"dropped columns ({len(stats['dropped_columns'])}): {', '.join(stats['dropped_columns'])}")
    print(f"output columns ({len(OUT_FIELDS)})")
    cjk = [r["SKU"] for r in rows if re.search(r"[\u4e00-\u9fff]", " ".join(r.values()))]
    print(f"rows still containing CJK: {len(cjk)}")
    noise = [
        r["SKU"]
        for r in rows
        if re.search(r"1688|অবাঞ্জিত|অবাঞ্ছিত|from photo", " ".join(r.values()), re.I)
    ]
    print(f"rows still containing noise words: {len(noise)} {noise[:5]}")
    for r in rows:
        assert decode(r["SKU"]) == r["_id"], r
    print("sku <-> product id round trip: OK")
    print("=" * 68)


def main() -> None:
    p = argparse.ArgumentParser(description="Build data/products.csv for product.html")
    p.add_argument("--input", default=str(SRC_CSV), help="source CSV (default: data/three_groups_products.csv)")
    p.add_argument("--output", default=str(OUT_CSV), help="output CSV (default: data/products.csv)")
    p.add_argument("--check", action="store_true", help="শুধু সারাংশ দেখাও, ফাইল লিখো না")
    args = p.parse_args()

    rows, stats = build_rows(Path(args.input))
    report(rows, stats)
    if not args.check:
        write_csv(rows, Path(args.output))
        print(f"written -> {args.output}")


if __name__ == "__main__":
    sys.exit(main())
