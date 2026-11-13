# প্রোডাক্ট ছবি থেকে শুধু চাইনিজ লেখা মুছে ফেলা ও jsDelivr লিংক জেনারেটর

এই প্রজেক্টে আপনার `three_groups_products.csv` ফাইলের সব প্রোডাক্ট ছবির (`Image 1` থেকে `Image 5` — মোট ২,১২৬টি ইউনিক ছবি) জন্য সম্পূর্ণ স্বয়ংক্রিয় **Chinese-Only Text Eraser + jsDelivr Link Updater** তৈরি করা হয়েছে।

---

## প্রোগ্রামটি ঠিক কীভাবে কাজ করে? (`remove_chinese_text.py`)

1. **সব ছবি চেক করে, কিন্তু শুধু চাইনিজ থাকলেই পরিবর্তন করে:**
   - **PP-OCRv4 (`RapidOCR` character-level word box)** দিয়ে প্রতিটি ছবি স্ক্যান করে।
   - যেসব ছবিতে **কোনো চাইনিজ লেখা নেই**, সেগুলো **অপরিবর্তিত থাকে** এবং CSV ফাইলে সেগুলোর **আগের অরিজিনাল লিংকই বহাল থাকে**।
   - আইকন বা বাটনের কারণে ভুল শনাক্তকরণ (যেমন `+` বাটনকে `田` বা আইকনকে `回` মনে করা) স্বয়ংক্রিয়ভাবে ফিল্টার করে বাদ দেয়।

2. **শুধু চাইনিজ লেখা মুছে ফেলে, বাকি সব অংশ অক্ষত রাখে:**
   - একই লাইনে যদি ইংরেজি স্পেসিফিকেশন/মাপ ও চাইনিজ একসাথে থাকে (যেমন `USB输出 5V2.1A 9V2A`, `TYPE-C快充口`, `28cm`, `22L`, `60min`, `IPX7`, `1:24`, `NO.8605`, `SCY18101`, `ODM OEM`), প্রোগ্রামটি অক্ষর-ভিত্তিক বক্স ধরে **শুধু চাইনিজ অক্ষরগুলো মুছে ফেলে** এবং ইংরেজি ও সংখ্যাগুলো অক্ষত রাখে।
   - রঙিন ব্যাজ/বক্স বা সাদা ব্যাকগ্রাউন্ডের ভেতরের লেখা মুছতে **Solid-Badge Topology Fill** এবং জটিল ছবির ব্যাকগ্রাউন্ডের জন্য **LaMa Neural Inpainting / Multi-Scale Telea Inpainting** ব্যবহার করে, যাতে পণ্যের মূল ছবি নষ্ট না হয়।

3. **পরিবর্তিত ছবির জায়গায় GitHub (jsDelivr) লিংক বসায়:**
   - শুধুমাত্র যেসব ছবিতে চাইনিজ মুছে নতুন ছবি তৈরি হয়েছে, সেগুলো `cleaned-images/img_<hash>.jpg` হিসেবে সেভ হয় এবং CSV ফাইলে শুধুমাত্র সেই লিংকগুলো পরিবর্তন হয়ে নিচের ফরম্যাটে বসে:
     ```text
     https://cdn.jsdelivr.net/gh/mokaimmaman/njimkoylo@main/cleaned-images/img_<hash>.jpg
     ```
   - চাইলে `--gh-repo mokaimmaman/fiad-shop-media` দিয়ে আপনার মিডিয়া রিপোজিটরিতেও আপলোড করতে পারবেন।

---

## কীভাবে এক ক্লিকে সব ২,১২৬টি ছবি প্রসেস করবেন?

> **নোট:** Arena-র এই কোডিং স্যান্ডবক্সে শুধুমাত্র `github.com` ও `pypi.org`-এ নেটওয়ার্ক অ্যাক্সেস অনুমোদিত থাকে (`alicdn.com` / `ozone.ru` ব্লক থাকে)। তাই নিচের যেকোনো একটি উপায়ে (মোবাইল ব্রাউজার থেকেও বিনা ঝামেলায়!) পুরো ২,১২৬টি ছবি প্রসেস করে সরাসরি GitHub ও jsDelivr-এ লাইভ করতে পারবেন:

### পদ্ধতি ১ (সবচেয়ে দ্রুত — ফ্রি GPU সহ): Google Colab (মোবাইল বা পিসি থেকে ১ ক্লিকে)
1. এই ব্রাঞ্চের PR টি `main`-এ Merge করুন।
2. আপনার ব্রাউজারে নিচের লিংকটি ওপেন করুন:
   `https://colab.research.google.com/github/mokaimmaman/njimkoylo/blob/main/Chinese_Text_Remover_Colab.ipynb`
3. মেনু থেকে **Runtime -> Change runtime type -> T4 GPU** বেছে নিন এবং **Run all** চাপুন।
4. সব ছবি পরিষ্কার হয়ে স্বয়ংক্রিয়ভাবে আপনার GitHub রিপোজিটরিতে আপলোড হয়ে যাবে এবং আপডেটেড **`three_groups_products_cleaned.csv`** ফাইলটি আপনার ফোনে/কম্পিউটারে ডাউনলোড হয়ে যাবে!

### পদ্ধতি ২ (কোনো টোকেন ছাড়াই সরাসরি GitHub সার্ভারে): GitHub Actions
1. PR মার্জ করার পর GitHub ওয়েবসাইটে `workflows/clean-chinese-images.yml` ফাইলটিকে `.github/workflows/clean-chinese-images.yml` পাথে মুভ/কপি করে কমিট করুন।
2. রিপোজিটরির **Actions** ট্যাবে গিয়ে **Clean Chinese Text From Product Images** ওয়ার্কফ্লো সিলেক্ট করে **Run workflow** বাটনে ক্লিক করুন।
3. কাজ শেষ হলে `cleaned-images/` এবং `output/three_groups_products_cleaned.csv` সরাসরি আপনার রিপোজিটরিতে যুক্ত হয়ে যাবে!

### পদ্ধতি ৩: নিজের কম্পিউটার বা Termux/Linux থেকে চালানো
```bash
chmod +x setup_env.sh
./setup_env.sh

python3 remove_chinese_text.py \
  --input data/three_groups_products.csv \
  --output output/three_groups_products_cleaned.csv \
  --image-dir cleaned-images \
  --gh-repo mokaimmaman/njimkoylo \
  --gh-branch main \
  --inpainter auto \
  --workers 12 \
  --git-push
```

---

## আউটপুট ফাইলসমূহ
- `output/three_groups_products_cleaned.csv` — চূড়ান্ত CSV (যেখানে শুধু চাইনিজ মুছে ফেলা ছবির লিংকগুলো `cdn.jsdelivr.net/gh/...` হয়েছে এবং বাকি সব ছবি আগের লিংকেই আছে)।
- `output/cleaning_report.csv` — কোন ছবিতে কী চাইনিজ লেখা পাওয়া গেছে ও মুছে ফেলা হয়েছে তার বিস্তারিত রিপোর্ট।
- `output/progress_manifest.json` — ক্র্যাশ বা ইন্টারনেট বিচ্ছিন্ন হলে যেখান থেকে থেমেছিল ঠিক সেখান থেকে পুনরায় শুরু করার (`--resume`) চেকপয়েন্ট ফাইল।
