# প্রোডাক্ট CSV + products.html + product-detail.html

এই ফোল্ডারে এখন দুই ধরনের পাবলিক CSV আছে:

* `data/products.csv` — `products.html`-এর পুরো ক্যাটালগ (SKU, নাম, দাম, ক্যাটাগরি, ছবি)।
* `data/products/<SKU>.csv` — প্রতিটি SKU-র জন্য একটি করে ডিটেইল ফাইল; `product-detail.html` এটি লোড করে।

```
data/three_groups_products.csv   ← কিউরেটেড কাঁচা সোর্স (৩৬ কলাম)
        │  python3 build_products_csv.py
        ▼
data/products.csv                ← ৬৯১টি SKU + ১৭টি পরিচ্ছন্ন কলাম
        │  python3 build_product_detail_csvs.py
        ▼
data/products/<SKU>.csv          ← ৬৯১টি ফাইল; প্রতিটিতে header + ১টি সারি
        │  product-detail.html?sku=<SKU>  →  GitHub raw/jsDelivr fetch()
        ▼
নির্দিষ্ট পণ্যের ডিটেইল পেইজ
```

`three_groups_products.csv`-এর আপলোড করা ভার্সনে ৬৫৩টি সারি ছিল, কিন্তু
`products.csv`-এ ৬৯১টি SKU আছে। ৪১টি অনুপস্থিত সারি আগের রিপো-সোর্স থেকে যোগ
করা হয়েছে, যেন `products.html`-এর **প্রতিটি কার্ডের** জন্য ডিটেইল CSV থাকে।
SKU-র ক্রম ও কোড আগের `products.csv`-এর সাথে হুবহু মিলে যায়। যে ৩টি পণ্য
ইচ্ছাকৃতভাবে বাদ দেওয়া হয় তাদের কোনো SKU নেই, তাই তাদের ফাইলও তৈরি হয় না।

## ১. কী কী বাদ যায়

**কলাম** (`build_products_csv.py` → `DROP_COLUMNS` + `OUT_FIELDS` whitelist): `Group`, `Also curated?`,
`Photo Status`, `SKU Codes` (পুরনো কোড), `Photos (n)`, `Product IDs (all variants)`,
`Source Price Min/Max (USD)`, `Variant Source`, `Colours seen in photo`,
`Photo Note`, `1688 Links`, `Review Status`, `Notes`, `Price Note`,
`Dropped Offer Prices`, `Price Confidence`, `Variant Note`, `Feature Note`,
`MOQ`, `MOQ Note` — অর্থাৎ ক্রেতার পেইজে যেগুলো লাগে না, সব। ৩৬টি কাঁচা
কলাম থেকে ১৭টি পাবলিক কলাম রাখা হয়:

`SKU`, `Product Name`, `Category`, `Model`, `Price (USD)`, `Description`,
`Specifications`, `Features`, `Variants`, `Variant Count`, `Offers (n)`,
`Image 1` … `Image 5`, `Image Count`।

**টেক্সট থেকে** (`NOISE_PATTERNS`): `1688`, `অবাঞ্জিত` / `অবাঞ্ছিত`,
`[from photo]`, `[from photo + specs]` ইত্যাদি মার্কার, `check the 1688 link`,
`Source price $x to $y`, চাইনিজ অক্ষর ও চাইনিজ যতিচিহ্ন।

`Variants` কলাম থেকে দাম ও "N offers" অংশ বাদ যায় — শুধু ভ্যারিয়েন্টের নাম থাকে।
`Product ID` আর সাপ্লায়ারের কাঁচা দাম পাবলিক ফাইলে রাখা হয়নি; GitHub-এ ফাইলগুলো
সবার পড়ার যোগ্য, তাই শুধু বিক্রয়মূল্য `Price (USD)`-ই থাকে।

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
* ছোট, পড়ার উপযোগী — কিন্তু key ছাড়া কেউ আইডি বের করতে পারবে না
* কোনো তথ্য হারায় না: সব ১২–১৩ ডিজিটের 1688 আইডি হুবহু ফেরত আসে

