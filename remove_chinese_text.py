#!/usr/bin/env python3
"""
Chinese-Only Product Image Text Eraser & jsDelivr CSV Updater (v2)
==================================================================
- Scans product image URLs (Image 1 .. Image 5) from a CSV file.
- Detects ONLY Chinese text using padded PP-OCRv4 (RapidOCR) with character-level word boxes:
  * Images WITHOUT Chinese text are left 100% untouched (original URL preserved in CSV).
  * Non-Chinese specs/labels (e.g. USB, TYPE-C, 5V2.1A, 28cm, 22L, 60min, IPX7, 220mL, ZH-10, OEM/ODM)
    are preserved even when they appear on the same line as Chinese text.
  * Geometric single-character icon/bristle hallucinations ('*', '+', '[>]', brush bristles) are filtered out.
- Erases ONLY Chinese text using a 3-Tier Surrounding-Color & Neural Inpainting Engine:
  1) Solid Badge / Capsule / Starburst Topology Fill: fills flat graphic callouts & studio canvases with the
     exact pure surrounding badge color while preserving rounded corners, starburst teeth, and adjacent borders.
  2) Smooth Surrounding Background Surface Fill: reconstructs 2D linear, radial, and glossy/curved banner or
     studio-wall gradients strictly from clean surrounding background pixels (zero rectangular blur patches!).
  3) Stroke-Masked Cropped SimpleLama Neural Inpainting (CPU & GPU): masks ONLY character strokes on complex
     photographic scenes and inpaints local crops with LaMa (with feathered multi-scale Telea offline fallback).
- Saves modified images as compact WebP (`cleaned-images/img_<sha1_14>.webp`, <=720px) keeping total repo size
  ~31-34 MB — safely below jsDelivr's 50 MB repository limit — and pins jsDelivr URLs to the clean commit SHA
  so links serve immediately without stale CDN error caches.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import cv2
import numpy as np
import requests
from PIL import Image
from rapidocr_onnxruntime import RapidOCR
from tqdm import tqdm

ENGINE_VERSION = "v2"

# ---------------------------------------------------------------------------
# Unicode & Token Rules
# ---------------------------------------------------------------------------

# Chinese Han ideographs (CJK Unified Ideographs + Extension A + Compatibility)
HAN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")

# Fullwidth / CJK punctuation associated with Chinese copy
CJK_PUNCT_RE = re.compile(
    r"[\u3000-\u303f\uff01-\uff60\uffe0-\uffe6（）《》【】「」『』、。，；：！？—…]"
)

# Single geometric Han characters that OCR often hallucinates on '*', '+', grilles, ports, or icons
# when NO genuine multi-character Chinese text exists elsewhere on the image.
GEOMETRIC_SINGLE_HAN = set(
    "口回田日日目十丨一二三川山工丁厂冂凵匚囗乂了卜入八人几刀力又土士干千寸小大团国出合米木本水火井丰王主中品"
)

# Pure stripe/box single characters that OCR can hallucinate on bristles/grilles/ports even when banners exist
PURE_STRIPE_BOX_HAN = set("一二三丨十口回日目田川工冂凵匚囗米")

# Wholesale promo words frequently glued to Chinese B2B slogans (e.g. "定制logo") that shouldn't stay orphaned
PROMO_LATIN_WORDS = {"logo", "app", "vip", "pk", "diy"}

# International units, dimensions, electrical ratings, ratios, and model codes to KEEP on mixed lines
SPEC_UNIT_RE = re.compile(
    r"(?:\d+(?:\.\d+)?\s*(?:V|A|W|mAh|Ah|Wh|Hz|kHz|MHz|GHz|mm|cm|m|km|g|kg|mg|ml|mL|L|oz|lb|inch|in|ft|min|sec|s|h|dB|K|pcs|PCS|LED|°C|℃|%)(?![A-Za-z]))"
    r"|(?:\d+:\d+)"
    r"|(?:[A-Za-z]{2,}[-\d.]+[A-Za-z0-9.\-/+]*)"
    r"|(?:[A-Za-z]+\d+[A-Za-z0-9.\-/+]*)"
)
STANDALONE_LATIN_WORD_RE = re.compile(r"[A-Za-z]{2,}")


# ---------------------------------------------------------------------------
# OCR & Chinese-Only Region Selection
# ---------------------------------------------------------------------------


def run_ocr_padded(ocr_engine: RapidOCR, img_bgr: np.ndarray, pad: int = 20) -> List[Any]:
    """
    Pad image borders before running PP-OCRv4 so Chinese banners/watermarks touching
    x=0 or y=H-1 are reliably detected, then shift coordinates back to original space.
    """
    padded = cv2.copyMakeBorder(img_bgr, pad, pad, pad, pad, cv2.BORDER_REPLICATE)
    res, _ = ocr_engine(padded, return_word_box=True)
    if not res:
        return []

    adjusted = []
    for item in res:
        line_box = [[pt[0] - pad, pt[1] - pad] for pt in item[0]]
        txt = item[1]
        score = item[2]
        if len(item) >= 6 and item[3] is not None:
            wboxes = [[[pt[0] - pad, pt[1] - pad] for pt in wb] for wb in item[3]]
            adjusted.append([line_box, txt, score, wboxes, item[4], item[5]])
        else:
            adjusted.append([line_box, txt, score])
    return adjusted


def should_keep_non_cjk_span(span_str: str) -> bool:
    """Return True if a non-CJK substring inside a mixed line is a real English/numeric product spec."""
    s = span_str.strip(" ()[]/,+-:：!！")
    if not s:
        return False
    if SPEC_UNIT_RE.search(s):
        return True
    words = STANDALONE_LATIN_WORD_RE.findall(s)
    meaningful = [w for w in words if w.lower() not in PROMO_LATIN_WORDS]
    return len(meaningful) > 0


def shrink_poly_horizontal(poly: np.ndarray, px: float = 2.0) -> np.ndarray:
    """Slightly inset a character box horizontally by `px` so adjacent Chinese stroke tips aren't shielded."""
    poly = np.array(poly, dtype=np.float32)
    cx = float(poly[:, 0].mean())
    half_w = max(1.0, float(poly[:, 0].max() - poly[:, 0].min()) * 0.5)
    eff_px = min(px, half_w * 0.28)
    out = poly.copy()
    for i in range(len(out)):
        if out[i, 0] < cx - 1.0:
            out[i, 0] = min(cx - 0.5, out[i, 0] + eff_px)
        elif out[i, 0] > cx + 1.0:
            out[i, 0] = max(cx + 0.5, out[i, 0] - eff_px)
    return out


