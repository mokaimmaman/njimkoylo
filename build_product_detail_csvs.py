#!/usr/bin/env python3
"""
three_groups_products_cleaned.csv-এর প্রতিটি পণ্যের জন্য ডিটেইল CSV বানায়
=============================================================================

ফাইলের নাম আসে ``data/products.csv``-এর ``SKU`` থেকে::

    data/three_groups_products_cleaned.csv  (সঠিক পরিচ্ছন্ন সোর্স)
        │  build_products_csv.py-এর একই পরিচ্ছন্নতা + SKU ম্যাপিং
        ▼
    data/products/<SKU>.csv                (শুধু product-detail.html-এর প্রয়োজনীয় কলাম)

প্রতিটি ফাইলে header + ঠিক একটিই data row থাকে। সোর্সের অভ্যন্তরীণ কলাম
(Group, Review Status, 1688 Links, Source Price, Product IDs, নোট ইত্যাদি)
এখানে আসে না — ``build_products_csv.OUT_FIELDS``-ই whitelist।

ব্যবহার::

    python3 build_product_detail_csvs.py
    python3 build_product_detail_csvs.py --check
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

import build_products_csv as builder

ROOT = Path(__file__).resolve().parent
SOURCE_CSV = ROOT / "data" / "three_groups_products_cleaned.csv"
CATALOG_CSV = ROOT / "data" / "products.csv"
OUT_DIR = ROOT / "data" / "products"

DETAIL_FIELDS = list(builder.OUT_FIELDS)
SAFE_SKU = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def read_catalog(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        rows = [
            {field: str(row.get(field, "") or "") for field in DETAIL_FIELDS}
            for row in reader
        ]
    if not rows:
        raise SystemExit(f"catalog is empty: {path}")
    skus = [row["SKU"].strip() for row in rows]
    if any(not SAFE_SKU.fullmatch(sku) for sku in skus):
        raise SystemExit("catalog contains an unsafe/empty SKU")
    if len(set(skus)) != len(skus):
        raise SystemExit("catalog contains duplicate SKUs")
    return rows


def build_detail_rows(source: Path, catalog: list[dict[str, str]]) -> tuple[list[dict[str, str]], dict]:
    cleaned, stats = builder.build_rows(source)
    by_sku: dict[str, dict[str, str]] = {}
    for row in cleaned:
        by_sku[row["SKU"]] = {
            field: str(row.get(field, "") or "") for field in DETAIL_FIELDS
        }

    catalog_skus = {row["SKU"].strip() for row in catalog}
    out_rows: list[dict[str, str]] = []
    fallback_skus: list[str] = []

    # products.csv-ই SKU-র মূল উৎস; তার ক্রম ধরে রাখি যেন products.html-এর
    # প্রতিটি কার্ডের জন্য নিশ্চিতভাবে একটি ডিটেইল ফাইল থাকে।
    for entry in catalog:
        sku = entry["SKU"].strip()
        row = by_sku.get(sku)
        if row is None:
            row = dict(entry)
            fallback_skus.append(sku)
        row["SKU"] = sku
        out_rows.append(row)

    skipped_skus = sorted(set(by_sku) - catalog_skus)
    report = {
        "source_rows": stats["source_rows"],
        "cleaned_rows": len(cleaned),
        "catalog_rows": len(catalog),
        "output_rows": len(out_rows),
        "fallback_skus": fallback_skus,
        "skipped_skus": skipped_skus,
        "dropped_columns": stats["dropped_columns"],
        "output_columns": DETAIL_FIELDS,
    }
    return out_rows, report


def write_detail_csvs(rows: list[dict[str, str]], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    wanted = {row["SKU"] + ".csv" for row in rows}

    for row in rows:
        sku = row["SKU"]
        path = out_dir / (sku + ".csv")
        with path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(
                fh,
                fieldnames=DETAIL_FIELDS,
                quoting=csv.QUOTE_MINIMAL,
                extrasaction="ignore",
                lineterminator="\n",
            )
            writer.writeheader()
            writer.writerow({field: row.get(field, "") or "" for field in DETAIL_FIELDS})

    # পুরনো/অপ্রচলিত SKU ফাইল থেকে যেন ভুল তথ্য না দেখাই।
    for path in out_dir.glob("*.csv"):
        if path.name not in wanted:
            path.unlink()


def verify_detail_csvs(rows: list[dict[str, str]], out_dir: Path) -> None:
    for row in rows:
        sku = row["SKU"]
        path = out_dir / (sku + ".csv")
        with path.open(encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            if list(reader.fieldnames or []) != DETAIL_FIELDS:
                raise SystemExit(f"unexpected columns in {path}")
            data = list(reader)
        if len(data) != 1:
            raise SystemExit(f"expected exactly one data row in {path}")
        if data[0]["SKU"].strip() != sku:
            raise SystemExit(f"SKU does not match filename: {path}")


def report(rows: list[dict[str, str]], info: dict) -> None:
    print("=" * 68)
    print(f"source rows            : {info['source_rows']}")
    print(f"cleaned source rows    : {info['cleaned_rows']}")
    print(f"catalog SKUs           : {info['catalog_rows']}")
    print(f"detail CSV files        : {info['output_rows']}")
    print(f"catalog-only fallbacks : {len(info['fallback_skus'])}")
    if info["fallback_skus"]:
        print("    " + ", ".join(info["fallback_skus"][:8]) + (" ..." if len(info["fallback_skus"]) > 8 else ""))
    print(f"source rows without SKU: {len(info['skipped_skus'])}")
    if info["skipped_skus"]:
        print("    " + ", ".join(info["skipped_skus"][:8]) + (" ..." if len(info["skipped_skus"]) > 8 else ""))
    print(f"dropped source columns : {len(info['dropped_columns'])}")
    print(f"kept columns ({len(info['output_columns'])}): {', '.join(info['output_columns'])}")
    print("=" * 68)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build one data/products/<SKU>.csv for every SKU in data/products.csv"
    )
    parser.add_argument("--source", default=str(SOURCE_CSV), help="curated three_groups_products.csv")
    parser.add_argument("--catalog", default=str(CATALOG_CSV), help="products.csv (SKU authority)")
    parser.add_argument("--out-dir", default=str(OUT_DIR), help="per-SKU CSV directory")
    parser.add_argument("--check", action="store_true", help="report only; do not write files")
    args = parser.parse_args()

    source = Path(args.source)
    catalog_path = Path(args.catalog)
    out_dir = Path(args.out_dir)

    catalog = read_catalog(catalog_path)
    rows, info = build_detail_rows(source, catalog)
    report(rows, info)

    if args.check:
        return

    write_detail_csvs(rows, out_dir)
    verify_detail_csvs(rows, out_dir)
    print(f"written -> {out_dir} ({len(rows)} CSV files)")


if __name__ == "__main__":
    sys.exit(main())