```bash
python3 sku_codec.py encode 1078479825767     # 1078479825767 -> HC3W20MA
python3 sku_codec.py decode HC3W20MA          # HC3W20MA -> 1078479825767
```

## ৪. products.html → product-detail.html রিকুয়েস্ট

`products.html`-এর প্রতিটি কার্ড এখন এই লিংক তৈরি করে:

```
product-detail.html?sku=Y3GX2OTP
```

`?branch=...`, `?ref=...`, `?product_discount=...` বা `?csv=...` দিলে সেগুলোও
ডিটেইল লিংকে পৌঁছে যায়। পুরনো `?id=<SKU>` লিংকও কাজ করে।

`product-detail.html` প্রথমে URL/from referrer থেকে SKU বের করে, SKU-টি শুধু
`A-Z a-z 0-9 _ -` পর্যন্ত সীমাবদ্ধ রেখে পাথ ট্র্যাভার্সাল আটকায়, তারপর ক্রমান্বয়ে
চেষ্টা করে:

1. `data/products/<SKU>.csv` — পেইজের পাশেই থাকলে (GitHub Pages / লোকাল)
2. `raw.githubusercontent.com/mokaimmaman/njimkoylo/<branch>/data/products/<SKU>.csv`
3. `cdn.jsdelivr.net/gh/mokaimmaman/njimkoylo@<branch>/data/products/<SKU>.csv`
4. `main` / `master` ব্যাকআপ ব্রাঞ্চ

`main`-এ মার্জ না হওয়া পর্যন্ত `arena/a8e34751-njimkoylo` দুই পেইজেই
অটোম্যাটিক fallback ব্রাঞ্চ হিসেবে যোগ করা আছে। তাই প্রিভিউ সার্ভার বন্ধ
থাকলেও বা `?branch=...` না দিলেও এই ব্রাঞ্চের CSV GitHub raw/jsDelivr থেকে
পাওয়া যায়। শেয়ার করা লিংকে ব্রাঞ্চটি স্পষ্ট রাখতে চাইলে দিতে পারেন:

```
products.html?branch=arena/a8e34751-njimkoylo
product-detail.html?sku=Y3GX2OTP&branch=arena/a8e34751-njimkoylo
```

`main`-এ মার্জ করার পর সাধারণ `product-detail.html?sku=Y3GX2OTP`-ই যথেষ্ট।

CSV পাওয়া গেলে পেইজে নাম, ক্যাটাগরি, SKU, দাম, ছবির গ্যালারি, বর্ণনা,
Features, Variants, Specifications, Model, Offers ও Image Count দেখায়; কার্ট,
Wishlist, Share, quantity ও variant sheet-ও ওই CSV-র তথ্য ব্যবহার করে।
`You may also like` একই ক্যাটাগরির অন্য SKU-তে `?sku=...` লিংক দেয়।

## ৫. আবার বানানো

```bash
python3 build_products_csv.py             # data/products.csv নতুন করে লেখে
python3 build_product_detail_csvs.py      # data/products/<SKU>.csv গুলো লেখে
python3 build_product_detail_csvs.py --check   # শুধু সারাংশ দেখায়
```

`build_product_detail_csvs.py` `products.csv`-কেই SKU-র মূল উৎস ধরে, তাই
ফাইলের নাম সবসময় ক্যাটালগের SKU-র সাথে মেলে। পুরনো/অপ্রচলিত `*.csv`
ফাইল অটোম্যাটিক মুছে যায়।

`workflows/rebuild-products-csv.yml` একটি GitHub Actions টেমপ্লেট। প্রয়োজনে
`.github/workflows/`-এ কপি করলে `data/three_groups_products.csv` বা বিল্ড
স্ক্রিপ্ট বদলালে ক্যাটালগ ও ৬৯১টি ডিটেইল ফাইল অটোম্যাটিক আবার তৈরি হয়।
এই ব্রাঞ্চে workflow file সরাসরি যোগ করার অনুমতি না থাকায় টেমপ্লেটটি
`workflows/` ফোল্ডারেই রাখা হয়েছে।