def select_chinese_regions(res: List[Any]) -> List[Dict[str, Any]]:
    """
    Analyze OCR output and return ONLY regions corresponding to genuine Chinese text,
    along with `keep_polys` protecting nearby non-Chinese text/specs.
    """
    if not res:
        return []

    strong_chinese_lines = 0
    for item in res:
        txt, score = item[1], float(item[2])
        han_chars = HAN_RE.findall(txt)
        if len(han_chars) >= 2 and score >= 0.68:
            if len(set(han_chars)) == 1 and len(txt.strip()) == len(han_chars) and score < 0.80:
                continue
            strong_chinese_lines += 1
        elif len(han_chars) == 1 and han_chars[0] not in GEOMETRIC_SINGLE_HAN and score >= 0.88:
            strong_chinese_lines += 1

    global_keep_polys: List[np.ndarray] = []
    for item in res:
        txt = item[1]
        if not HAN_RE.search(txt) and re.search(r"[A-Za-z0-9]", txt) and float(item[2]) >= 0.60:
            global_keep_polys.append(shrink_poly_horizontal(np.array(item[0], dtype=np.float32), 1.0))

    regions: List[Dict[str, Any]] = []
    for item in res:
        line_box = np.array(item[0], dtype=np.float32)
        txt = item[1]
        score = float(item[2])
        word_boxes = item[3] if len(item) > 3 else None
        word_chars = item[4] if len(item) > 4 else None

        han_chars = HAN_RE.findall(txt)
        if not han_chars:
            continue

        # Filter isolated single-character or twin-bristle icon misrecognitions
        if len(han_chars) == 1:
            ch = han_chars[0]
            if score < 0.76:
                continue
            if ch in PURE_STRIPE_BOX_HAN and len(txt.strip()) == 1 and score < 0.94:
                continue
            if ch in GEOMETRIC_SINGLE_HAN and strong_chinese_lines == 0:
                continue
            if strong_chinese_lines == 0 and score < 0.90:
                continue
        else:
            if len(set(han_chars)) == 1 and len(txt.strip()) == len(han_chars) and score < 0.78:
                continue
            if score < 0.64:
                continue

        if word_boxes and word_chars and len(word_boxes) == len(word_chars):
            n = len(word_chars)
            is_cjk = [bool(HAN_RE.search(c) or CJK_PUNCT_RE.search(c)) for c in word_chars]

            erase_flags = list(is_cjk)
            i = 0
            while i < n:
                if is_cjk[i]:
                    i += 1
                    continue
                j = i
                while j < n and not is_cjk[j]:
                    j += 1
                span_str = "".join(word_chars[i:j])
                if not should_keep_non_cjk_span(span_str):
                    for k in range(i, j):
                        erase_flags[k] = True
                i = j

            if all(erase_flags):
                regions.append(
                    {
                        "poly": line_box,
                        "keep_polys": list(global_keep_polys),
                        "text": txt,
                        "is_subline": False,
                    }
                )
                continue

            line_keep_polys = list(global_keep_polys)
            for idx in range(n):
                if not erase_flags[idx] and word_chars[idx].strip():
                    line_keep_polys.append(
                        shrink_poly_horizontal(np.array(word_boxes[idx], dtype=np.float32), 2.0)
                    )

            idx = 0
            while idx < n:
                if not erase_flags[idx]:
                    idx += 1
                    continue
                end = idx
                while end + 1 < n and erase_flags[end + 1]:
                    end += 1
                pts = np.vstack([np.array(word_boxes[k], dtype=np.float32) for k in range(idx, end + 1)])
                rect = cv2.minAreaRect(pts)
                box_pts = cv2.boxPoints(rect)
                regions.append(
                    {
                        "poly": box_pts,
                        "keep_polys": line_keep_polys,
                        "text": "".join(word_chars[idx : end + 1]),
                        "is_subline": True,
                    }
                )
                idx = end + 1
        else:
            non_han = HAN_RE.sub(" ", txt)
            if not should_keep_non_cjk_span(non_han):
                regions.append(
                    {
                        "poly": line_box,
                        "keep_polys": list(global_keep_polys),
                        "text": txt,
                        "is_subline": False,
                    }
                )
            else:
                regions.append(
                    {
                        "poly": line_box,
                        "keep_polys": list(global_keep_polys),
                        "text": txt,
                        "is_subline": True,
                    }
                )

    # Also sweep orphaned pure CJK punctuation boxes immediately adjacent to erased Chinese regions
    if regions:
        for item in res:
            txt = item[1]
            if not HAN_RE.search(txt) and CJK_PUNCT_RE.search(txt) and not should_keep_non_cjk_span(txt):
                pbox = np.array(item[0], dtype=np.float32)
                px_c, py_c = float(pbox[:, 0].mean()), float(pbox[:, 1].mean())
                for reg in list(regions):
                    rbox = reg["poly"]
                    ry0, ry1 = float(rbox[:, 1].min()), float(rbox[:, 1].max())
                    rx0, rx1 = float(rbox[:, 0].min()), float(rbox[:, 0].max())
                    rh_box = max(8.0, ry1 - ry0)
                    if (ry0 - rh_box * 0.5) <= py_c <= (ry1 + rh_box * 0.5) and (rx0 - rh_box * 2.0) <= px_c <= (rx1 + rh_box * 2.0):
                        regions.append(
                            {
                                "poly": pbox,
                                "keep_polys": list(global_keep_polys),
                                "text": txt,
                                "is_subline": False,
                            }
                        )
                        break

    return regions


# ---------------------------------------------------------------------------
# 3-Tier Surrounding-Color & Neural Inpainting Engine
# ---------------------------------------------------------------------------


def anisotropic_surrounding_fill(
    roi_f32: np.ndarray,
    w_bg_outer: np.ndarray,
    fallback_val: np.ndarray,
    char_h: float,
) -> np.ndarray:
    """
    Reconstruct smooth surrounding background color strictly from clean outer pixels (`w_bg_outer`).
    Uses horizontal-dominant multi-scale normalized Gaussian kernels so 2D linear/radial gradients
    (peach pills, studio walls, blue OEM/ODM bars) AND curved/glossy horizontal footers reconstruct
    seamlessly with the exact surrounding color and zero rectangular blur patches.
    """
    w = w_bg_outer.astype(np.float32)
    prem = roi_f32 * w[..., None]
    scales = [
        (max(4.0, char_h * 0.45), max(2.0, char_h * 0.14)),
        (max(10.0, char_h * 1.10), max(3.5, char_h * 0.30)),
        (max(22.0, char_h * 2.20), max(8.0, char_h * 0.75)),
    ]
    result = np.broadcast_to(np.asarray(fallback_val, dtype=np.float32), roi_f32.shape).copy()
    for sx, sy in reversed(scales):
        kx = min(199, int(round(sx * 4.5)) | 1)
        ky = min(151, int(round(sy * 4.5)) | 1)
        num = cv2.GaussianBlur(prem, (kx, ky), sigmaX=sx, sigmaY=sy, borderType=cv2.BORDER_REPLICATE)
        den = cv2.GaussianBlur(w, (kx, ky), sigmaX=sx, sigmaY=sy, borderType=cv2.BORDER_REPLICATE)
        valid = den > 1e-3
        interp = np.zeros_like(roi_f32)
        interp[valid] = num[valid] / den[valid, None]
        conf = np.clip(den / 0.05, 0.0, 1.0)[..., None]
        result = result * (1.0 - conf) + interp * conf
    return np.clip(result, 0.0, 255.0)


