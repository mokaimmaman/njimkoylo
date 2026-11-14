# প্রোডাক্ট CSV + product.html (নতুন সিস্টেম)

এই ফোল্ডারের `products.csv` হলো **একমাত্র পাবলিক প্রোডাক্ট CSV** — `products.html` সরাসরি
GitHub থেকে এটাই লোড করে। হাতে এডিট করার দরকার নেই; স্ক্রিপ্ট দিয়ে তৈরি হয়।

```
data/three_groups_products.csv   ← কাঁচা সোর্স (সাপ্লায়ার রিপোর্ট)
        │  python3 build_products_csv.py
        ▼
data/products.csv                ← পাবলিক CSV (SKU + পরিচ্ছন্ন টেক্সট + ছবি)
        │  products.html  fetch()
        ▼
সাইটে প্রোডাক্ট গ্রিড
```

## ১. কী কী বাদ যায়

**কলাম** (`build_products_csv.py` → `DROP_COLUMNS`):

`Group`, `Also curated?`, `Photo Status`, `Variant Source`, `Colours seen in photo`,
`Photo Note`, `1688 Links`, `Notes`, `Photos (n)`, `Review Status`, `Price Note`,
`Dropped Offer Prices`, `Price Confidence`, `Variant Note`, `Feature Note`, `MOQ`,
`MOQ Note`, `SKU Codes` (পুরনো কোড) — অর্থাৎ product.html-এ যেগুলো লাগে না, সব।

**টেক্সট থেকে** (`NOISE_PATTERNS`): `1688`, `অবাঞ্জিত` / `অবাঞ্ছিত`,
`[from photo]`, `[from photo + specs]` ইত্যাদি মার্কার, `check the 1688 link`,
`Source price $x to $y`, চাইনিজ অক্ষর ও চাইনিজ যতিচিহ্ন।

`Variants` কলাম থেকে দাম ও "N offers" অংশ বাদ যায় — শুধু ভ্যারিয়েন্টের নাম থাকে।

## ২. বাদ দেওয়া পণ্য ও ছবি

`REMOVED_PRODUCTS` (পুরো পণ্য বাদ):

* Hot-Selling Product Mystical Soft Toy Box 5-Piece Toy Set
* Dumpling-shaped Squeeze Toy Halloween Dumpling Squeeze Soft Cute Toy
* Best-Selling New Highland Cow Soft Squeeze Toy

`REMOVED_IMAGES` (শুধু নির্দিষ্ট ছবি বাদ, বাকিগুলো এক ঘর করে এগিয়ে আসে):

| পণ্য | বাদ যাওয়া ছবি |
|---|---|
| Bestseller Nested Box Toy 3D Printed Creative Surprise | Image 3 |
| Children's Water Gun With Continuous | Image 1 |
| 3D Printed Virus Fidget Toy | Image 2 |

## ৩. নতুন SKU (encrypted, readable)

পুরনো `SKU Codes` বাদ। প্রতিটি সারির **প্রথম product id** নিয়ে `sku_codec.py` দিয়ে
৮ অক্ষরের কোড বানানো হয় (0-9, A-Z):

```
y = (id × 48271 + 104729) mod 36^8     → base36 → "HC3W20MA"
id = ((y − 104729) × 48271⁻¹) mod 36^8
```

* key = `KEY_A = 48271`, `KEY_B = 104729`, `MODULUS = 36^8` (`sku_codec.py`-তে)
* ছোট, পড়ার উপযোগী, লেটার কম — কিন্তু key ছাড়া কেউ আইডি বের করতে পারবে না
* কোনো তথ্য হারায় না: সব ১২–১৩ ডিজিটের 1688 আইডি হুবহু ফেরত আসে

```bash
python3 sku_codec.py encode 1078479825767     # 1078479825767 -> HC3W20MA
python3 sku_codec.py decode HC3W20MA          # HC3W20MA -> 1078479825767
```

## ৪. পাবলিক CSV-তে যা ইচ্ছাকৃতভাবে নেই

`Product ID` আর `Source Price Min/Max` পাবলিক ফাইলে রাখা হয়নি — ফাইলটা GitHub-এ
সবাই পড়তে পারে, তাই কাঁচা আইডি বা সাপ্লায়ারের দাম সরাসরি দেখা যাবে না।
আইডি দরকার হলে SKU decode করুন; দাম দরকার হলে সোর্স CSV-ই আছে।

## ৫. product.html কীভাবে লোড করে

```js
GH_REPO   = 'mokaimmaman/njimkoylo'
GH_BRANCH = 'main'
CSV_PATH  = 'data/products.csv'
```

তিনটি সোর্স ক্রমান্বয়ে চেষ্টা করা হয়:

1. `https://raw.githubusercontent.com/mokaimmaman/njimkoylo/main/data/products.csv`
2. `https://cdn.jsdelivr.net/gh/mokaimmaman/njimkoylo@main/data/products.csv`
3. `data/products.csv` (একই ফোল্ডারে থাকলে — `products.html?csv=data/products.csv` দিয়েও দেওয়া যায়)

UI/ডিজাইন আগের মতোই আছে; শুধু ডেটা এখন লাইভ CSV থেকে আসে (SKU, নাম, দাম,
ক্যাটাগরি, প্রথম ছবি)। CSV বদলালে সাইটে পরের রিলোডেই নতুন ডেটা দেখা যাবে।

## ৬. আবার বানানো

```bash
python3 build_products_csv.py            # data/products.csv নতুন করে লেখে
python3 build_products_csv.py --check    # শুধু সারাংশ দেখায়
```

`data/three_groups_products.csv` বদলালে GitHub Action
(**Rebuild Product CSV**) নিজে থেকেই CSV আবার বানিয়ে কমিট করে দেয়।
