#!/usr/bin/env python3
"""
Chinese-Only Product Image Text Eraser & jsDelivr CSV Updater
=============================================================
- Scans product image URLs (Image 1 .. Image 5) from a CSV file.
- Detects ONLY Chinese text using padded PP-OCRv4 (RapidOCR) with character-level word boxes:
  * Images WITHOUT Chinese text are left 100% untouched (original URL preserved in CSV).
  * Non-Chinese specs/labels (e.g. USB, TYPE-C, 5V2.1A, 28cm, 22L, 60min, IPX7, 1:24, NO.8605, ODM OEM)
    are preserved even when they appear on the same line as Chinese text.
  * Geometric single-character icon false positives (e.g. '+' button -> '田', '[>]' icon -> '回') are filtered out.
- Erases ONLY the Chinese text regions using a hybrid engine:
  1) Solid-Badge / Studio-Canvas Topology Fill (Jordan-curve outer-canvas protection + purest non-halo color fill)
     for crisp colored callout pills, banners, and white backgrounds.
  2) Neural LaMa Inpainting (`simple-lama-inpainting`, GPU/CPU auto-detected) or Multi-Scale Coarse-to-Fine
     OpenCV Telea fallback for complex photographic/gradient backgrounds, keeping all unmasked pixels bit-exact.
- Saves ONLY modified images to `cleaned-images/<sha1_12>.jpg` and replaces ONLY those changed URLs in the
  output CSV with `https://cdn.jsdelivr.net/gh/<repo>@<branch>/cleaned-images/<sha1_12>.jpg`.
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

# ---------------------------------------------------------------------------
# Unicode & Token Rules
# ---------------------------------------------------------------------------

# Chinese Han ideographs (CJK Unified Ideographs + Extension A + Compatibility)
HAN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")

# Fullwidth / CJK punctuation associated with Chinese copy
CJK_PUNCT_RE = re.compile(
    r"[\u3000-\u303f\uff01-\uff60\uffe0-\uffe6（）《》【】「」『』、。，；：！？—…]"
)

# Single geometric Han characters that OCR often hallucinates on UI icons, grilles, '+' buttons, or ports
# when NO genuine multi-character Chinese text exists elsewhere on the image.
GEOMETRIC_SINGLE_HAN = set(
    "口回田日日目十丨一二三川山工丁厂冂凵匚囗乂了卜入八人几刀力又土士干千寸小大团国出合"
)

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
    out = poly.copy()
    for i in range(len(out)):
        if out[i, 0] < cx - 1.5:
            out[i, 0] = min(cx - 0.5, out[i, 0] + px)
        elif out[i, 0] > cx + 1.5:
            out[i, 0] = max(cx + 0.5, out[i, 0] - px)
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
        if len(han_chars) >= 2 and score >= 0.62:
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

        # Filter isolated single-character icon misrecognitions
        if len(han_chars) == 1:
            ch = han_chars[0]
            if score < 0.75:
                continue
            if ch in GEOMETRIC_SINGLE_HAN and strong_chinese_lines == 0:
                continue
            if strong_chinese_lines == 0 and score < 0.90:
                continue
        else:
            if score < 0.55:
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
                        shrink_poly_horizontal(np.array(word_boxes[idx], dtype=np.float32), 2.5)
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
            regions.append(
                {
                    "poly": line_box,
                    "keep_polys": list(global_keep_polys),
                    "text": txt,
                    "is_subline": False,
                }
            )

    # Catch standalone punctuation boxes (e.g. '!' or '！') sitting adjacent to an erased Chinese line
    if regions:
        for item in res:
            txt = item[1].strip()
            if not txt or HAN_RE.search(txt):
                continue
            if re.fullmatch(r"[!！?？:：~～、，。…·]+", txt):
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
# Hybrid Inpainting Engine (Solid-Badge Topology Fill + LaMa / Multi-Scale Telea)
# ---------------------------------------------------------------------------


def try_solid_badge_fill(
    roi: np.ndarray,
    pm_tight: np.ndarray,
    pm_exp: np.ndarray,
    prot_roi: np.ndarray,
    all_text_roi: np.ndarray,
) -> Tuple[np.ndarray, bool]:
    """
    Detect if text sits inside a flat/solid-colored graphic badge, pill, banner, or solid studio canvas.
    Uses Jordan-curve outer-canvas component exclusion + collinear anti-aliasing rejection + purest-quantile fill.
    """
    moat = cv2.dilate(pm_tight, np.ones((7, 7), np.uint8)) - pm_tight
    moat[all_text_roi > 0] = 0
    moat[prot_roi > 0] = 0
    if np.count_nonzero(moat) < 8 or np.count_nonzero(pm_tight) < 8:
        return roi, False

    in_pts = roi[pm_tight > 0].astype(np.float32)
    ys_m, xs_m = np.where(moat > 0)
    moat_pts = roi[ys_m, xs_m].astype(np.float32)

    rh, rw = roi.shape[:2]
    far_mask = np.zeros((rh, rw), dtype=bool)
    far_mask[:2, :] = True
    far_mask[-2:, :] = True
    far_mask[:, :2] = True
    far_mask[:, -2:] = True
    far_mask[pm_exp > 0] = False
    far_pts = roi[far_mask].astype(np.float32) if np.count_nonzero(far_mask) >= 8 else moat_pts

    all_pts = np.vstack([in_pts, moat_pts])
    K = 3 if len(all_pts) >= 36 else 2
    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 20, 1.0)
    _, _, centers = cv2.kmeans(all_pts, K, None, criteria, 3, cv2.KMEANS_PP_CENTERS)

    in_lbl = np.argmin(np.linalg.norm(in_pts[:, None, :] - centers[None, :, :], axis=2), axis=1)
    moat_lbl = np.argmin(np.linalg.norm(moat_pts[:, None, :] - centers[None, :, :], axis=2), axis=1)

    best_bg_med: Optional[np.ndarray] = None
    best_score = -1e9
    best_k: Optional[int] = None

    for k in range(K):
        sel_m = moat_lbl == k
        m_pts_k = moat_pts[sel_m]
        if len(m_pts_k) < 5:
            continue
        moat_f = len(m_pts_k) / len(moat_pts)
        in_f = float(np.mean(in_lbl == k))
        if moat_f < 0.14 or in_f < 0.15:
            continue

        med_raw = np.median(m_pts_k, axis=0)
        moat_mad = float(np.median(np.linalg.norm(m_pts_k - med_raw, axis=1)))
        if moat_mad > 24.0:
            continue

        xs_k = xs_m[sel_m]
        if len(xs_k) >= 10 and (xs_k.max() - xs_k.min()) > 30:
            mid_x = 0.5 * (xs_k.min() + xs_k.max())
            left_k = m_pts_k[xs_k <= mid_x]
            right_k = m_pts_k[xs_k > mid_x]
            if len(left_k) >= 3 and len(right_k) >= 3:
                lr_diff = float(np.linalg.norm(np.median(left_k, axis=0) - np.median(right_k, axis=0)))
                if lr_diff > 22.0:
                    continue

        non_k_in = in_pts[in_lbl != k]
        if len(non_k_in) < 0.08 * len(in_pts):
            continue
        fg_med = np.median(non_k_in, axis=0)
        contrast = float(np.linalg.norm(fg_med - med_raw))
        if contrast < 26.0:
            continue

        # Reject intermediate anti-aliased transition cluster sitting between the other two clusters
        if K == 3:
            others = [centers[j] for j in range(3) if j != k]
            seg = others[1] - others[0]
            seg_len = float(np.linalg.norm(seg))
            if seg_len > 40.0:
                t_proj = float(np.dot(centers[k] - others[0], seg) / (seg_len * seg_len))
                if 0.18 < t_proj < 0.82:
                    perp = float(np.linalg.norm((centers[k] - others[0]) - t_proj * seg))
                    if perp < 25.0:
                        continue

        d_from_fg = np.linalg.norm(m_pts_k - fg_med[None, :], axis=1)
        pure_pts = m_pts_k[d_from_fg >= np.median(d_from_fg)]
        pure_med = np.median(pure_pts, axis=0)

        far_f = float(np.mean(np.linalg.norm(far_pts - pure_med[None, :], axis=1) < 26.0))
        far_mad = float(np.median(np.linalg.norm(far_pts - np.median(far_pts, axis=0), axis=1)))
        is_enclosed_badge = far_f < 0.32

        if not is_enclosed_badge and (moat_mad > 6.5 or far_mad > 8.5):
            continue

        sc = (2.2 if is_enclosed_badge else 0.0) + 1.2 * moat_f + 1.0 * in_f - moat_mad * 0.02
        if sc > best_score:
            best_score = sc
            best_bg_med = pure_med
            best_k = k

    if best_bg_med is None or best_k is None:
        return roi, False

    dist_to_bg = np.linalg.norm(roi.astype(np.float32) - best_bg_med[None, None, :], axis=2)

    outer_canvas_mask = np.zeros((rh, rw), dtype=bool)
    for k in range(K):
        if k == best_k:
            continue
        if np.linalg.norm(centers[k] - best_bg_med) > 28.0:
            far_match = np.linalg.norm(far_pts - centers[k][None, :], axis=1) < 28.0
            if np.mean(far_match) >= 0.20:
                outer_med_k = np.median(far_pts[far_match], axis=0)
                dist_outer_k = np.linalg.norm(roi.astype(np.float32) - outer_med_k[None, None, :], axis=2)
                cand_outer = ((dist_outer_k < 22.0) & (dist_outer_k < dist_to_bg)).astype(np.uint8)
                _, cc_lbl = cv2.connectedComponents(cand_outer, connectivity=4)
                border_ccs = set(np.unique(cc_lbl[far_mask])) - {0}
                for bcc in border_ccs:
                    outer_canvas_mask |= cc_lbl == bcc

    fill_mask = ((pm_exp > 0) & (~outer_canvas_mask)).astype(np.uint8) * 255
    fill_mask[prot_roi > 0] = 0

    alpha = cv2.GaussianBlur(fill_mask.astype(np.float32) / 255.0, (3, 3), 0)
    alpha = np.clip(alpha * 1.6, 0.0, 1.0)[..., None]
    alpha[outer_canvas_mask] = 0.0
    alpha[prot_roi > 0] = 0.0

    filled = roi.astype(np.float32) * (1.0 - alpha) + best_bg_med[None, None, :] * alpha
    return np.clip(np.round(filled), 0, 255).astype(np.uint8), True


def clean_chinese_from_image(
    img_bgr: np.ndarray,
    regions: List[Dict[str, Any]],
    lama_model: Optional[Any] = None,
) -> Tuple[np.ndarray, bool]:
    """Erase all selected Chinese regions while keeping non-Chinese parts untouched."""
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

    photo_inpaint_mask = np.zeros((h, w), dtype=np.uint8)

    for reg in regions:
        poly = np.array(reg["poly"], dtype=np.float32)
        rect = cv2.minAreaRect(poly)
        bw, bh = rect[1]
        char_h = max(min(bw, bh), 8.0)

        pad_px = max(2, int(round(char_h * (0.10 if reg["is_subline"] else 0.16))))
        center = poly.mean(axis=0, keepdims=True)
        vec = poly - center
        norms = np.linalg.norm(vec, axis=1, keepdims=True) + 1e-6
        extra_x = 2.0 if reg["is_subline"] else min(12.0, char_h * 0.65)
        exp_pts = poly + (vec / norms) * (pad_px * 1.35)
        if extra_x > 0:
            for idx_p in range(len(exp_pts)):
                exp_pts[idx_p, 0] += -extra_x if exp_pts[idx_p, 0] < center[0, 0] else extra_x

        exp_poly = np.clip(np.round(exp_pts), [0, 0], [w - 1, h - 1]).astype(np.int32)
        tight_poly = np.clip(np.round(poly), [0, 0], [w - 1, h - 1]).astype(np.int32)

        x0 = max(0, int(exp_poly[:, 0].min()) - 14)
        y0 = max(0, int(exp_poly[:, 1].min()) - 14)
        x1 = min(w, int(exp_poly[:, 0].max()) + 15)
        y1 = min(h, int(exp_poly[:, 1].max()) + 15)
        if x1 - x0 < 3 or y1 - y0 < 3:
            continue

        roi = out[y0:y1, x0:x1]
        rh, rw = roi.shape[:2]

        pm_tight = np.zeros((rh, rw), dtype=np.uint8)
        cv2.fillPoly(pm_tight, [tight_poly - np.array([[x0, y0]])], 255)

        pm_exp = np.zeros((rh, rw), dtype=np.uint8)
        cv2.fillPoly(pm_exp, [exp_poly - np.array([[x0, y0]])], 255)

        prot_roi = protect_mask[y0:y1, x0:x1]
        all_txt_roi = all_chinese_tight[y0:y1, x0:x1]
        pm_exp[prot_roi > 0] = 0
        pm_tight[prot_roi > 0] = 0

        if np.count_nonzero(pm_tight) < 4:
            continue

        filled_roi, ok = try_solid_badge_fill(roi, pm_tight, pm_exp, prot_roi, all_txt_roi)
        if ok:
            out[y0:y1, x0:x1] = filled_roi
        else:
            photo_inpaint_mask[y0:y1, x0:x1] = np.maximum(photo_inpaint_mask[y0:y1, x0:x1], pm_exp)

    photo_inpaint_mask[protect_mask > 0] = 0

    if np.count_nonzero(photo_inpaint_mask) > 0:
        if lama_model is not None:
            pil_in = Image.fromarray(cv2.cvtColor(out, cv2.COLOR_BGR2RGB))
            pil_mask = Image.fromarray(photo_inpaint_mask)
            pil_out = lama_model(pil_in, pil_mask)
            if pil_out.size != (w, h):
                pil_out = pil_out.resize((w, h), Image.Resampling.LANCZOS)
            lama_bgr = cv2.cvtColor(np.array(pil_out), cv2.COLOR_RGB2BGR)
            # Blend strictly inside feathered mask so unmasked product pixels stay 100% bit-exact
            dil_m = cv2.dilate(photo_inpaint_mask, np.ones((3, 3), np.uint8))
            alpha = cv2.GaussianBlur(dil_m.astype(np.float32) / 255.0, (5, 5), 0)
            alpha = np.clip(alpha * 1.4, 0.0, 1.0)[..., None]
            alpha[protect_mask > 0] = 0.0
            out = np.clip(
                np.round(out.astype(np.float32) * (1.0 - alpha) + lama_bgr.astype(np.float32) * alpha),
                0,
                255,
            ).astype(np.uint8)
        else:
            dist = cv2.distanceTransform(photo_inpaint_mask, cv2.DIST_L2, 5)
            max_radius = float(dist.max()) if dist.size else 3.0
            if max_radius > 7.5:
                scale = min(0.5, 5.0 / max_radius)
                sw, sh = max(16, int(round(w * scale))), max(16, int(round(h * scale)))
                small_img = cv2.resize(out, (sw, sh), interpolation=cv2.INTER_AREA)
                small_mask = cv2.resize(photo_inpaint_mask, (sw, sh), interpolation=cv2.INTER_NEAREST)
                small_mask = cv2.dilate(small_mask, np.ones((3, 3), np.uint8))
                coarse = cv2.inpaint(small_img, small_mask, inpaintRadius=5, flags=cv2.INPAINT_TELEA)
                coarse_up = cv2.resize(coarse, (w, h), interpolation=cv2.INTER_CUBIC)

                core = (dist > 3.5).astype(np.uint8) * 255
                out[core > 0] = coarse_up[core > 0]
                boundary_mask = cv2.subtract(photo_inpaint_mask, cv2.erode(core, np.ones((5, 5), np.uint8)))
                out = cv2.inpaint(out, boundary_mask, inpaintRadius=4, flags=cv2.INPAINT_TELEA)
            else:
                r = max(3, min(6, int(round(max_radius + 2))))
                out = cv2.inpaint(out, photo_inpaint_mask, inpaintRadius=r, flags=cv2.INPAINT_TELEA)

    return out, True


# ---------------------------------------------------------------------------
# Image Downloading, Decoding & Saving
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


def save_cleaned_jpeg(img_bgr: np.ndarray, dst_path: Path, max_dim: int = 1200, quality: int = 90) -> None:
    """Save cleaned BGR image as optimized progressive JPEG well within jsDelivr size limits."""
    h, w = img_bgr.shape[:2]
    if max_dim > 0 and max(h, w) > max_dim:
        scale = max_dim / float(max(h, w))
        nw, nh = int(round(w * scale)), int(round(h * scale))
        img_bgr = cv2.resize(img_bgr, (nw, nh), interpolation=cv2.INTER_AREA)

    dst_path.parent.mkdir(parents=True, exist_ok=True)
    pil_rgb = Image.fromarray(cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB))
    pil_rgb.save(dst_path, format="JPEG", quality=quality, optimize=True, progressive=True)


def url_to_filename(url: str) -> str:
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:14]
    return f"img_{digest}.jpg"


# ---------------------------------------------------------------------------
# Optional LaMa Loader & Git Batch Pusher
# ---------------------------------------------------------------------------


def load_inpainter(mode: str) -> Optional[Any]:
    """Load `SimpleLama` neural inpainter when requested/available; return None to use OpenCV fallback."""
    if mode == "opencv":
        return None
    try:
        import torch
        from simple_lama_inpainting import SimpleLama

        device = "cuda" if torch.cuda.is_available() else "cpu"
        if mode == "auto" and device == "cpu":
            # Allow CPU LaMa only if explicitly requested via --inpainter lama, OR if CUDA is present in auto mode
            # because on 2,000 images CPU LaMa takes ~2.5s/img whereas GPU LaMa takes ~0.08s/img!
            print("[Info] No CUDA GPU detected; using fast Solid-Badge + Multi-Scale Telea inpainter (pass --inpainter lama to force CPU LaMa).")
            return None
        print(f"[Info] Loading SimpleLama neural inpainter on {device}...")
        return SimpleLama(device=torch.device(device))
    except Exception as exc:
        if mode == "lama":
            print(f"[Warning] Could not load SimpleLama ({exc}); falling back to OpenCV multi-scale inpainter.")
        return None


def git_commit_and_push_batch(repo_root: Path, branch: str, paths_to_add: List[str], message: str) -> None:
    subprocess.run(["git", "add", "--"] + paths_to_add, cwd=repo_root, check=True)
    status = subprocess.run(["git", "status", "--porcelain"], cwd=repo_root, capture_output=True, text=True, check=True)
    if not status.stdout.strip():
        return
    subprocess.run(["git", "commit", "-m", message], cwd=repo_root, check=True)
    subprocess.run(["git", "push", "origin", branch], cwd=repo_root, check=True)


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

    with open(input_csv, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)

    image_cols = [c.strip() for c in args.image_cols.split(",") if c.strip() in fieldnames]
    if not image_cols:
        raise ValueError(f"None of the specified image columns ({args.image_cols}) found in CSV header: {fieldnames}")

    # Collect unique URLs in deterministic order
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

    # Load existing manifest if resuming
    manifest: Dict[str, Dict[str, Any]] = {}
    if args.resume and manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            print(f"[Info] Resumed {len(manifest)} previously processed URLs from {manifest_path.name}")
        except Exception:
            manifest = {}

    # Relative path of image_dir inside repo for jsDelivr URL construction
    try:
        rel_img_dir = image_dir.relative_to(repo_root).as_posix()
    except ValueError:
        rel_img_dir = image_dir.name

    jsdelivr_prefix = f"https://cdn.jsdelivr.net/gh/{args.gh_repo}@{args.gh_branch}/{rel_img_dir}"

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
            # Support local filename match by URL basename or sha1 filename
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
    processed_since_save = 0

    # Process in chunks so downloads are parallelized while OCR/inpainting stays memory-bounded
    chunk_size = max(16, args.workers * 4)
    pbar = tqdm(total=len(todo_urls), desc="Scanning & cleaning Chinese text", unit="img")

    for start_idx in range(0, len(todo_urls), chunk_size):
        batch_urls = todo_urls[start_idx : start_idx + chunk_size]
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            fetched = list(pool.map(fetch_one, batch_urls))

        for u, img_bgr, err in fetched:
            pbar.update(1)
            if img_bgr is None:
                manifest[u] = {
                    "status": "download_error",
                    "error": err or "unknown",
                    "new_url": u,
                    "erased_texts": [],
                }
                continue

            res = run_ocr_padded(ocr_engine, img_bgr)
            regions = select_chinese_regions(res)

            if not regions:
                # NO Chinese text -> keep original URL untouched!
                manifest[u] = {
                    "status": "no_chinese",
                    "new_url": u,
                    "erased_texts": [],
                }
            else:
                cleaned_bgr, _ = clean_chinese_from_image(img_bgr, regions, lama_model=lama_model)
                fname = url_to_filename(u)
                out_file = image_dir / fname
                save_cleaned_jpeg(cleaned_bgr, out_file, max_dim=args.max_dim, quality=args.jpeg_quality)
                new_url = f"{jsdelivr_prefix}/{fname}"
                manifest[u] = {
                    "status": "cleaned",
                    "filename": fname,
                    "new_url": new_url,
                    "erased_texts": [r["text"] for r in regions],
                }
                newly_cleaned_files.append(str(out_file.relative_to(repo_root)) if out_file.is_relative_to(repo_root) else str(out_file))

            processed_since_save += 1

        # Checkpoint manifest after each chunk
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

        # Optional incremental git commit & push every `push_batch_size` cleaned images
        if args.git_push and len(newly_cleaned_files) >= args.push_batch_size:
            git_commit_and_push_batch(
                repo_root,
                args.gh_branch,
                newly_cleaned_files,
                f"Add batch of {len(newly_cleaned_files)} Chinese-cleaned product images",
            )
            newly_cleaned_files.clear()

    pbar.close()

    # Write updated CSV (replacing ONLY URLs whose status == 'cleaned')
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

    # Write detailed audit report CSV
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

    # Final git push if requested
    if args.git_push:
        to_commit = list(newly_cleaned_files)
        for p in (output_csv, report_path):
            if p.is_relative_to(repo_root):
                to_commit.append(str(p.relative_to(repo_root)))
        if to_commit:
            git_commit_and_push_batch(
                repo_root,
                args.gh_branch,
                to_commit,
                "Update cleaned product images and CSV with jsDelivr links",
            )

    cleaned_unique = sum(1 for u in unique_urls if manifest.get(u, {}).get("status") == "cleaned")
    no_zh_unique = sum(1 for u in unique_urls if manifest.get(u, {}).get("status") == "no_chinese")
    err_unique = sum(1 for u in unique_urls if manifest.get(u, {}).get("status") == "download_error")

    print("\n" + "=" * 64)
    print("SUMMARY")
    print("=" * 64)
    print(f"Total unique URLs inspected : {len(unique_urls)}")
    print(f"  - Cleaned (had Chinese)   : {cleaned_unique}  --> replaced with jsDelivr link")
    print(f"  - Untouched (no Chinese)  : {no_zh_unique}  --> original link preserved")
    print(f"  - Download errors         : {err_unique}")
    print(f"CSV image cells replaced    : {replaced_cells}")
    print(f"CSV image cells unchanged   : {untouched_cells}")
    print(f"Cleaned images directory    : {image_dir}")
    print(f"Updated CSV saved to        : {output_csv}")
    print(f"Audit report saved to       : {report_path}")
    print("=" * 64)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Erase ONLY Chinese text from product images in CSV and update modified links to jsDelivr."
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
        help="Git branch/tag/SHA for jsDelivr URLs (default: main)",
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
        help="Inpainting backend for complex backgrounds: auto (GPU LaMa if available else OpenCV), lama, or opencv",
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
        default=1200,
        help="Max output image width/height in pixels (default: 1200)",
    )
    p.add_argument(
        "--jpeg-quality",
        type=int,
        default=90,
        help="JPEG save quality 1-100 (default: 90)",
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