def clean_chinese_from_image(
    img_bgr: np.ndarray,
    regions: List[Dict[str, Any]],
    lama_model: Optional[Any] = None,
) -> Tuple[np.ndarray, bool]:
    """Erase all selected Chinese regions using surrounding background colors or stroke-masked LaMa."""
    if not regions:
        return img_bgr, False

    h, w = img_bgr.shape[:2]
    out = img_bgr.copy()

    protect_mask = np.zeros((h, w), dtype=np.uint8)
    for reg in regions:
        for kp in reg["keep_polys"]:
            kp_clip = np.clip(np.round(kp), [0, 0], [w - 1, h - 1]).astype(np.int32)
            cv2.fillPoly(protect_mask, [kp_clip], 255)

    all_chinese_tight = np.zeros((h, w), dtype=np.uint8)
    for reg in regions:
        tp = np.clip(np.round(np.array(reg["poly"], dtype=np.float32)), [0, 0], [w - 1, h - 1]).astype(np.int32)
        cv2.fillPoly(all_chinese_tight, [tp], 255)

    rng = np.random.default_rng(42)
    photo_stroke_mask = np.zeros((h, w), dtype=np.uint8)

    for reg in regions:
        poly = np.array(reg["poly"], dtype=np.float32)
        rect = cv2.minAreaRect(poly)
        bw, bh = rect[1]
        char_h = max(min(bw, bh), 8.0)

        center = poly.mean(axis=0, keepdims=True)
        pad_y = max(3.5, char_h * 0.20)
        pad_x = max(3.5, char_h * 0.16) if reg["is_subline"] else max(4.5, char_h * 0.38)
        exp_pts = poly.copy()
        for idx_p in range(len(exp_pts)):
            exp_pts[idx_p, 0] += -pad_x if exp_pts[idx_p, 0] < center[0, 0] else pad_x
            exp_pts[idx_p, 1] += -pad_y if exp_pts[idx_p, 1] < center[0, 1] else pad_y

        exp_poly = np.clip(np.round(exp_pts), [0, 0], [w - 1, h - 1]).astype(np.int32)
        tight_poly = np.clip(np.round(poly), [0, 0], [w - 1, h - 1]).astype(np.int32)

        ring_in = max(2, int(round(char_h * 0.14)))
        ring_out = max(8, int(round(char_h * 0.52)))
        margin = ring_out + 12

        x0 = max(0, int(tight_poly[:, 0].min()) - margin)
        y0 = max(0, int(tight_poly[:, 1].min()) - margin)
        x1 = min(w, int(tight_poly[:, 0].max()) + margin + 1)
        y1 = min(h, int(tight_poly[:, 1].max()) + margin + 1)
        if x1 - x0 < 5 or y1 - y0 < 5:
            continue

        roi = out[y0:y1, x0:x1]
        roi_f = roi.astype(np.float32)
        rh, rw = roi.shape[:2]

        pm_tight = np.zeros((rh, rw), dtype=np.uint8)
        cv2.fillPoly(pm_tight, [tight_poly - np.array([[x0, y0]])], 255)
        pm_exp = np.zeros((rh, rw), dtype=np.uint8)
        cv2.fillPoly(pm_exp, [exp_poly - np.array([[x0, y0]])], 255)

        prot_roi = protect_mask[y0:y1, x0:x1]
        all_txt_roi = all_chinese_tight[y0:y1, x0:x1]
        pm_tight[prot_roi > 0] = 0
        pm_exp[prot_roi > 0] = 0
        if np.count_nonzero(pm_tight) < 6:
            continue

        # ---- TIER 1: Crisp Solid Badge / Capsule / Starburst / Flat Studio Canvas ----
        k_pill = (2 * max(3, min(5, int(round(char_h * 0.14)))) + 1) | 1
        pill_moat = cv2.dilate(pm_tight, np.ones((k_pill, k_pill), np.uint8)) - pm_tight
        pill_moat[all_txt_roi > 0] = 0
        pill_moat[prot_roi > 0] = 0

        far_mask = np.zeros((rh, rw), dtype=bool)
        far_mask[:2, :] = True
        far_mask[-2:, :] = True
        far_mask[:, :2] = True
        far_mask[:, -2:] = True
        far_mask[pm_exp > 0] = False
        far_pts = roi_f[far_mask] if np.count_nonzero(far_mask) >= 8 else roi_f.reshape(-1, 3)

        in_pts = roi_f[pm_tight > 0]

        if np.count_nonzero(pill_moat) >= 8:
            ys_pm, xs_pm = np.where(pill_moat > 0)
            pm_pts = roi_f[ys_pm, xs_pm]
            all_pm = np.vstack([in_pts, pm_pts])
            K_s = 3 if len(all_pm) >= 36 else 2
            criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 20, 1.0)
            _, _, centers_s = cv2.kmeans(all_pm, K_s, None, criteria, 3, cv2.KMEANS_PP_CENTERS)
            in_lbl_s = np.argmin(np.linalg.norm(in_pts[:, None, :] - centers_s[None, :, :], axis=2), axis=1)
            pm_lbl_s = np.argmin(np.linalg.norm(pm_pts[:, None, :] - centers_s[None, :, :], axis=2), axis=1)

            best_solid_med: Optional[np.ndarray] = None
            best_solid_sc = -1e9
            best_solid_k: Optional[int] = None

            for k in range(K_s):
                sel_k = pm_lbl_s == k
                m_pts_k = pm_pts[sel_k]
                if len(m_pts_k) < 6:
                    continue
                moat_f = len(m_pts_k) / len(pm_pts)
                in_f = float(np.mean(in_lbl_s == k))
                if moat_f < 0.14 or in_f < 0.15:
                    continue

                # Overhanging character strokes (e.g. orange glyphs on a peach banner) are beaten by the banner in BOTH interior and moat
                beaten_both = False
                for j_chk in range(K_s):
                    if j_chk != k:
                        if float(np.mean(in_lbl_s == j_chk)) > in_f and float(np.mean(pm_lbl_s == j_chk)) > moat_f:
                            beaten_both = True
                            break
                if beaten_both:
                    continue

                # True pill/canvas background surrounds text on opposite sides or >=3 sides (rejects adjacent 1-sided banners/text)
                ys_k_pm, xs_k_pm = ys_pm[sel_k], xs_pm[sel_k]
                tx_min, ty_min = tight_poly[:, 0].min() - x0, tight_poly[:, 1].min() - y0
                tx_max, ty_max = tight_poly[:, 0].max() - x0, tight_poly[:, 1].max() - y0
                top_h = np.count_nonzero(ys_k_pm <= ty_min + 1) >= 2
                bot_h = np.count_nonzero(ys_k_pm >= ty_max - 1) >= 2
                left_h = np.count_nonzero(xs_k_pm <= tx_min + 1) >= 2
                right_h = np.count_nonzero(xs_k_pm >= tx_max - 1) >= 2
                if not ((left_h and right_h) or (top_h and bot_h) or (int(top_h) + int(bot_h) + int(left_h) + int(right_h) >= 3)):
                    continue

                med_raw = np.median(m_pts_k, axis=0)
                xs_k = xs_pm[sel_k]
                if len(xs_k) >= 10 and (xs_k.max() - xs_k.min()) > 26:
                    mid_x = 0.5 * (xs_k.min() + xs_k.max())
                    lk, rk = m_pts_k[xs_k <= mid_x], m_pts_k[xs_k > mid_x]
                    if len(lk) >= 3 and len(rk) >= 3:
                        if float(np.linalg.norm(np.median(lk, axis=0) - np.median(rk, axis=0))) > 22.0:
                            continue

                non_k_in = in_pts[in_lbl_s != k]
                if len(non_k_in) < 0.08 * len(in_pts):
                    continue
                fg_med = np.median(non_k_in, axis=0)
                if float(np.linalg.norm(fg_med - med_raw)) < 26.0:
                    continue

                if K_s == 3:
                    others = [centers_s[j] for j in range(3) if j != k]
                    seg = others[1] - others[0]
                    seg_len = float(np.linalg.norm(seg))
                    if seg_len > 40.0:
                        t_p = float(np.dot(centers_s[k] - others[0], seg) / (seg_len * seg_len))
                        if 0.18 < t_p < 0.82 and float(np.linalg.norm((centers_s[k] - others[0]) - t_p * seg)) < 25.0:
                            continue

                d_from_fg = np.linalg.norm(m_pts_k - fg_med[None, :], axis=1)
                pure_pts = m_pts_k[d_from_fg >= np.median(d_from_fg)]
                pure_med = np.median(pure_pts, axis=0)
                moat_mad = float(np.median(np.linalg.norm(pure_pts - pure_med, axis=1)))
                if moat_mad > 20.0:
                    continue

                far_f = float(np.mean(np.linalg.norm(far_pts - pure_med[None, :], axis=1) < 26.0))
                far_mad = float(np.median(np.linalg.norm(far_pts - np.median(far_pts, axis=0), axis=1)))
                is_enclosed = far_f < 0.32
                if not is_enclosed and (moat_mad > 4.5 or far_mad > 5.5):
                    continue
                if is_enclosed and in_f < 0.32:
                    continue

                sc = (2.5 if is_enclosed else 0.0) + 1.2 * moat_f + 1.0 * in_f - moat_mad * 0.02
                if sc > best_solid_sc:
                    best_solid_sc = sc
                    best_solid_med = pure_med
                    best_solid_k = k

            if best_solid_med is not None and best_solid_k is not None:
                dist_to_bg = np.linalg.norm(roi_f - best_solid_med[None, None, :], axis=2)
                fg_med_s = np.median(in_pts[in_lbl_s != best_solid_k], axis=0)
                dist_to_fg_s = np.linalg.norm(roi_f - fg_med_s[None, None, :], axis=2)

                # Preserve rounded capsule corners & zig-zag starburst edges outside `pm_tight`
                cand_outer = ((pm_tight == 0) & (dist_to_bg > 45.0) & (dist_to_fg_s > 45.0)).astype(np.uint8)
                for k in range(K_s):
                    if k != best_solid_k and np.linalg.norm(centers_s[k] - best_solid_med) > 28.0:
                        far_match = np.linalg.norm(far_pts - centers_s[k][None, :], axis=1) < 28.0
                        if np.mean(far_match) >= 0.18:
                            outer_med_k = np.median(far_pts[far_match], axis=0)
                            dist_outer_k = np.linalg.norm(roi_f - outer_med_k[None, None, :], axis=2)
                            cand_outer |= ((dist_outer_k < 28.0) & (dist_outer_k < dist_to_bg)).astype(np.uint8)

                _, cc_lbl = cv2.connectedComponents(cand_outer, connectivity=4)
                border_ccs = set(np.unique(cc_lbl[far_mask])) - {0}
                outer_canvas_mask = np.isin(cc_lbl, list(border_ccs))

                fill_mask = ((pm_exp > 0) & (~outer_canvas_mask)).astype(np.uint8) * 255
                fill_mask[prot_roi > 0] = 0
                fk = max(3, (int(round(char_h * 0.06)) | 1))
                alpha = cv2.GaussianBlur(fill_mask.astype(np.float32) / 255.0, (fk, fk), 0)
                alpha = np.clip(alpha * 1.6, 0.0, 1.0)[..., None]
                alpha[outer_canvas_mask | (prot_roi > 0)] = 0.0
                filled = roi_f * (1.0 - alpha) + best_solid_med[None, None, :] * alpha
                out[y0:y1, x0:x1] = np.clip(np.round(filled), 0, 255).astype(np.uint8)
                continue

        # ---- TIER 2 & TIER 3: Smooth Surrounding Gradient Surface vs Textured Photo ----
        k_in = (2 * ring_in + 1) | 1
        k_out = (2 * ring_out + 1) | 1
        txt_excl = cv2.dilate(
            cv2.bitwise_or(all_txt_roi, prot_roi),
            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k_in, k_in)),
        )
        wide_moat = cv2.dilate(pm_tight, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k_out, k_out)))
        wide_moat[txt_excl > 0] = 0
        moat = wide_moat if np.count_nonzero(wide_moat) >= 16 else pill_moat
        if np.count_nonzero(moat) < 8:
            photo_stroke_mask[y0:y1, x0:x1] = np.maximum(photo_stroke_mask[y0:y1, x0:x1], pm_tight)
            continue

        ys_m, xs_m = np.where(moat > 0)
        moat_pts = roi_f[ys_m, xs_m]

        ty0, ty1 = int(tight_poly[:, 1].min() - y0), int(tight_poly[:, 1].max() - y0)
        band_y0 = ty0 + int(0.10 * max(1, ty1 - ty0))
        band_y1 = ty1 - int(0.10 * max(1, ty1 - ty0))
        in_hband = (ys_m >= band_y0) & (ys_m <= max(band_y0 + 1, band_y1))

        if len(moat_pts) < 20:
            dom_med = np.median(moat_pts, axis=0)
        else:
            K_g3 = 3 if len(moat_pts) >= 36 else 2
            criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 20, 1.0)
            _, m_lbl_g, m_centers_g = cv2.kmeans(moat_pts, K_g3, None, criteria, 3, cv2.KMEANS_PP_CENTERS)
            m_lbl_g = m_lbl_g.ravel()
            best_c = 0
            best_csc = -1e9
            for c_i in range(K_g3):
                f_m = float(np.mean(m_lbl_g == c_i))
                f_hb = float(np.mean(m_lbl_g[in_hband] == c_i)) if np.count_nonzero(in_hband) >= 6 else f_m
                f_in = float(np.mean(np.linalg.norm(in_pts - m_centers_g[c_i][None, :], axis=1) < 45.0))
                sc_c = (2.0 * f_in + 1.0 * f_hb + 0.5 * f_m) if f_in >= 0.12 else (0.2 * f_hb)
                if sc_c > best_csc:
                    best_csc = sc_c
                    best_c = c_i
            dom_med = m_centers_g[best_c]

        outer_clean = (txt_excl == 0) & (np.linalg.norm(roi_f - dom_med[None, None, :], axis=2) < 68.0)
        bg_surf = anisotropic_surrounding_fill(roi_f, outer_clean.astype(np.float32), dom_med, char_h)

        diff_to_surf = np.linalg.norm(roi_f - bg_surf, axis=2)
        banner_moat = (moat > 0) & outer_clean
        if np.count_nonzero(banner_moat) >= 10:
            bm_diffs = diff_to_surf[banner_moat]
            surf_res = float(np.median(bm_diffs))
            surf_p90 = float(np.percentile(bm_diffs, 90))
            frac_hb_clean = (
                float(np.mean(outer_clean[ys_m[in_hband], xs_m[in_hband]]))
                if np.count_nonzero(in_hband) >= 6
                else float(np.mean(outer_clean[ys_m, xs_m]))
            )
            in_bg_match = float(np.mean(diff_to_surf[pm_tight > 0] < max(28.0, surf_p90 * 2.0)))
        else:
            surf_res, surf_p90, frac_hb_clean, in_bg_match = 99.0, 99.0, 0.0, 0.0

        other_banner = (diff_to_surf > max(32.0, surf_p90 * 2.4)) & (pm_tight == 0)
        if np.any(other_banner & (pm_exp == 0)):
            _, cc_lbl = cv2.connectedComponents(other_banner.astype(np.uint8), connectivity=4)
            ext_ccs = set(np.unique(cc_lbl[other_banner & (pm_exp == 0)])) - {0}
            other_banner_ext = np.isin(cc_lbl, list(ext_ccs))
        else:
            other_banner_ext = np.zeros((rh, rw), dtype=bool)

        if surf_res <= 7.2 and surf_p90 <= 18.5 and frac_hb_clean >= 0.45 and in_bg_match >= 0.20:
            # TIER 2: Smooth Surrounding Background Color Surface (gradients, glossy banners, studio walls)
            w_clean2 = (
                (txt_excl == 0)
                & (~other_banner_ext)
                & (diff_to_surf < max(26.0, surf_p90 * 1.9))
            ).astype(np.float32)
            if np.count_nonzero(w_clean2) >= 10:
                bg_surf = anisotropic_surrounding_fill(roi_f, w_clean2, dom_med, char_h)
            grain_std = float(np.clip(surf_res * 0.28, 0.25, 1.8))
            bg_surf = np.clip(bg_surf + rng.normal(0.0, grain_std, size=(rh, rw, 1)).astype(np.float32), 0.0, 255.0)

            fill_zone = ((pm_exp > 0) & (~other_banner_ext) & (prot_roi == 0)).astype(np.uint8) * 255
            fk = max(5, (int(round(char_h * 0.10)) | 1))
            alpha = cv2.GaussianBlur(fill_zone.astype(np.float32) / 255.0, (fk, fk), 0)
            alpha = np.clip(alpha * 1.35, 0.0, 1.0)[..., None]
            alpha[other_banner_ext | (prot_roi > 0)] = 0.0

            filled_roi = roi_f * (1.0 - alpha) + bg_surf * alpha
            out[y0:y1, x0:x1] = np.clip(np.round(filled_roi), 0, 255).astype(np.uint8)
        else:
            # TIER 3: Textured Multi-Color Photographic Scene -> Character-stroke-only mask for LaMa
            k_st = (2 * max(2, int(round(char_h * 0.08))) + 1) | 1
            pm_stroke_zone = cv2.dilate(pm_tight, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k_st, k_st)))
            stroke_thresh = max(15.0, surf_p90 * 1.45)
            raw_stroke = (diff_to_surf > stroke_thresh) & (pm_stroke_zone > 0) & (~other_banner_ext) & (prot_roi == 0)
            halo_rad = max(3, int(round(char_h * 0.11))) | 1
            stroke_dil = cv2.dilate(
                raw_stroke.astype(np.uint8) * 255,
                cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (halo_rad, halo_rad)),
            )
            stroke_dil = cv2.morphologyEx(
                stroke_dil,
                cv2.MORPH_CLOSE,
                cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (halo_rad, halo_rad)),
            )
            stroke_dil[(pm_stroke_zone == 0) | other_banner_ext | (prot_roi > 0)] = 0
            if np.count_nonzero(stroke_dil) < 0.08 * np.count_nonzero(pm_tight):
                stroke_dil = ((pm_stroke_zone > 0) & (~other_banner_ext) & (prot_roi == 0)).astype(np.uint8) * 255
            photo_stroke_mask[y0:y1, x0:x1] = np.maximum(photo_stroke_mask[y0:y1, x0:x1], stroke_dil)

    photo_stroke_mask[protect_mask > 0] = 0

    if np.count_nonzero(photo_stroke_mask) > 0:
        if lama_model is not None:
            num_cc, _, stats, _ = cv2.connectedComponentsWithStats(
                cv2.dilate(photo_stroke_mask, np.ones((35, 35), np.uint8)),
                connectivity=8,
            )
            for cc_id in range(1, num_cc):
                cx0, cy0, cw, ch_b, _ = stats[cc_id]
                pad_c = 64
                px0, py0 = max(0, cx0 - pad_c), max(0, cy0 - pad_c)
                px1, py1 = min(w, cx0 + cw + pad_c), min(h, cy0 + ch_b + pad_c)
                sub_m = photo_stroke_mask[py0:py1, px0:px1]
                if np.count_nonzero(sub_m) == 0:
                    continue
                sub_img = out[py0:py1, px0:px1]
                sh, sw = sub_img.shape[:2]
                pil_in = Image.fromarray(cv2.cvtColor(sub_img, cv2.COLOR_BGR2RGB))
                pil_m = Image.fromarray(sub_m)
                pil_out = lama_model(pil_in, pil_m)
                # `simple-lama-inpainting` pads right/bottom to modulo-8; slice `[:sh, :sw]` for pixel-exact alignment
                lama_rgb = np.array(pil_out)[:sh, :sw]
                lama_bgr = cv2.cvtColor(lama_rgb, cv2.COLOR_RGB2BGR)
                dil_sub = cv2.dilate(sub_m, np.ones((3, 3), np.uint8))
                alpha = cv2.GaussianBlur(dil_sub.astype(np.float32) / 255.0, (5, 5), 0)
                alpha = np.clip(alpha * 1.4, 0.0, 1.0)[..., None]
                alpha[protect_mask[py0:py1, px0:px1] > 0] = 0.0
                blended = sub_img.astype(np.float32) * (1.0 - alpha) + lama_bgr.astype(np.float32) * alpha
                out[py0:py1, px0:px1] = np.clip(np.round(blended), 0, 255).astype(np.uint8)
        else:
            dist = cv2.distanceTransform(photo_stroke_mask, cv2.DIST_L2, 5)
            max_r = float(dist.max()) if dist.size else 3.0
            if max_r > 6.0:
                scale = max(0.35, min(0.6, 4.5 / max_r))
                sw, sh = max(16, int(round(w * scale))), max(16, int(round(h * scale)))
                small_img = cv2.resize(out, (sw, sh), interpolation=cv2.INTER_AREA)
                small_m = cv2.dilate(
                    cv2.resize(photo_stroke_mask, (sw, sh), interpolation=cv2.INTER_NEAREST),
                    np.ones((3, 3), np.uint8),
                )
                coarse = cv2.resize(
                    cv2.inpaint(small_img, small_m, inpaintRadius=3, flags=cv2.INPAINT_TELEA),
                    (w, h),
                    interpolation=cv2.INTER_CUBIC,
                )
                fine = cv2.inpaint(out, photo_stroke_mask, inpaintRadius=4, flags=cv2.INPAINT_TELEA)
                w_coarse = np.clip((dist - 2.0) / max(3.0, max_r * 0.45), 0.0, 1.0)[..., None]
                blended = fine.astype(np.float32) * (1.0 - w_coarse) + coarse.astype(np.float32) * w_coarse
                m_bool = photo_stroke_mask > 0
                out[m_bool] = np.clip(np.round(blended[m_bool]), 0, 255).astype(np.uint8)
            else:
                out = cv2.inpaint(out, photo_stroke_mask, inpaintRadius=4, flags=cv2.INPAINT_TELEA)

    return out, True


# ---------------------------------------------------------------------------
# Image Downloading, Decoding & Compact WebP Saving (< 50 MB Repo Cap)
# ---------------------------------------------------------------------------


def decode_image_bytes(raw: bytes) -> Optional[np.ndarray]:
    """Decode JPEG / PNG / WebP / BMP bytes safely into BGR uint8, compositing RGBA onto white."""
    try:
        with Image.open(io.BytesIO(raw)) as im:
            if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
                rgba = im.convert("RGBA")
                white = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
                rgb = Image.alpha_composite(white, rgba).convert("RGB")
            else:
                rgb = im.convert("RGB")
            arr = np.array(rgb)
            return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
    except Exception:
        arr = np.frombuffer(raw, dtype=np.uint8)
        return cv2.imdecode(arr, cv2.IMREAD_COLOR)


def download_image(url: str, timeout: int = 25, retries: int = 3) -> Tuple[Optional[np.ndarray], Optional[str]]:
    """
    Download an image URL with appropriate Referer/User-Agent headers (crucial for 1688/alicdn)
    and automatic fallback if `.jpg_.webp` suffix fails.
    """
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    }
    if "alicdn.com" in host or "1688.com" in host or "aliexpress" in host:
        headers["Referer"] = "https://detail.1688.com/"

    candidate_urls = [url]
    if url.endswith("_.webp"):
        candidate_urls.append(url[:-6])

    last_err = None
    for cand in candidate_urls:
        for attempt in range(retries):
            try:
                resp = requests.get(cand, headers=headers, timeout=timeout)
                if resp.status_code == 200 and resp.content:
                    img = decode_image_bytes(resp.content)
                    if img is not None and img.size > 0:
                        return img, None
                    last_err = "decode_failed"
                else:
                    last_err = f"HTTP {resp.status_code}"
            except Exception as exc:
                last_err = str(exc)
            if attempt + 1 < retries:
                time.sleep(0.8 * (attempt + 1))

    return None, last_err


def save_cleaned_webp(
    img_bgr: np.ndarray,
    dst_path: Path,
    max_dim: int = 720,
    quality: int = 76,
    max_bytes: int = 46000,
) -> None:
    """
    Save cleaned BGR image as high-efficiency WebP so ~915 cleaned product images total ~30-33 MB,
    keeping the entire GitHub repository safely below jsDelivr's 50 MB package limit.
    """
    h, w = img_bgr.shape[:2]
    cur_bgr = img_bgr
    if max_dim > 0 and max(h, w) > max_dim:
        scale = max_dim / float(max(h, w))
        nw, nh = int(round(w * scale)), int(round(h * scale))
        cur_bgr = cv2.resize(img_bgr, (nw, nh), interpolation=cv2.INTER_AREA)

    dst_path.parent.mkdir(parents=True, exist_ok=True)
    pil_rgb = Image.fromarray(cv2.cvtColor(cur_bgr, cv2.COLOR_BGR2RGB))

    buf = io.BytesIO()
    pil_rgb.save(buf, format="WEBP", quality=quality, method=6)
    data = buf.getvalue()

    # Enforce per-image soft byte cap so 915+ images never approach jsDelivr's 50 MB repo cap
    if max_bytes > 0 and len(data) > max_bytes:
        for q_try in (68, 60):
            buf = io.BytesIO()
            pil_rgb.save(buf, format="WEBP", quality=q_try, method=6)
            data = buf.getvalue()
            if len(data) <= max_bytes:
                break
        if len(data) > max_bytes and max(pil_rgb.size) > 600:
            s2 = 600.0 / float(max(pil_rgb.size))
            pil_small = pil_rgb.resize(
                (int(round(pil_rgb.size[0] * s2)), int(round(pil_rgb.size[1] * s2))),
                Image.Resampling.LANCZOS,
            )
            buf = io.BytesIO()
            pil_small.save(buf, format="WEBP", quality=68, method=6)
            data = buf.getvalue()

    dst_path.write_bytes(data)


def url_to_filename(url: str) -> str:
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:14]
    return f"img_{digest}.webp"


# ---------------------------------------------------------------------------
# Neural LaMa Loader (GPU + CPU) & Git Batch Pusher
# ---------------------------------------------------------------------------


def load_inpainter(mode: str) -> Optional[Any]:
    """Load `SimpleLama` neural inpainter on CUDA or CPU (used only for cropped Tier-3 photo regions)."""
    if mode == "opencv":
        return None
    try:
        import torch
        from simple_lama_inpainting import SimpleLama

        device = "cuda" if torch.cuda.is_available() else "cpu"
        print(f"[Info] Loading SimpleLama neural inpainter on {device} (for cropped Tier-3 photo regions)...")
        return SimpleLama(device=torch.device(device))
    except Exception as exc:
        print(f"[Info] SimpleLama unavailable ({exc}); using multi-scale stroke inpainter fallback.")
        return None


def purge_legacy_jpegs(repo_root: Path, image_dir: Path, use_git: bool) -> None:
    """Remove any legacy v1 `.jpg` files in `cleaned-images/` so repo size stays < 50 MB."""
    legacy_jpgs = list(image_dir.glob("*.jpg"))
    if not legacy_jpgs:
        return
    print(f"[Info] Removing {len(legacy_jpgs)} legacy .jpg files from {image_dir.name}/ to stay under jsDelivr 50 MB limit...")
    for p in legacy_jpgs:
        p.unlink(missing_ok=True)
    if use_git and image_dir.is_relative_to(repo_root):
        rel = image_dir.relative_to(repo_root).as_posix()
        subprocess.run(["git", "add", "-u", "--", rel], cwd=repo_root, check=False)


def git_commit_and_push_batch(repo_root: Path, branch: str, paths_to_add: List[str], message: str) -> None:
    if paths_to_add:
        subprocess.run(["git", "add", "--"] + paths_to_add, cwd=repo_root, check=True)
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    if not status.stdout.strip():
        return
    subprocess.run(["git", "commit", "-m", message], cwd=repo_root, check=True)
    subprocess.run(["git", "push", "origin", branch], cwd=repo_root, check=True)


def get_head_short_sha(repo_root: Path) -> Optional[str]:
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--short=10", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
        sha = res.stdout.strip()
        return sha if sha else None
    except Exception:
        return None


def best_effort_purge_jsdelivr(gh_repo: str, gh_branch: str, rel_img_dir: str, sample_files: List[str]) -> None:
    """Trigger jsDelivr cache purge on `@<branch>` for sample files."""
    for fname in sample_files[:5]:
        purge_url = f"https://purge.jsdelivr.net/gh/{gh_repo}@{gh_branch}/{rel_img_dir}/{fname}"
        try:
            requests.get(purge_url, timeout=8)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Main Pipeline
# ---------------------------------------------------------------------------


def process_csv(args: argparse.Namespace) -> None:
    input_csv = Path(args.input).resolve()
    output_csv = Path(args.output).resolve()
    image_dir = Path(args.image_dir).resolve()
    repo_root = Path(args.repo_root).resolve()
    manifest_path = output_csv.parent / "progress_manifest.json"
    report_path = output_csv.parent / "cleaning_report.csv"

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    image_dir.mkdir(parents=True, exist_ok=True)

    # Ensure legacy v1 112 MB JPEGs are removed before adding compact v2 WebPs
    purge_legacy_jpegs(repo_root, image_dir, use_git=args.git_push)

    with open(input_csv, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)

    image_cols = [c.strip() for c in args.image_cols.split(",") if c.strip() in fieldnames]
    if not image_cols:
        raise ValueError(f"None of the specified image columns ({args.image_cols}) found in CSV header: {fieldnames}")

    unique_urls: List[str] = []
    seen = set()
    for row in rows:
        for col in image_cols:
            u = (row.get(col) or "").strip()
            if u and u not in seen:
                seen.add(u)
                unique_urls.append(u)

    if args.limit and args.limit > 0:
        unique_urls = unique_urls[: args.limit]

    print(f"[Info] Loaded {len(rows)} rows from {input_csv.name}")
    print(f"[Info] Image columns: {image_cols} | Unique URLs to inspect: {len(unique_urls)}")

    # Load existing manifest if resuming, invalidating any entries from pre-v2 engines or missing WebP files
    manifest: Dict[str, Dict[str, Any]] = {}
    if args.resume and manifest_path.exists():
        try:
            raw_m = json.loads(manifest_path.read_text(encoding="utf-8"))
            for k_u, info in raw_m.items():
                if info.get("engine_version") != ENGINE_VERSION:
                    continue
                if info.get("status") == "cleaned":
                    fn = info.get("filename", "")
                    if not fn.endswith(".webp") or not (image_dir / fn).exists():
                        continue
                manifest[k_u] = info
            print(f"[Info] Resumed {len(manifest)} v2-verified URLs from {manifest_path.name}")
        except Exception:
            manifest = {}

    try:
        rel_img_dir = image_dir.relative_to(repo_root).as_posix()
    except ValueError:
        rel_img_dir = image_dir.name

    todo_urls = [
        u for u in unique_urls
        if u not in manifest or manifest[u].get("status") == "download_error"
    ]
    print(f"[Info] Remaining URLs to process this run: {len(todo_urls)}")

    ocr_engine = RapidOCR()
    lama_model = load_inpainter(args.inpainter)

    local_dir = Path(args.local_dir).resolve() if args.local_dir else None

    def fetch_one(u: str) -> Tuple[str, Optional[np.ndarray], Optional[str]]:
        if local_dir is not None:
            cand1 = local_dir / url_to_filename(u)
            cand2 = local_dir / Path(urlparse(u).path).name
            for cpath in (cand1, cand2):
                if cpath.exists():
                    img = decode_image_bytes(cpath.read_bytes())
                    if img is not None:
                        return u, img, None
        if u.startswith("file://"):
            p = Path(u[7:])
            if p.exists():
                img = decode_image_bytes(p.read_bytes())
                return u, img, (None if img is not None else "decode_failed")
            return u, None, "file_not_found"
        img, err = download_image(u, timeout=args.timeout, retries=args.retries)
        return u, img, err

    newly_cleaned_files: List[str] = []
    default_prefix = f"https://cdn.jsdelivr.net/gh/{args.gh_repo}@{args.gh_branch}/{rel_img_dir}"

    chunk_size = max(16, args.workers * 4)
    pbar = tqdm(total=len(todo_urls), desc="Scanning & cleaning Chinese text (v2)", unit="img")

    for start_idx in range(0, len(todo_urls), chunk_size):
        batch_urls = todo_urls[start_idx : start_idx + chunk_size]
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            fetched = list(pool.map(fetch_one, batch_urls))

        for u, img_bgr, err in fetched:
            pbar.update(1)
            if img_bgr is None:
                manifest[u] = {
                    "engine_version": ENGINE_VERSION,
                    "status": "download_error",
                    "error": err or "unknown",
                    "new_url": u,
                    "erased_texts": [],
                }
                continue

            res = run_ocr_padded(ocr_engine, img_bgr)
            regions = select_chinese_regions(res)

            if not regions:
                manifest[u] = {
                    "engine_version": ENGINE_VERSION,
                    "status": "no_chinese",
                    "new_url": u,
                    "erased_texts": [],
                }
            else:
                cleaned_bgr, _ = clean_chinese_from_image(img_bgr, regions, lama_model=lama_model)
                fname = url_to_filename(u)
                out_file = image_dir / fname
                save_cleaned_webp(
                    cleaned_bgr,
                    out_file,
                    max_dim=args.max_dim,
                    quality=args.jpeg_quality,
                )
                manifest[u] = {
                    "engine_version": ENGINE_VERSION,
                    "status": "cleaned",
                    "filename": fname,
                    "new_url": f"{default_prefix}/{fname}",
                    "erased_texts": [r["text"] for r in regions],
                }
                newly_cleaned_files.append(
                    str(out_file.relative_to(repo_root)) if out_file.is_relative_to(repo_root) else str(out_file)
                )

        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

        if args.git_push and len(newly_cleaned_files) >= args.push_batch_size:
            git_commit_and_push_batch(
                repo_root,
                args.gh_branch,
                newly_cleaned_files,
                f"Add batch of {len(newly_cleaned_files)} surrounding-color cleaned WebP images (v2)",
            )
            newly_cleaned_files.clear()

    pbar.close()

    # Push final batch of cleaned WebP images BEFORE generating CSV so we can pin jsDelivr URLs
    # to the exact clean commit SHA on `main` (bypasses any stale `@main` 50 MB CDN cache immediately!)
    cdn_ref = args.gh_branch
    if args.git_push:
        git_commit_and_push_batch(
            repo_root,
            args.gh_branch,
            newly_cleaned_files,
            "Finalize v2 cleaned WebP product images (<50 MB jsDelivr budget)",
        )
        newly_cleaned_files.clear()
        head_sha = get_head_short_sha(repo_root)
        if head_sha:
            cdn_ref = head_sha

    final_prefix = f"https://cdn.jsdelivr.net/gh/{args.gh_repo}@{cdn_ref}/{rel_img_dir}"
    cleaned_fnames: List[str] = []
    for u, info in manifest.items():
        if info.get("status") == "cleaned" and info.get("filename"):
            info["new_url"] = f"{final_prefix}/{info['filename']}"
            cleaned_fnames.append(info["filename"])

    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    replaced_cells = 0
    untouched_cells = 0
    out_rows = []
    for row in rows:
        new_row = dict(row)
        for col in image_cols:
            orig_u = (new_row.get(col) or "").strip()
            if not orig_u:
                continue
            info = manifest.get(orig_u)
            if info and info.get("status") == "cleaned" and info.get("new_url"):
                new_row[col] = info["new_url"]
                replaced_cells += 1
            else:
                untouched_cells += 1
        out_rows.append(new_row)

    with open(output_csv, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(out_rows)

    with open(report_path, "w", encoding="utf-8-sig", newline="") as f:
        rw = csv.writer(f)
        rw.writerow(["Original URL", "Status", "Final URL (jsDelivr if modified)", "Erased Chinese Text", "Error"])
        for u in unique_urls:
            info = manifest.get(u, {})
            rw.writerow([
                u,
                info.get("status", "unprocessed"),
                info.get("new_url", u),
                " | ".join(info.get("erased_texts", [])),
                info.get("error", ""),
            ])

    if args.git_push:
        csv_paths = []
        for p in (output_csv, report_path):
            if p.is_relative_to(repo_root):
                csv_paths.append(str(p.relative_to(repo_root)))
        if csv_paths:
            git_commit_and_push_batch(
                repo_root,
                args.gh_branch,
                csv_paths,
                f"Update cleaned CSV & audit report with jsDelivr @{cdn_ref} WebP links",
            )
        best_effort_purge_jsdelivr(args.gh_repo, args.gh_branch, rel_img_dir, cleaned_fnames)

    cleaned_unique = sum(1 for u in unique_urls if manifest.get(u, {}).get("status") == "cleaned")
    no_zh_unique = sum(1 for u in unique_urls if manifest.get(u, {}).get("status") == "no_chinese")
    err_unique = sum(1 for u in unique_urls if manifest.get(u, {}).get("status") == "download_error")
    total_webp_mb = sum(p.stat().st_size for p in image_dir.glob("*.webp")) / (1024 * 1024)

    print("\n" + "=" * 68)
    print("SUMMARY (v2 Surrounding-Color & WebP <50MB Engine)")
    print("=" * 68)
    print(f"Total unique URLs inspected : {len(unique_urls)}")
    print(f"  - Cleaned (had Chinese)   : {cleaned_unique}  --> replaced with jsDelivr WebP link")
    print(f"  - Untouched (no Chinese)  : {no_zh_unique}  --> original link preserved")
    print(f"  - Download errors         : {err_unique}")
    print(f"CSV image cells replaced    : {replaced_cells}")
    print(f"CSV image cells unchanged   : {untouched_cells}")
    print(f"Total cleaned-images/ size  : {total_webp_mb:.2f} MB (jsDelivr limit: 50.00 MB)")
    print(f"jsDelivr CDN ref used       : @{cdn_ref}")
    print(f"Updated CSV saved to        : {output_csv}")
    print(f"Audit report saved to       : {report_path}")
    print("=" * 68)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Erase ONLY Chinese text using surrounding background colors and update modified CSV links to jsDelivr."
    )
    p.add_argument(
        "--input",
        default="data/three_groups_products.csv",
        help="Path to input CSV file (default: data/three_groups_products.csv)",
    )
    p.add_argument(
        "--output",
        default="output/three_groups_products_cleaned.csv",
        help="Path to output CSV file (default: output/three_groups_products_cleaned.csv)",
    )
    p.add_argument(
        "--image-dir",
        default="cleaned-images",
        help="Directory inside repo to store modified images (default: cleaned-images)",
    )
    p.add_argument(
        "--repo-root",
        default=".",
        help="Path to git repository root (default: current directory)",
    )
    p.add_argument(
        "--gh-repo",
        default="mokaimmaman/njimkoylo",
        help="GitHub owner/repo for jsDelivr URLs (default: mokaimmaman/njimkoylo)",
    )
    p.add_argument(
        "--gh-branch",
        default="main",
        help="Git branch for pushing cleaned images (default: main)",
    )
    p.add_argument(
        "--image-cols",
        default="Image 1,Image 2,Image 3,Image 4,Image 5",
        help="Comma-separated image column names in CSV",
    )
    p.add_argument(
        "--inpainter",
        choices=["auto", "lama", "opencv"],
        default="auto",
        help="Inpainting backend for Tier-3 textured photo strokes: auto (LaMa on GPU/CPU), lama, or opencv",
    )
    p.add_argument(
        "--workers",
        type=int,
        default=12,
        help="Concurrent image download threads (default: 12)",
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=25,
        help="HTTP download timeout in seconds (default: 25)",
    )
    p.add_argument(
        "--retries",
        type=int,
        default=3,
        help="Download retry attempts per URL (default: 3)",
    )
    p.add_argument(
        "--max-dim",
        type=int,
        default=720,
        help="Max output WebP width/height in pixels to stay <50 MB on jsDelivr (default: 720)",
    )
    p.add_argument(
        "--jpeg-quality",
        "--quality",
        dest="jpeg_quality",
        type=int,
        default=76,
        help="WebP save quality 1-100 (default: 76)",
    )
    p.add_argument(
        "--local-dir",
        default=None,
        help="Optional local folder of pre-downloaded images",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Optional max number of unique URLs to process (0 = all)",
    )
    p.add_argument(
        "--resume",
        action="store_true",
        default=True,
        help="Resume from output/progress_manifest.json if present (default: enabled)",
    )
    p.add_argument(
        "--no-resume",
        dest="resume",
        action="store_false",
        help="Ignore existing progress manifest and reprocess everything",
    )
    p.add_argument(
        "--git-push",
        action="store_true",
        default=False,
        help="Automatically git add, commit, and push cleaned images & output CSV in batches",
    )
    p.add_argument(
        "--push-batch-size",
        type=int,
        default=200,
        help="Number of cleaned images per git push batch when --git-push is enabled (default: 200)",
    )
    return p.parse_args()


if __name__ == "__main__":
    process_csv(parse_args())
