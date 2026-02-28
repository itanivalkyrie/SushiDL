import argparse
import re
import statistics
from collections import Counter
from pathlib import Path
from zipfile import ZipFile

from PIL import Image, ImageChops, ImageStat

try:
    import cv2
    import numpy as np

    HAS_OPENCV = True
except Exception:
    cv2 = None
    np = None
    HAS_OPENCV = False


VALID_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
OUTPUT_MODES = ("images", "cbz", "both")


def natural_sort_key(name: str):
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", name)]


def is_mostly_white(pil_img: Image.Image, ratio_threshold: float = 0.98, white_cutoff: int = 245) -> bool:
    gray = pil_img.convert("L")
    histogram = gray.histogram()
    white_pixels = sum(histogram[white_cutoff + 1 :])
    ratio_white = white_pixels / (gray.width * gray.height)
    return ratio_white > ratio_threshold


def is_mostly_dark(pil_img: Image.Image, ratio_threshold: float = 0.98, dark_cutoff: int = 12) -> bool:
    gray = pil_img.convert("L")
    histogram = gray.histogram()
    dark_pixels = sum(histogram[: dark_cutoff + 1])
    ratio_dark = dark_pixels / (gray.width * gray.height)
    return ratio_dark > ratio_threshold


def is_low_texture(pil_img: Image.Image, stddev_threshold: float = 6.0) -> bool:
    gray = pil_img.convert("L")
    stddev = ImageStat.Stat(gray).stddev[0]
    return stddev <= stddev_threshold


def normalize_width(img: Image.Image, target_width: int, return_action: bool = False):
    action = "unchanged"
    if target_width <= 0:
        out = img
        if return_action:
            return out, action
        return out
    if img.width == target_width:
        out = img
        if return_action:
            return out, action
        return out
    if img.width > target_width:
        # Always keep original aspect ratio when reducing extra-wide images.
        scale = target_width / float(img.width)
        new_height = max(1, int(round(img.height * scale)))
        out = img.resize((target_width, new_height), Image.Resampling.LANCZOS)
        action = "resized_down_keep_ratio"
        if return_action:
            return out, action
        return out
    canvas = Image.new("RGB", (target_width, img.height), (255, 255, 255))
    canvas.paste(img, (0, 0))
    out = canvas
    action = "padded"
    if return_action:
        return out, action
    return out


def trim_top(img: Image.Image, pixels: int) -> Image.Image:
    if pixels <= 0:
        return img
    if pixels >= img.height:
        raise ValueError(f"trim-top ({pixels}) is >= image height ({img.height}).")
    return img.crop((0, pixels, img.width, img.height))


def trim_bottom(img: Image.Image, pixels: int) -> Image.Image:
    if pixels <= 0:
        return img
    if pixels >= img.height:
        raise ValueError(f"trim-last-bottom ({pixels}) is >= image height ({img.height}).")
    return img.crop((0, 0, img.width, img.height - pixels))


def build_default_output_folder(input_folder: Path) -> Path:
    return input_folder / f"{input_folder.name}_cut"


def load_images(input_folder: Path):
    image_paths = sorted(
        [p for p in input_folder.iterdir() if p.is_file() and p.suffix.lower() in VALID_EXTENSIONS],
        key=lambda p: natural_sort_key(p.name),
    )
    if not image_paths:
        raise FileNotFoundError(f"No supported images found in: {input_folder}")

    images = []
    for path in image_paths:
        with Image.open(path) as img:
            images.append(img.convert("RGB"))
    return image_paths, images


def _moving_average_1d(arr: np.ndarray, window: int) -> np.ndarray:
    if arr.size == 0:
        return arr
    w = max(1, int(window))
    if w == 1:
        return arr
    kernel = np.ones(w, dtype=np.float32) / float(w)
    return np.convolve(arr.astype(np.float32), kernel, mode="same")


def compute_orange_row_ratio(img: Image.Image) -> np.ndarray:
    arr = np.asarray(img.convert("RGB"), dtype=np.uint8)
    if arr.ndim != 3 or arr.shape[2] != 3:
        return np.zeros((img.height,), dtype=np.float32)

    r = arr[:, :, 0].astype(np.int16)
    g = arr[:, :, 1].astype(np.int16)
    b = arr[:, :, 2].astype(np.int16)

    # Orange mask tuned for SushiScan banner colors.
    mask = (
        (r >= 145)
        & (g >= 65)
        & (b <= 140)
        & ((r - g) >= 20)
        & ((g - b) >= 5)
    )
    return mask.mean(axis=1).astype(np.float32)


def detect_top_banner_trim(
    img: Image.Image,
    default_trim: int,
    scan_limit: int = 1300,
    row_threshold: float = 0.12,
    smooth_window: int = 35,
    raw_row_threshold: float = 0.05,
) -> int:
    h = img.height
    if h <= 2:
        return default_trim
    scan_h = min(max(1, int(scan_limit)), h - 1)
    ratios = compute_orange_row_ratio(img)[:scan_h]
    smooth = _moving_average_1d(ratios, smooth_window)
    mask = smooth >= float(row_threshold)
    idx = np.where(mask)[0]
    if idx.size == 0:
        return default_trim

    runs = []
    start = None
    for i, flag in enumerate(mask):
        if flag and start is None:
            start = i
        elif not flag and start is not None:
            runs.append((start, i - 1))
            start = None
    if start is not None:
        runs.append((start, len(mask) - 1))
    if not runs:
        return default_trim

    # Prefer the orange run around the expected trim area, not isolated
    # orange objects lower in the cover.
    chosen = None
    for r0, r1 in runs:
        if r0 <= default_trim <= (r1 + 4):
            chosen = (r0, r1)
            break
    if chosen is None:
        near_runs = [r for r in runs if r[0] <= default_trim + 120]
        if near_runs:
            chosen = max(near_runs, key=lambda r: (r[1] - r[0], r[1]))
        else:
            chosen = runs[0]

    candidate = int(chosen[1]) + 1
    # Refine with non-smoothed ratios to avoid ~10px over-trim introduced by
    # smoothing at the banner edge.
    refine_from = max(0, int(chosen[0]) - int(smooth_window))
    refine_to = min(scan_h - 1, int(chosen[1]) + int(smooth_window))
    strong_idx = np.where(ratios[refine_from : refine_to + 1] >= float(raw_row_threshold))[0]
    if strong_idx.size > 0:
        candidate = refine_from + int(strong_idx[-1]) + 1

    low = max(0, default_trim - 120)
    high = default_trim + 120
    candidate = max(low, min(high, candidate))
    return candidate


def detect_bottom_banner_trim(
    img: Image.Image,
    default_trim: int,
    scan_limit: int = 1300,
    row_threshold: float = 0.12,
    smooth_window: int = 35,
    raw_row_threshold: float = 0.05,
) -> int:
    h = img.height
    if h <= 2:
        return default_trim
    start = max(0, h - max(1, int(scan_limit)))
    ratios = compute_orange_row_ratio(img)[start:]
    smooth = _moving_average_1d(ratios, smooth_window)
    mask = smooth >= float(row_threshold)
    idx = np.where(mask)[0]
    if idx.size == 0:
        return default_trim

    runs = []
    run_start = None
    for i, flag in enumerate(mask):
        if flag and run_start is None:
            run_start = i
        elif not flag and run_start is not None:
            runs.append((run_start, i - 1))
            run_start = None
    if run_start is not None:
        runs.append((run_start, len(mask) - 1))
    if not runs:
        return default_trim

    expected_banner_start = h - default_trim
    chosen = None
    for r0, r1 in runs:
        abs_r0 = start + r0
        abs_r1 = start + r1
        if abs_r0 <= expected_banner_start <= (abs_r1 + 4):
            chosen = (r0, r1)
            break
    if chosen is None:
        # Prefer closest run start to expected position.
        chosen = min(runs, key=lambda r: abs((start + r[0]) - expected_banner_start))

    banner_start = start + int(chosen[0])
    # Refine with non-smoothed ratios to avoid edge bias from smoothing.
    refine_from = max(0, int(chosen[0]) - int(smooth_window))
    refine_to = min(len(ratios) - 1, int(chosen[1]) + int(smooth_window))
    strong_idx = np.where(ratios[refine_from : refine_to + 1] >= float(raw_row_threshold))[0]
    if strong_idx.size > 0:
        banner_start = start + refine_from + int(strong_idx[0])

    candidate_trim = h - banner_start
    low = max(0, default_trim - 120)
    high = default_trim + 120
    candidate_trim = max(low, min(high, candidate_trim))
    return candidate_trim


def select_target_width(images, width_mode: str = "auto") -> int:
    widths = [img.width for img in images if img.width > 0]
    if not widths:
        return 0

    mode = (width_mode or "auto").lower()
    if mode == "min":
        return min(widths)
    if mode == "max":
        return max(widths)
    if mode == "mode":
        return Counter(widths).most_common(1)[0][0]

    # auto (learned from *_ok references):
    # - if a significant x2-width population exists, keep max width and pad narrow pages;
    # - otherwise normalize to minimum width.
    counts = Counter(widths)
    min_w = min(widths)
    max_w = max(widths)
    total = len(widths)
    if (
        len(counts) >= 2
        and max_w / float(max(1, min_w)) >= 1.9
        and counts[max_w] / float(total) >= 0.15
    ):
        return max_w
    return min_w


def prepare_images(
    images,
    trim_first_top: int,
    trim_last_bottom: int,
    width_mode: str = "auto",
    auto_banner_detect: bool = True,
):
    if not images:
        return [], 0, trim_first_top, trim_last_bottom, {}

    target_width = select_target_width(images, width_mode=width_mode)
    normalized = []
    normalize_stats = Counter()
    for img in images:
        fixed, action = normalize_width(img, target_width, return_action=True)
        normalized.append(fixed)
        normalize_stats[action] += 1

    applied_trim_first = trim_first_top
    applied_trim_last = trim_last_bottom
    if auto_banner_detect and normalized:
        applied_trim_first = detect_top_banner_trim(normalized[0], trim_first_top)
        applied_trim_last = detect_bottom_banner_trim(normalized[-1], trim_last_bottom)

    prepared = []
    for idx, img in enumerate(normalized):
        current = img
        if idx == 0 and applied_trim_first > 0:
            current = trim_top(current, applied_trim_first)
        if idx == len(normalized) - 1 and applied_trim_last > 0:
            current = trim_bottom(current, applied_trim_last)
        prepared.append(current)

    return prepared, target_width, applied_trim_first, applied_trim_last, dict(normalize_stats)


def infer_page_height(images, fallback: int = 2132) -> int:
    if not images:
        return fallback

    if len(images) >= 3:
        candidates = [img.height for img in images[1:-1]]
    else:
        candidates = [img.height for img in images]

    candidates = [h for h in candidates if h > 0]
    if not candidates:
        return fallback

    # SushiScan segmented captures are typically 2000/2100 + 1250 heights,
    # while the reconstructed manga page target is 2250.
    has_tall = any(1850 <= h <= 2150 for h in candidates)
    has_short = any(1100 <= h <= 1400 for h in candidates)
    if has_tall and has_short:
        return 2250

    counts = Counter(candidates)
    mode_height, mode_count = counts.most_common(1)[0]
    median_height = int(round(statistics.median(candidates)))

    if mode_count >= max(2, len(candidates) // 2):
        return mode_height
    return median_height


def concatenate_images(images, width: int) -> Image.Image:
    total_height = sum(img.height for img in images)
    big_img = Image.new("RGB", (width, total_height), (255, 255, 255))
    y = 0
    for img in images:
        big_img.paste(img, (0, y))
        y += img.height
    return big_img


def mean_abs_diff(img_a: Image.Image, img_b: Image.Image) -> float:
    diff = ImageChops.difference(img_a, img_b)
    stat = ImageStat.Stat(diff)
    return sum(stat.mean) / len(stat.mean)


def overlap_band_stddev(gray_band: Image.Image) -> float:
    return ImageStat.Stat(gray_band).stddev[0]


def detect_bottom_overlap_pil(
    prev_page: Image.Image,
    next_page: Image.Image,
    max_overlap_px: int,
    score_threshold: float,
    min_stddev: float,
) -> int:
    limit = min(max_overlap_px, prev_page.height - 1, next_page.height - 1)
    if limit <= 0:
        return 0

    best = 0
    for px in range(1, limit + 1):
        prev_band = prev_page.crop((0, prev_page.height - px, prev_page.width, prev_page.height))
        next_band = next_page.crop((0, 0, next_page.width, px))

        score = mean_abs_diff(prev_band, next_band)
        if score > score_threshold:
            continue

        if min_stddev > 0:
            prev_std = overlap_band_stddev(prev_band.convert("L"))
            next_std = overlap_band_stddev(next_band.convert("L"))
            if prev_std < min_stddev or next_std < min_stddev:
                continue

        best = px

    return best


def _resize_gray_for_overlap(img: Image.Image, scan_width: int):
    gray = img.convert("L")
    if scan_width <= 0 or gray.width == scan_width:
        return np.array(gray), 1.0
    new_height = max(2, int(round(gray.height * scan_width / gray.width)))
    resized = gray.resize((scan_width, new_height), Image.Resampling.BILINEAR)
    scale = gray.height / float(new_height)
    return np.array(resized), scale


def _mad_np(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.mean(np.abs(a.astype(np.int16) - b.astype(np.int16))))


def _otsu_binary_np(gray: np.ndarray) -> np.ndarray:
    if gray.size == 0:
        return gray
    _th, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return bw


def _edge_iou(edge_a: np.ndarray, edge_b: np.ndarray) -> float:
    if edge_a.shape != edge_b.shape or edge_a.size == 0:
        return 0.0
    a = edge_a > 0
    b = edge_b > 0
    union = np.logical_or(a, b)
    union_count = int(union.sum())
    if union_count == 0:
        return 0.0
    inter_count = int(np.logical_and(a, b).sum())
    return float(inter_count / union_count)


def _edge_density(edge: np.ndarray) -> float:
    if edge.size == 0:
        return 0.0
    return float((edge > 0).mean())


def _choose_informative_offsets(next_gray: np.ndarray, max_offset: int, patch_h: int, step: int = 16):
    max_offset = max(0, int(max_offset))
    patch_h = max(8, int(patch_h))
    if next_gray.shape[0] <= patch_h:
        return [0]

    max_valid = min(max_offset, next_gray.shape[0] - patch_h - 1)
    if max_valid <= 0:
        return [0]

    offsets = list(range(0, max_valid + 1, max(4, int(step))))
    scored = []
    for off in offsets:
        patch = next_gray[off : off + patch_h, :]
        if patch.size == 0:
            continue
        std = float(patch.std())
        bw = _otsu_binary_np(patch)
        ink_ratio = float((bw < 128).mean())
        edge = cv2.Canny(patch, 50, 150)
        edge_density = _edge_density(edge)

        if std < 5.5:
            continue
        if edge_density < 0.010:
            continue
        if ink_ratio < 0.015 or ink_ratio > 0.985:
            continue

        balance = 1.0 - min(abs(ink_ratio - 0.5) / 0.5, 1.0)
        score = (std * 0.12) + (edge_density * 15.0) + (balance * 1.2)
        scored.append((score, off))

    if not scored:
        return [0]

    scored.sort(reverse=True)
    chosen = []
    min_gap = max(12, patch_h // 4)
    for _score, off in scored:
        if all(abs(off - prev) >= min_gap for prev in chosen):
            chosen.append(off)
        if len(chosen) >= 6:
            break
    return sorted(chosen) if chosen else [0]


def detect_bottom_overlap_cv_otsu(
    prev_page: Image.Image,
    next_page: Image.Image,
    max_overlap_px: int,
    score_threshold: float,
    min_stddev: float,
    scan_width: int,
    scan_step: int,
) -> int:
    prev_gray, prev_scale = _resize_gray_for_overlap(prev_page, scan_width)
    next_gray, next_scale = _resize_gray_for_overlap(next_page, scan_width)
    scale = (prev_scale + next_scale) / 2.0

    h1, w1 = prev_gray.shape
    h2, _w2 = next_gray.shape
    limit = min(
        int(round(max_overlap_px / scale)),
        h1 - 1,
        h2 - 1,
    )
    if limit <= 0:
        return 0

    step = max(1, int(scan_step))
    threshold = max(0.0, float(score_threshold))
    min_std = max(0.0, float(min_stddev))

    x_margin = max(4, int(w1 * 0.08))
    x0, x1 = x_margin, w1 - x_margin
    if (x1 - x0) < 120:
        x0, x1 = 0, w1
    usable_w = x1 - x0
    if usable_w < 60:
        return 0

    win_w = max(64, int(usable_w * 0.36))
    centers = (0.22, 0.5, 0.78)
    windows = []
    for frac in centers:
        cx = x0 + int(usable_w * frac)
        wx0 = max(x0, cx - win_w // 2)
        wx1 = min(x1, wx0 + win_w)
        wx0 = max(x0, wx1 - win_w)
        if (wx1 - wx0) >= 48:
            windows.append((wx0, wx1))
    if not windows:
        windows = [(x0, x1)]

    patch_h = max(42, min(120, limit // 2 if limit > 0 else 42))
    patch_h = min(patch_h, h2 - 2)
    if patch_h < 24:
        return 0

    max_patch_offset = min(max(0, limit - patch_h), 240)
    patch_offsets = _choose_informative_offsets(next_gray[:, x0:x1], max_patch_offset, patch_h, step=max(8, step * 2))

    search_extra = max(24, min(220, limit // 2 + 24))
    y_start = max(0, h1 - (limit + search_extra))
    search_gray = next_gray  # keep naming clear below

    candidates = []
    for off in patch_offsets:
        for wx0, wx1 in windows:
            templ_gray = next_gray[off : off + patch_h, wx0:wx1]
            if templ_gray.size == 0:
                continue

            templ_std = float(templ_gray.std())
            if min_std > 0 and templ_std < min_std:
                continue
            templ_bw = _otsu_binary_np(templ_gray)
            templ_ink_ratio = float((templ_bw < 128).mean())
            if templ_ink_ratio < 0.015 or templ_ink_ratio > 0.985:
                continue
            templ_edge = cv2.Canny(templ_gray, 50, 150)
            templ_edge_density = _edge_density(templ_edge)
            if templ_edge_density < 0.010:
                continue

            src_gray = prev_gray[y_start:h1, wx0:wx1]
            if src_gray.shape[0] <= templ_gray.shape[0]:
                continue
            src_edge = cv2.Canny(src_gray, 50, 150)
            if src_edge.shape[0] < templ_edge.shape[0]:
                continue

            result = cv2.matchTemplate(src_edge, templ_edge, cv2.TM_CCOEFF_NORMED)
            _min_val, max_corr, _min_loc, max_loc = cv2.minMaxLoc(result)
            if max_corr < 0.42:
                continue

            y_match = y_start + int(max_loc[1])
            overlap_scaled = h1 - y_match + off
            if overlap_scaled <= 0 or overlap_scaled > limit:
                continue

            prev_patch = prev_gray[y_match : y_match + patch_h, wx0:wx1]
            if prev_patch.shape != templ_gray.shape:
                continue
            patch_mad = _mad_np(prev_patch, templ_gray)
            prev_bw = _otsu_binary_np(prev_patch)
            bw_match = float((prev_bw == templ_bw).mean())
            prev_edge_patch = cv2.Canny(prev_patch, 50, 150)
            edge_iou = _edge_iou(prev_edge_patch, templ_edge)

            quality = (
                (max_corr * 1.55)
                + (bw_match * 0.95)
                + (edge_iou * 0.75)
                - (patch_mad * 0.028)
                + (templ_edge_density * 0.35)
            )
            candidates.append((int(overlap_scaled), quality, float(max_corr), float(patch_mad), bw_match, edge_iou))

    if len(candidates) < 2:
        return 0

    tol = max(4, step * 2)
    clusters = []
    for cand in sorted(candidates, key=lambda x: x[0]):
        ov = cand[0]
        placed = False
        for cluster in clusters:
            if abs(ov - cluster["center"]) <= tol:
                cluster["items"].append(cand)
                cluster["center"] = int(round(statistics.mean(item[0] for item in cluster["items"])))
                placed = True
                break
        if not placed:
            clusters.append({"center": ov, "items": [cand]})

    best_cluster = None
    best_cluster_score = -1e9
    for cluster in clusters:
        items = cluster["items"]
        count = len(items)
        quality_sum = sum(item[1] for item in items)
        corr_mean = statistics.mean(item[2] for item in items)
        cluster_score = quality_sum + (count * 0.35) + (corr_mean * 0.8)
        if cluster_score > best_cluster_score:
            best_cluster_score = cluster_score
            best_cluster = cluster

    if not best_cluster:
        return 0

    items = best_cluster["items"]
    if len(items) < 2:
        return 0
    overlap_scaled = int(round(statistics.median(item[0] for item in items)))
    corr_mean = statistics.mean(item[2] for item in items)
    patch_mad_mean = statistics.mean(item[3] for item in items)
    bw_match_mean = statistics.mean(item[4] for item in items)
    edge_iou_mean = statistics.mean(item[5] for item in items)

    if corr_mean < 0.52:
        return 0
    if patch_mad_mean > max(11.0, threshold * 2.7):
        return 0
    if bw_match_mean < 0.56 and edge_iou_mean < 0.08:
        return 0

    full_prev = prev_gray[-overlap_scaled:, :]
    full_next = next_gray[:overlap_scaled, :]
    if full_prev.shape != full_next.shape or full_prev.size == 0:
        return 0

    full_std = max(float(full_prev.std()), float(full_next.std()))
    if min_std > 0 and full_std < min_std:
        return 0

    full_mad = _mad_np(full_prev, full_next)
    if full_mad > max(18.0, threshold * 3.6):
        return 0

    full_bw_prev = _otsu_binary_np(full_prev)
    full_bw_next = _otsu_binary_np(full_next)
    full_bw_match = float((full_bw_prev == full_bw_next).mean())
    full_edge_prev = cv2.Canny(full_prev, 50, 150)
    full_edge_next = cv2.Canny(full_next, 50, 150)
    full_edge_iou = _edge_iou(full_edge_prev, full_edge_next)
    full_edge_density = max(_edge_density(full_edge_prev), _edge_density(full_edge_next))

    if full_bw_match < 0.58 and full_edge_iou < 0.10:
        return 0
    if full_edge_density < 0.006 and full_mad > max(8.0, threshold * 1.8):
        return 0

    very_large = overlap_scaled > int(limit * 0.70)
    if very_large and (corr_mean < 0.64 or full_bw_match < 0.62):
        return 0

    overlap_px = int(round(overlap_scaled * scale))
    max_valid = min(prev_page.height, next_page.height) - 1
    if overlap_px > max_valid:
        overlap_px = max_valid
    if overlap_px <= 0:
        return 0
    return overlap_px


def detect_bottom_overlap_cv_match(
    prev_page: Image.Image,
    next_page: Image.Image,
    max_overlap_px: int,
    score_threshold: float,
    min_stddev: float,
    scan_width: int,
    scan_step: int,
) -> int:
    prev_gray, prev_scale = _resize_gray_for_overlap(prev_page, scan_width)
    next_gray, next_scale = _resize_gray_for_overlap(next_page, scan_width)
    scale = (prev_scale + next_scale) / 2.0

    limit = min(
        int(round(max_overlap_px / scale)),
        prev_gray.shape[0] - 1,
        next_gray.shape[0] - 1,
    )
    if limit <= 0:
        return 0

    threshold = max(0.0, float(score_threshold))
    min_std = max(0.0, float(min_stddev))
    step = max(1, int(scan_step))

    h1, w1 = prev_gray.shape
    h2, _ = next_gray.shape
    x_margin = max(2, int(w1 * 0.10))
    usable_x0, usable_x1 = x_margin, w1 - x_margin
    if usable_x1 - usable_x0 < 96:
        usable_x0, usable_x1 = 0, w1

    windows = []
    usable_w = usable_x1 - usable_x0
    if usable_w < 96:
        windows = [(usable_x0, usable_x1)]
    else:
        half = max(24, int(usable_w * 0.18))
        for frac in (0.25, 0.5, 0.75):
            cx = usable_x0 + int(usable_w * frac)
            wx0 = max(usable_x0, cx - half)
            wx1 = min(usable_x1, cx + half)
            if wx1 - wx0 >= 48:
                windows.append((wx0, wx1))
        if not windows:
            windows = [(usable_x0, usable_x1)]

    search_pad = max(8, min(48, step * 8))
    y_start = max(0, h1 - limit - search_pad)
    search_h = h1 - y_start
    if search_h <= 0:
        return 0

    blur_prev = cv2.GaussianBlur(prev_gray, (3, 3), 0)
    blur_next = cv2.GaussianBlur(next_gray, (3, 3), 0)
    prev_edge = cv2.Canny(blur_prev, 50, 150)
    next_edge = cv2.Canny(blur_next, 50, 150)
    patch_h = min(max(48, int(min(limit, h2) * 0.20)), 180, h2 - 1, search_h - 1)
    if patch_h < 24:
        return 0

    max_offset = min(h2 - patch_h, limit + max(40, patch_h // 2))
    off_step = max(4, step * 2)
    offsets = list(range(0, max_offset + 1, off_step))
    if not offsets:
        offsets = [0]

    candidates = []
    for off in offsets:
        window_matches = []
        for wx0, wx1 in windows:
            patch_gray = next_gray[off : off + patch_h, wx0:wx1]
            patch_edge = next_edge[off : off + patch_h, wx0:wx1]
            search_edge = prev_edge[y_start:h1, wx0:wx1]
            if patch_gray.size == 0 or patch_edge.size == 0 or search_edge.size == 0:
                continue
            if search_edge.shape[0] < patch_edge.shape[0]:
                continue

            patch_std = float(patch_gray.std())
            edge_density = float((patch_edge > 0).mean())
            if min_std > 0 and patch_std < min_std:
                continue
            if edge_density < 0.012 and patch_std < max(min_std, 6.5):
                continue

            result = cv2.matchTemplate(search_edge, patch_edge, cv2.TM_CCOEFF_NORMED)
            _, corr_edge, _, max_loc = cv2.minMaxLoc(result)
            y = y_start + max_loc[1]
            overlap_scaled = h1 - y + off
            if overlap_scaled < 1 or overlap_scaled > limit:
                continue

            prev_patch = prev_gray[y : y + patch_h, wx0:wx1]
            if prev_patch.shape != patch_gray.shape:
                continue

            mad = _mad_np(prev_patch, patch_gray)
            window_matches.append((corr_edge, mad, edge_density, overlap_scaled))

        if len(window_matches) < 2:
            continue

        overlap_values = [m[3] for m in window_matches]
        if max(overlap_values) - min(overlap_values) > max(6, step * 4):
            continue

        overlap_scaled = int(round(statistics.median(overlap_values)))
        corr_edge = statistics.mean(m[0] for m in window_matches)
        mad = statistics.mean(m[1] for m in window_matches)
        edge_density = statistics.mean(m[2] for m in window_matches)

        band_h = max(12, min(48, overlap_scaled, patch_h // 2))
        prev_boundary = prev_gray[h1 - band_h : h1, usable_x0:usable_x1]
        next_boundary = next_gray[:band_h, usable_x0:usable_x1]
        if prev_boundary.shape == next_boundary.shape and prev_boundary.size > 0:
            boundary_mad = _mad_np(prev_boundary, next_boundary)
        else:
            boundary_mad = 999.0

        quality = corr_edge - (0.020 * mad) - (0.012 * boundary_mad) + min(edge_density, 0.10)
        candidates.append((quality, corr_edge, mad, boundary_mad, overlap_scaled))

    if not candidates:
        return 0

    candidates.sort(reverse=True, key=lambda item: item[0])
    best_quality, best_corr, best_mad, best_boundary_mad, best_overlap = candidates[0]

    if best_corr < 0.48:
        return 0
    if best_mad > max(10.0, threshold * 2.7):
        return 0
    if best_boundary_mad > max(14.0, threshold * 3.5):
        return 0
    if best_overlap >= int(limit * 0.95) and best_corr < 0.70:
        return 0

    near = [
        c for c in candidates
        if abs(c[4] - best_overlap) <= max(3, step * 3) and c[1] >= best_corr - 0.12
    ]
    if near:
        overlap_scaled = int(round(statistics.median([c[4] for c in near])))
    else:
        overlap_scaled = int(round(best_overlap))

    overlap_px = int(round(overlap_scaled * scale))
    max_valid = min(prev_page.height, next_page.height) - 1
    if overlap_px > max_valid:
        overlap_px = max_valid
    if overlap_px <= 0:
        return 0

    very_large_overlap = overlap_px > int(min(prev_page.height, next_page.height) * 0.35)
    if very_large_overlap and best_corr < 0.80:
        return 0
    return overlap_px


def detect_bottom_overlap_cv_scan(
    prev_page: Image.Image,
    next_page: Image.Image,
    max_overlap_px: int,
    score_threshold: float,
    min_stddev: float,
    scan_width: int,
    scan_step: int,
) -> int:
    prev_gray, prev_scale = _resize_gray_for_overlap(prev_page, scan_width)
    next_gray, next_scale = _resize_gray_for_overlap(next_page, scan_width)
    scale = (prev_scale + next_scale) / 2.0

    limit = min(
        int(round(max_overlap_px / scale)),
        prev_gray.shape[0] - 1,
        next_gray.shape[0] - 1,
    )
    if limit <= 0:
        return 0

    threshold = max(0.0, float(score_threshold))
    min_std = max(0.0, float(min_stddev))
    step = max(1, int(scan_step))

    def eval_mad(px: int):
        prev_band = prev_gray[-px:, :]
        next_band = next_gray[:px, :]
        std_val = max(float(prev_band.std()), float(next_band.std()))
        if min_std > 0 and std_val < min_std:
            return None
        mad = _mad_np(prev_band, next_band)
        return mad, std_val

    sampled = []
    for px in range(1, limit + 1, step):
        out = eval_mad(px)
        if out is None:
            continue
        mad, std_val = out
        sampled.append((px, mad, std_val))

    if not sampled:
        return 0

    # refine around the best coarse candidates
    sampled.sort(key=lambda item: item[1])
    top = sampled[: min(4, len(sampled))]
    refined = {}
    radius = max(2, step * 2)
    for coarse_px, _coarse_mad, _std in top:
        start = max(1, coarse_px - radius)
        end = min(limit, coarse_px + radius)
        for px in range(start, end + 1):
            if px in refined:
                continue
            out = eval_mad(px)
            if out is None:
                continue
            refined[px] = out

    if not refined:
        return 0

    refined_items = sorted((px, vals[0], vals[1]) for px, vals in refined.items())
    best_mad = min(item[1] for item in refined_items)
    if best_mad > threshold:
        return 0

    # keep all near-best candidates then choose the smallest overlap among equivalent matches
    near_eps = max(0.28, threshold * 0.08)
    near = [item for item in refined_items if item[1] <= best_mad + near_eps]
    if not near:
        return 0
    best_scaled = min(item[0] for item in near)

    prev_band = prev_gray[-best_scaled:, :]
    next_band = next_gray[:best_scaled, :]
    bw_prev = _otsu_binary_np(prev_band)
    bw_next = _otsu_binary_np(next_band)
    bw_match = float((bw_prev == bw_next).mean())
    edge_prev = cv2.Canny(prev_band, 50, 150)
    edge_next = cv2.Canny(next_band, 50, 150)
    edge_iou = _edge_iou(edge_prev, edge_next)
    edge_density = max(_edge_density(edge_prev), _edge_density(edge_next))

    if bw_match < 0.56 and edge_iou < 0.08 and best_mad > max(1.2, threshold * 0.45):
        return 0
    if edge_density < 0.005 and best_mad > max(2.5, threshold * 0.65):
        return 0
    if best_scaled > int(limit * 0.70) and (bw_match < 0.62 or edge_iou < 0.09):
        return 0

    overlap_px = int(round(best_scaled * scale))
    max_valid = min(prev_page.height, next_page.height) - 1
    if overlap_px > max_valid:
        overlap_px = max_valid
    return max(0, overlap_px)


def detect_bottom_overlap_cv(
    prev_page: Image.Image,
    next_page: Image.Image,
    max_overlap_px: int,
    score_threshold: float,
    min_stddev: float,
    scan_width: int,
    scan_step: int,
) -> int:
    robust = detect_bottom_overlap_cv_otsu(
        prev_page=prev_page,
        next_page=next_page,
        max_overlap_px=max_overlap_px,
        score_threshold=score_threshold,
        min_stddev=min_stddev,
        scan_width=scan_width,
        scan_step=scan_step,
    )
    if robust > 0:
        return robust

    # Legacy matchTemplate fallback was too permissive on low-texture dark/white zones.
    # Keep a stricter scan-based fallback only.
    return detect_bottom_overlap_cv_scan(
        prev_page=prev_page,
        next_page=next_page,
        max_overlap_px=max_overlap_px,
        score_threshold=score_threshold,
        min_stddev=min_stddev,
        scan_width=scan_width,
        scan_step=scan_step,
    )


def detect_bottom_overlap(
    prev_page: Image.Image,
    next_page: Image.Image,
    max_overlap_px: int,
    score_threshold: float,
    min_stddev: float,
    detector: str = "cv",
    scan_width: int = 256,
    scan_step: int = 2,
) -> int:
    if detector == "cv" and HAS_OPENCV:
        try:
            return detect_bottom_overlap_cv(
                prev_page=prev_page,
                next_page=next_page,
                max_overlap_px=max_overlap_px,
                score_threshold=score_threshold,
                min_stddev=min_stddev,
                scan_width=scan_width,
                scan_step=scan_step,
            )
        except Exception:
            # Fallback robuste: l'ancien detecteur PIL.
            pass

    return detect_bottom_overlap_pil(
        prev_page=prev_page,
        next_page=next_page,
        max_overlap_px=max_overlap_px,
        score_threshold=score_threshold,
        min_stddev=min_stddev,
    )


def infer_constant_overlap_ratio(images, source_widths: Counter | None = None):
    if not images:
        return 0.0

    mixed_x2 = False
    if source_widths:
        keys = sorted(source_widths.keys())
        min_w = min(keys)
        max_w = max(keys)
        total = sum(source_widths.values())
        if (
            len(keys) >= 2
            and max_w / float(max(1, min_w)) >= 1.9
            and source_widths[max_w] / float(max(1, total)) >= 0.15
        ):
            mixed_x2 = True

    heights = [img.height for img in images]
    has_short = sum(1 for h in heights if h <= 1300) >= max(3, int(0.08 * len(heights)))

    # Learned heuristics from Tome 11/12/13 raw vs *_ok:
    # - mixed x2 widths -> low overlap ratio
    # - single-width SushiScan segmented sources (2000/1250) -> stronger overlap ratio
    if mixed_x2:
        return 0.028
    if has_short:
        return 0.111
    return 0.0


def find_reference_ok_folder(input_folder: Path) -> Path | None:
    candidate = input_folder.parent / f"{input_folder.name}_ok"
    if candidate.exists() and candidate.is_dir():
        return candidate
    return None


def get_reference_page_stats(ref_folder: Path):
    paths = sorted(
        [p for p in ref_folder.iterdir() if p.is_file() and p.suffix.lower() in VALID_EXTENSIONS],
        key=lambda p: natural_sort_key(p.name),
    )
    if not paths:
        return 0, 0
    heights = []
    for p in paths:
        with Image.open(p) as img:
            heights.append(img.height)
    if not heights:
        return len(paths), 0
    return len(paths), Counter(heights).most_common(1)[0][0]


def estimate_constant_overlap_ratio_for_target_count(images, target_count: int, page_height_hint: int):
    if not images or target_count <= 0 or page_height_hint <= 0:
        return 0.0

    heights = [img.height for img in images]
    if len(heights) <= 1:
        return 0.0

    def total_for_ratio(r: float) -> int:
        total = heights[0]
        for h in heights[1:]:
            cut = int(round(h * r))
            cut = max(0, min(cut, h - 1))
            total += h - cut
        return total

    target_total = int(target_count * page_height_hint)

    # Coarse-to-fine search (fast and deterministic).
    # Priority:
    # 1) closest page count
    # 2) slight underfill over overfill (avoids pulling next page top)
    # 3) closest total height
    best_ratio = 0.0
    best_key = (10**9, 10**9, 10**9)  # (count_diff, overshoot_flag, total_diff)
    for r in [x / 1000.0 for x in range(0, 301)]:
        total = total_for_ratio(r)
        c = max(1, int(round(total / float(page_height_hint))))
        d_total = abs(total - target_total)
        d_count = abs(c - target_count)
        overshoot_flag = 1 if total > target_total else 0
        key = (d_count, overshoot_flag, d_total)
        if key < best_key:
            best_key = key
            best_ratio = r

    # Fine pass around best coarse value.
    start = max(0.0, best_ratio - 0.001)
    end = min(0.49, best_ratio + 0.001)
    r = start
    while r <= end + 1e-9:
        total = total_for_ratio(r)
        c = max(1, int(round(total / float(page_height_hint))))
        d_total = abs(total - target_total)
        d_count = abs(c - target_count)
        overshoot_flag = 1 if total > target_total else 0
        key = (d_count, overshoot_flag, d_total)
        if key < best_key:
            best_key = key
            best_ratio = r
        r += 0.0001
    return best_ratio


def estimate_constant_overlap_ratio_for_page_grid(
    images,
    page_height_hint: int,
    base_ratio: float,
):
    if not images or page_height_hint <= 0:
        return base_ratio
    if base_ratio <= 0:
        return base_ratio

    heights = [img.height for img in images]
    if len(heights) <= 1:
        return base_ratio

    def total_for_ratio(r: float) -> int:
        total = heights[0]
        for h in heights[1:]:
            cut = int(round(h * r))
            cut = max(0, min(cut, h - 1))
            total += h - cut
        return total

    base_total = total_for_ratio(base_ratio)
    target_count = max(1, int(round(base_total / float(page_height_hint))))
    target_total = target_count * page_height_hint

    # Search around the inferred ratio only, to avoid unstable jumps.
    start = max(0.0, base_ratio - 0.05)
    end = min(0.49, base_ratio + 0.05)
    if end <= start:
        return base_ratio

    best_ratio = base_ratio
    best_diff_total = abs(base_total - target_total)
    best_diff_ratio = 0.0

    r = start
    while r <= end + 1e-9:
        total = total_for_ratio(r)
        d_total = abs(total - target_total)
        d_ratio = abs(r - base_ratio)
        if (d_total < best_diff_total) or (d_total == best_diff_total and d_ratio < best_diff_ratio):
            best_diff_total = d_total
            best_diff_ratio = d_ratio
            best_ratio = r
        r += 0.0001

    return best_ratio


def is_uniform_padding_half(pil_img: Image.Image, white_cutoff: int = 245) -> bool:
    if is_mostly_white(pil_img, ratio_threshold=0.985, white_cutoff=white_cutoff):
        return True
    if is_mostly_dark(pil_img, ratio_threshold=0.985, dark_cutoff=12):
        return True
    if is_low_texture(pil_img, stddev_threshold=3.0):
        return True
    return False


def auto_crop_side_padding(page: Image.Image, args) -> Image.Image:
    if not getattr(args, "auto_crop_padded_pages", True):
        return page
    base_w = int(getattr(args, "base_page_width", 0))
    if base_w <= 0:
        return page
    if page.width < (base_w * 2 - 2):
        return page

    left = page.crop((0, 0, base_w, page.height))
    right = page.crop((page.width - base_w, 0, page.width, page.height))
    left_uniform = is_uniform_padding_half(left, white_cutoff=args.white_cutoff)
    right_uniform = is_uniform_padding_half(right, white_cutoff=args.white_cutoff)
    if right_uniform and not left_uniform:
        return left
    if left_uniform and not right_uniform:
        return right
    return page


def _boundary_match_score(prev_img: Image.Image, next_img: Image.Image, overlap_px: int) -> float:
    """Lower is better."""
    if overlap_px < 0:
        return float("inf")

    probe_h = min(160, prev_img.height, next_img.height)
    if probe_h < 24:
        return float("inf")

    # Ignore outer borders where watermarks/padding can bias the score.
    x_margin = max(4, int(prev_img.width * 0.12))
    x0 = x_margin
    x1 = max(x0 + 32, prev_img.width - x_margin)
    if x1 - x0 < 32:
        x0, x1 = 0, prev_img.width

    prev_arr = np.asarray(prev_img.convert("L"), dtype=np.uint8)[:, x0:x1]
    next_arr = np.asarray(next_img.convert("L"), dtype=np.uint8)[:, x0:x1]
    if prev_arr.size == 0 or next_arr.size == 0:
        return float("inf")

    prev_band = prev_arr[-probe_h:, :]
    if overlap_px <= 0:
        next_band = next_arr[:probe_h, :]
    else:
        if overlap_px < probe_h or overlap_px > next_arr.shape[0]:
            return float("inf")
        next_band = next_arr[overlap_px - probe_h : overlap_px, :]
    if prev_band.shape != next_band.shape or prev_band.size == 0:
        return float("inf")

    mad = _mad_np(prev_band, next_band)
    prev_edge = cv2.Canny(prev_band, 50, 150)
    next_edge = cv2.Canny(next_band, 50, 150)
    edge_iou = _edge_iou(prev_edge, next_edge)
    # Reward edge alignment. (Lower score = better match)
    return float(mad - (15.0 * edge_iou))


def _ncc_score(a: np.ndarray, b: np.ndarray) -> float:
    if a.shape != b.shape or a.size == 0:
        return 0.0
    af = a.astype(np.float32)
    bf = b.astype(np.float32)
    af -= af.mean()
    bf -= bf.mean()
    denom = float(np.sqrt((af * af).sum() * (bf * bf).sum()))
    if denom <= 1e-8:
        return 0.0
    return float((af * bf).sum() / denom)


def _detect_overlap_near_expected(
    prev_img: Image.Image,
    next_img: Image.Image,
    expected_overlap_px: int,
    max_overlap_px: int,
    scan_width: int = 448,
    search_px: int = 140,
    scan_step: int = 2,
    min_stddev: float = 1.0,
):
    """
    Pairwise local refinement around expected overlap.
    Returns (overlap_px, used_refinement).
    """
    if not HAS_OPENCV or expected_overlap_px <= 0:
        return expected_overlap_px, False

    prev_gray, prev_scale = _resize_gray_for_overlap(prev_img, scan_width)
    next_gray, next_scale = _resize_gray_for_overlap(next_img, scan_width)
    scale = (prev_scale + next_scale) / 2.0

    h1, w = prev_gray.shape
    h2, _ = next_gray.shape
    limit = min(
        int(round(max_overlap_px / max(1e-6, scale))),
        h1 - 1,
        h2 - 1,
    )
    if limit <= 0:
        return expected_overlap_px, False

    expected_scaled = int(round(expected_overlap_px / max(1e-6, scale)))
    expected_scaled = max(1, min(expected_scaled, limit))
    search_scaled = max(8, int(round(search_px / max(1e-6, scale))))

    lo = max(8, expected_scaled - search_scaled)
    hi = min(limit, expected_scaled + search_scaled)
    if hi <= lo:
        return expected_overlap_px, False

    x_margin = max(4, int(w * 0.10))
    x0 = x_margin
    x1 = max(x0 + 48, w - x_margin)
    if x1 - x0 < 96:
        x0, x1 = 0, w
    usable_w = x1 - x0
    if usable_w < 48:
        return expected_overlap_px, False

    win_w = max(64, int(usable_w * 0.34))
    windows = []
    for frac in (0.22, 0.50, 0.78):
        cx = x0 + int(usable_w * frac)
        wx0 = max(x0, cx - win_w // 2)
        wx1 = min(x1, wx0 + win_w)
        wx0 = max(x0, wx1 - win_w)
        if (wx1 - wx0) >= 48:
            windows.append((wx0, wx1))
    if not windows:
        windows = [(x0, x1)]

    step = max(1, int(scan_step))
    candidates = []
    expected_q = None
    nearest_q = None
    nearest_dist = None
    for ov in range(lo, hi + 1, step):
        ov_start_prev = h1 - ov
        if ov_start_prev < 0:
            continue

        # Compare several relative bands inside the overlap area.
        segs = []
        h_head = min(72, ov)
        if h_head >= 18:
            segs.append((0, h_head))
        h_mid = min(72, ov)
        mid_start = (ov // 2) - (h_mid // 2)
        mid_end = mid_start + h_mid
        if mid_start >= 0 and mid_end <= ov and h_mid >= 18:
            segs.append((mid_start, mid_end))
        h_tail = min(96, ov)
        if h_tail >= 18:
            segs.append((ov - h_tail, ov))
        if not segs:
            continue

        qvals = []
        for wx0, wx1 in windows:
            seg_scores = []
            for seg_start, seg_end in segs:
                a = prev_gray[ov_start_prev + seg_start : ov_start_prev + seg_end, wx0:wx1]
                b = next_gray[seg_start:seg_end, wx0:wx1]
                if a.shape != b.shape or a.size == 0 or a.shape[0] < 16:
                    continue

                std_a = float(a.std())
                std_b = float(b.std())
                if min(std_a, std_b) < float(min_stddev):
                    continue

                # Normalize segment height for fair comparison across ov values.
                a_cmp = cv2.resize(a, (192, 64), interpolation=cv2.INTER_AREA)
                b_cmp = cv2.resize(b, (192, 64), interpolation=cv2.INTER_AREA)

                mad = _mad_np(a_cmp, b_cmp)
                ncc = _ncc_score(a_cmp, b_cmp)
                edge_a = cv2.Canny(a_cmp, 50, 150)
                edge_b = cv2.Canny(b_cmp, 50, 150)
                edge_iou = _edge_iou(edge_a, edge_b)

                seg_q = (ncc * 2.10) + (edge_iou * 0.95) - (mad * 0.030)
                seg_scores.append(seg_q)

            if not seg_scores:
                continue
            qvals.append(float(np.median(np.asarray(seg_scores, dtype=np.float32))))

        if len(qvals) < 1:
            continue

        q_med = float(np.median(np.asarray(qvals, dtype=np.float32)))
        # Distance prior around expected overlap; keep permissive for first-boundary outliers.
        q_med -= abs(ov - expected_scaled) * 0.004
        dist = abs(ov - expected_scaled)
        if dist == 0:
            expected_q = q_med
        if nearest_dist is None or dist < nearest_dist:
            nearest_dist = dist
            nearest_q = q_med
        candidates.append((q_med, ov))

    if not candidates:
        return expected_overlap_px, False

    candidates.sort(reverse=True, key=lambda x: x[0])
    best_q, best_ov = candidates[0]
    second_q = candidates[1][0] if len(candidates) > 1 else (best_q - 1.0)
    confidence = best_q - second_q
    if expected_q is None:
        expected_q = nearest_q if nearest_q is not None else best_q
    improvement_vs_expected = best_q - expected_q

    # Conservative acceptance to avoid bad jumps.
    if best_q < 0.04:
        return expected_overlap_px, False
    if confidence < 0.015 and abs(best_ov - expected_scaled) > max(6, step * 3):
        return expected_overlap_px, False
    if abs(best_ov - expected_scaled) > int(search_scaled * 0.35) and confidence < 0.18:
        return expected_overlap_px, False
    if abs(best_ov - expected_scaled) > max(3, step) and improvement_vs_expected < 0.08:
        return expected_overlap_px, False

    refined_px = int(round(best_ov * scale))
    refined_px = max(0, min(refined_px, max_overlap_px, min(prev_img.height, next_img.height) - 1))
    return refined_px, True


def _should_disable_first_constant_overlap(
    prev_img: Image.Image,
    next_img: Image.Image,
    overlap_px: int,
) -> bool:
    # Heuristic:
    # - only consider when first source page is visibly shorter after top trim
    # - disable overlap if boundary continuity is clearly better with no cut
    if overlap_px <= 0:
        return False
    if prev_img.height >= int(next_img.height * 0.82):
        return False

    score_zero = _boundary_match_score(prev_img, next_img, overlap_px=0)
    score_hint = _boundary_match_score(prev_img, next_img, overlap_px=overlap_px)
    if not np.isfinite(score_zero) or not np.isfinite(score_hint):
        return False

    # Require a meaningful gain to avoid noisy flips.
    return score_zero + 4.0 < score_hint


def _select_constant_overlap_candidate(
    prev_img: Image.Image,
    next_img: Image.Image,
    expected_overlap_px: int,
    refined_overlap_px: int | None = None,
    boundary_index: int = 1,
    allow_zero_candidate: bool = False,
) -> int:
    """
    Choose overlap per boundary using continuity score on candidate overlaps.
    Lower boundary score is better. A light distance prior keeps values close to expected.
    """
    expected = max(0, int(expected_overlap_px))
    max_valid = min(prev_img.height, next_img.height) - 1
    if max_valid <= 0:
        return 0
    expected = min(expected, max_valid)

    candidates = {expected}
    if refined_overlap_px is not None:
        candidates.add(max(0, min(int(refined_overlap_px), max_valid)))

    # Explore a local band around expected overlap so we don't get stuck on
    # only two candidates (e.g. 0 vs expected) when the real seam is in-between.
    if refined_overlap_px is not None:
        center = max(0, min(int(refined_overlap_px), max_valid))
        search_radius = 48
    else:
        center = expected
        search_radius = 120 if boundary_index <= 20 else 96
    search_radius = min(search_radius, max_valid)
    step = 4 if boundary_index <= 20 else 6
    lo = max(1, center - search_radius)
    hi = min(max_valid, center + search_radius)
    for cand in range(lo, hi + 1, step):
        candidates.add(int(cand))
    candidates.add(lo)
    candidates.add(hi)

    if allow_zero_candidate:
        # Some very early boundaries can be ambiguous after top-banner trim.
        candidates.add(0)

    # Keep a light distance prior to avoid unstable jumps while allowing real
    # local corrections where continuity is clearly better.
    dist_weight = 0.006 if boundary_index <= 8 else 0.010

    best_overlap = expected
    best_obj = float("inf")
    zero_obj = None
    best_nonzero_overlap = expected
    best_nonzero_obj = float("inf")
    for cand in sorted(candidates):
        score = _boundary_match_score(prev_img, next_img, overlap_px=int(cand))
        if not np.isfinite(score):
            continue
        obj = float(score) + (dist_weight * abs(int(cand) - expected))
        if cand == 0:
            zero_obj = obj
        else:
            if obj < best_nonzero_obj:
                best_nonzero_obj = obj
                best_nonzero_overlap = int(cand)
        if obj < best_obj:
            best_obj = obj
            best_overlap = int(cand)

    # Be conservative with 0-overlap: only keep it if it's clearly better than
    # the best non-zero candidate.
    if zero_obj is not None and np.isfinite(best_nonzero_obj):
        # Keep 0-overlap only when it is at least competitive with non-zero.
        # This prevents large false cuts while still allowing real no-overlap
        # boundaries in early pages.
        zero_tolerance = 0.8 if boundary_index <= 6 else 0.3
        if zero_obj <= (best_nonzero_obj + zero_tolerance):
            best_overlap = 0
        else:
            best_overlap = best_nonzero_overlap

    return max(0, min(int(best_overlap), max_valid))


def remove_source_overlaps(images, args, source_widths: Counter | None = None):
    if not images:
        return [], []

    fixed_images = [images[0]]
    overlap_events = []

    overlap_method = (getattr(args, "source_overlap_method", "auto") or "auto").lower()
    overlap_ratio = float(getattr(args, "source_overlap_constant_ratio", -1.0))
    if overlap_method == "auto":
        if overlap_ratio <= 0:
            overlap_ratio = infer_constant_overlap_ratio(images, source_widths)
        if overlap_ratio > 0:
            overlap_method = "constant"
        else:
            overlap_method = "cv"
    args._source_overlap_runtime = overlap_method
    args._source_overlap_runtime_ratio = overlap_ratio if overlap_method == "constant" else 0.0

    for idx in range(1, len(images)):
        prev_img = fixed_images[-1]
        current = images[idx]
        overlap_px = 0

        if args.fix_source_overlap:
            # First boundary is special because the first source page is usually top-trimmed
            # (banner removed). Overlap estimation there can easily over-cut and shift all pages.
            if idx == 1 and bool(getattr(args, "skip_first_source_overlap", True)):
                fixed_images.append(current)
                continue

            if overlap_method == "constant":
                expected_overlap_px = int(round(current.height * overlap_ratio))
                overlap_px = expected_overlap_px
                refined_candidate = None
                if overlap_px > 0:
                    refine_enabled = bool(getattr(args, "source_overlap_local_refine", True))
                    if refine_enabled:
                        local_search_px = max(80, int(getattr(args, "source_overlap_local_search_px", 140)))
                        if idx == 1:
                            local_search_px = max(local_search_px, 360)
                        refined_px, used_refine = _detect_overlap_near_expected(
                            prev_img=prev_img,
                            next_img=current,
                            expected_overlap_px=expected_overlap_px,
                            max_overlap_px=args.max_source_overlap_px,
                            scan_width=max(128, int(getattr(args, "overlap_scan_width", 384))),
                            search_px=local_search_px,
                            scan_step=max(1, int(getattr(args, "overlap_scan_step", 4))),
                            min_stddev=max(0.8, float(getattr(args, "source_overlap_min_std", 1.5))),
                        )
                        if used_refine:
                            # Allow wider local corrections; some SushiScan boundaries
                            # need >50px adjustments to avoid visible page seams.
                            max_delta_px = int(
                                getattr(
                                    args,
                                    "source_overlap_local_max_delta_px",
                                    220 if idx == 1 else 140,
                                )
                            )
                            if abs(int(refined_px) - int(expected_overlap_px)) <= max_delta_px:
                                refined_candidate = int(refined_px)
                overlap_px = _select_constant_overlap_candidate(
                    prev_img=prev_img,
                    next_img=current,
                    expected_overlap_px=expected_overlap_px,
                    refined_overlap_px=refined_candidate,
                    boundary_index=idx,
                    allow_zero_candidate=(idx <= 4),
                )
                if idx == 1 and _should_disable_first_constant_overlap(prev_img, current, overlap_px):
                    overlap_px = 0
            else:
                skip_due_to_uniform = False
                if args.source_overlap_skip_uniform:
                    probe_h = min(220, prev_img.height, current.height)
                    if probe_h > 0:
                        prev_probe = prev_img.crop((0, prev_img.height - probe_h, prev_img.width, prev_img.height))
                        next_probe = current.crop((0, 0, current.width, probe_h))
                        if is_mostly_white(
                            prev_probe,
                            ratio_threshold=args.source_overlap_uniform_ratio_threshold,
                            white_cutoff=args.white_cutoff,
                        ) or is_mostly_white(
                            next_probe,
                            ratio_threshold=args.source_overlap_uniform_ratio_threshold,
                            white_cutoff=args.white_cutoff,
                        ):
                            skip_due_to_uniform = True
                        if is_mostly_dark(
                            prev_probe,
                            ratio_threshold=args.source_overlap_uniform_ratio_threshold,
                        ) or is_mostly_dark(
                            next_probe,
                            ratio_threshold=args.source_overlap_uniform_ratio_threshold,
                        ):
                            skip_due_to_uniform = True
                        if is_low_texture(prev_probe) or is_low_texture(next_probe):
                            skip_due_to_uniform = True

                if not skip_due_to_uniform:
                    overlap_px = detect_bottom_overlap(
                        prev_img,
                        current,
                        max_overlap_px=args.max_source_overlap_px,
                        score_threshold=args.source_overlap_threshold,
                        min_stddev=args.source_overlap_min_std,
                        detector=args.overlap_detector,
                        scan_width=args.overlap_scan_width,
                        scan_step=args.overlap_scan_step,
                    )

            max_ratio_px = int(min(prev_img.height, current.height) * args.max_source_overlap_ratio)
            if max_ratio_px > 0:
                overlap_px = min(overlap_px, max_ratio_px)

        if overlap_px > 0 and current.height > overlap_px:
            current = current.crop((0, overlap_px, current.width, current.height))
            overlap_events.append((idx, overlap_px))

        fixed_images.append(current)

    return fixed_images, overlap_events


def _smart_row_scores(big_img: Image.Image, scan_width: int = 112, smooth_window: int = 9):
    """
    Transition-driven row score for page split detection.
    Higher score means a stronger page-change candidate.
    """
    if np is None:
        return None

    gray = big_img.convert("L")
    h = gray.height
    w = gray.width
    if h <= 4 or w <= 8:
        return None

    target_w = max(48, min(int(scan_width), w))
    if target_w != w:
        gray = gray.resize((target_w, h), Image.Resampling.BILINEAR)

    arr_u8 = np.asarray(gray, dtype=np.uint8)
    if arr_u8.ndim != 2 or arr_u8.shape[0] <= 4 or arr_u8.shape[1] <= 8:
        return None

    arr = arr_u8.astype(np.float32)
    hh, ww = arr.shape

    # Row jump around each possible cut y (between y-1 and y).
    row_jump = np.zeros(hh, dtype=np.float32)
    row_jump[1:] = np.abs(arr[1:] - arr[:-1]).mean(axis=1)

    # Edge jump helps when page switches occur on high-contrast lines.
    if cv2 is not None:
        edge = cv2.Canny(arr_u8, 40, 120).astype(np.float32) / 255.0
        _, bw = cv2.threshold(arr_u8, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    else:
        edge = np.abs(np.diff(arr, axis=1))
        pad = np.zeros((hh, 1), dtype=np.float32)
        edge = np.concatenate([edge, pad], axis=1)
        thr = int(np.mean(arr_u8))
        bw = (arr_u8 >= thr).astype(np.uint8) * 255

    edge_jump = np.zeros(hh, dtype=np.float32)
    edge_jump[1:] = np.abs(edge[1:] - edge[:-1]).mean(axis=1)

    # Compare neighborhoods above/below each cut row (transition strength).
    band = max(16, min(96, hh // 40))
    y_valid = np.arange(band, hh - band, dtype=np.int32)
    if y_valid.size == 0:
        return row_jump.astype(np.float32, copy=False)

    cs = np.vstack([np.zeros((1, ww), dtype=np.float32), np.cumsum(arr, axis=0, dtype=np.float32)])
    top_mean = (cs[y_valid] - cs[y_valid - band]) / float(band)
    bot_mean = (cs[y_valid + band] - cs[y_valid]) / float(band)
    cross_diff = np.abs(top_mean - bot_mean).mean(axis=1).astype(np.float32)

    bw_f = (bw >= 128).astype(np.float32)
    cs_bw = np.vstack([np.zeros((1, ww), dtype=np.float32), np.cumsum(bw_f, axis=0, dtype=np.float32)])
    top_white = (cs_bw[y_valid] - cs_bw[y_valid - band]) / float(band)
    bot_white = (cs_bw[y_valid + band] - cs_bw[y_valid]) / float(band)
    white_diff = np.abs(top_white - bot_white).mean(axis=1).astype(np.float32)

    # Texture confidence: down-weight boundaries fully inside flat zones.
    row_std = arr.std(axis=1).astype(np.float32)
    kernel = np.ones(band, dtype=np.float32) / float(band)
    std_smooth = np.convolve(row_std, kernel, mode="same")
    tex_conf = np.minimum(std_smooth[y_valid - 1], std_smooth[y_valid]).astype(np.float32)

    def _norm(v: np.ndarray) -> np.ndarray:
        v = v.astype(np.float32, copy=False)
        if v.size == 0:
            return v
        p10 = float(np.percentile(v, 10))
        p90 = float(np.percentile(v, 90))
        if p90 <= p10 + 1e-6:
            return np.zeros_like(v, dtype=np.float32)
        return np.clip((v - p10) / (p90 - p10), 0.0, 1.0)

    n_cross = _norm(cross_diff)
    n_jump = _norm(row_jump[y_valid])
    n_white = _norm(white_diff)
    n_edge = _norm(edge_jump[y_valid])
    n_tex = _norm(tex_conf)

    local_score = (
        (n_cross * 0.50)
        + (n_jump * 0.22)
        + (n_white * 0.18)
        + (n_edge * 0.10)
    )
    local_score *= (0.55 + (0.45 * n_tex))

    score = np.zeros(hh, dtype=np.float32)
    score[y_valid] = local_score.astype(np.float32, copy=False)

    win = max(1, int(smooth_window))
    if win > 1:
        kernel = np.ones(win, dtype=np.float32) / float(win)
        score = np.convolve(score, kernel, mode="same")
    return score.astype(np.float32, copy=False)


def _build_page_boundaries_smart(total_height: int, args, big_img: Image.Image):
    if total_height <= 0 or big_img is None or np is None:
        return []

    rough_h = max(1, int(args.page_height))
    search_px = max(40, int(getattr(args, "smart_search_px", 320)))
    scan_w = max(32, int(getattr(args, "smart_scan_width", 96)))
    min_ratio = float(getattr(args, "smart_min_ratio", 0.60))
    max_ratio = float(getattr(args, "smart_max_ratio", 1.40))
    dist_weight = float(getattr(args, "smart_distance_weight", 0.03))
    topk = max(1, int(getattr(args, "smart_topk", 12)))

    if min_ratio <= 0:
        min_ratio = 0.90
    if max_ratio <= min_ratio:
        max_ratio = min_ratio + 0.08

    page_count = max(1, int(round(total_height / float(rough_h))))
    if page_count <= 1:
        return [(0, total_height)]

    avg_h = total_height / float(page_count)
    min_h = max(24, int(round(avg_h * min_ratio)))
    max_h = max(min_h + 24, int(round(avg_h * max_ratio)))

    scores = _smart_row_scores(big_img, scan_width=scan_w, smooth_window=9)
    if scores is None or len(scores) != total_height:
        return []

    cuts = []
    prev_cut = 0
    for page_idx in range(1, page_count):
        rough = int(round(page_idx * avg_h))
        remaining_pages = page_count - page_idx

        lo = max(prev_cut + min_h, rough - search_px)
        hi = min(total_height - (remaining_pages * min_h), rough + search_px)
        hard_lo = prev_cut + min_h
        hard_hi = min(total_height - remaining_pages, prev_cut + max_h)
        if hi < lo:
            lo = max(hard_lo, rough - search_px)
            hi = min(total_height - remaining_pages, rough + search_px)
        if hi < lo:
            cut = min(total_height - remaining_pages, max(hard_lo, rough))
            cut = min(hard_hi, cut)
            cuts.append(cut)
            prev_cut = cut
            continue

        local = scores[lo : hi + 1]
        if local.size == 0:
            cut = min(total_height - remaining_pages, max(hard_lo, rough))
            cut = min(hard_hi, cut)
            cuts.append(cut)
            prev_cut = cut
            continue

        # Transition mode: highest score is best candidate.
        order = np.argsort(local)[::-1]
        chosen = lo + int(order[0])
        best_obj = -float("inf")
        for idx in order[:topk]:
            row = lo + int(idx)
            seg_h = row - prev_cut
            if seg_h < min_h or seg_h > max_h:
                continue
            dist = abs(row - rough)
            obj = float(local[idx]) - (dist_weight * float(dist))
            if obj > best_obj:
                best_obj = obj
                chosen = row

        cut = int(chosen)
        cut = max(hard_lo, cut)
        cut = min(hard_hi, cut)
        cuts.append(cut)
        prev_cut = cut

    boundaries = []
    y0 = 0
    for cut in cuts:
        boundaries.append((y0, int(cut)))
        y0 = int(cut)
    boundaries.append((y0, total_height))
    return boundaries


def build_page_boundaries(total_height: int, args, big_img: Image.Image | None = None):
    if total_height <= 0:
        return []

    split_mode = getattr(args, "split_mode", "equal")
    if split_mode == "smart":
        smart_boundaries = _build_page_boundaries_smart(total_height, args, big_img)
        if smart_boundaries:
            return smart_boundaries
        # Fallback if smart mode cannot run (missing numpy or invalid image).
        split_mode = "equal"

    if split_mode == "equal":
        hint = max(1, int(args.page_height))
        page_count = max(1, int(round(total_height / float(hint))))
        base_h = total_height // page_count
        remainder = total_height % page_count
        y = 0
        boundaries = []
        # Keep early pages stable: put +1px remainder on tail pages instead of
        # the first pages (avoids visible bleed on cover/page 1).
        extra_from = page_count - remainder
        for idx in range(page_count):
            h = base_h + (1 if idx >= extra_from else 0)
            boundaries.append((y, y + h))
            y += h
        return boundaries

    y = 0
    boundaries = []
    carry_px = max(0, int(getattr(args, "carry_next_top_px", 0)))
    first_page = True
    while y < total_height:
        page_h = args.page_height + (carry_px if first_page else 0)
        if page_h <= 0:
            break
        bottom = min(y + page_h, total_height)
        boundaries.append((y, bottom))
        y = bottom
        first_page = False
    return boundaries


def iter_pages_from_strip(big_img: Image.Image, args):
    boundaries = build_page_boundaries(big_img.height, args, big_img=big_img)
    for y0, y1 in boundaries:
        page = big_img.crop((0, y0, big_img.width, y1))
        page = auto_crop_side_padding(page, args)

        if args.page_bottom_trim > 0 and page.height > args.page_bottom_trim:
            page = page.crop((0, 0, page.width, page.height - args.page_bottom_trim))

        if page.height <= 0:
            continue

        if args.skip_mostly_white_pages and is_mostly_white(page, args.white_ratio_threshold, args.white_cutoff):
            continue

        yield page


def save_pages_from_strip(big_img: Image.Image, output_folder: Path, args):
    output_folder.mkdir(parents=True, exist_ok=True)
    saved_paths = []
    overlap_events = []

    page_iter = iter_pages_from_strip(big_img, args)
    current_page = next(page_iter, None)
    if current_page is None:
        return saved_paths, overlap_events

    page_idx = 1
    while True:
        next_page = next(page_iter, None)
        overlap_px = 0
        if next_page is not None and args.fix_bottom_overlap:
            skip_due_to_white = False
            if args.overlap_fix_skip_white:
                probe_h = min(220, current_page.height, next_page.height)
                if probe_h > 0:
                    prev_probe = current_page.crop(
                        (0, current_page.height - probe_h, current_page.width, current_page.height)
                    )
                    next_probe = next_page.crop((0, 0, next_page.width, probe_h))
                    if is_mostly_white(
                        prev_probe,
                        ratio_threshold=args.overlap_fix_white_ratio_threshold,
                        white_cutoff=args.white_cutoff,
                    ) or is_mostly_white(
                        next_probe,
                        ratio_threshold=args.overlap_fix_white_ratio_threshold,
                        white_cutoff=args.white_cutoff,
                    ):
                        skip_due_to_white = True
                    if is_mostly_dark(
                        prev_probe,
                        ratio_threshold=args.overlap_fix_white_ratio_threshold,
                    ) or is_mostly_dark(
                        next_probe,
                        ratio_threshold=args.overlap_fix_white_ratio_threshold,
                    ):
                        skip_due_to_white = True
                    if is_low_texture(prev_probe) or is_low_texture(next_probe):
                        skip_due_to_white = True

            if not skip_due_to_white:
                overlap_px = detect_bottom_overlap(
                    current_page,
                    next_page,
                    max_overlap_px=args.max_overlap_fix_px,
                    score_threshold=args.overlap_fix_threshold,
                    min_stddev=args.overlap_fix_min_std,
                    detector=args.overlap_detector,
                    scan_width=args.overlap_scan_width,
                    scan_step=args.overlap_scan_step,
                )
            if overlap_px > 0 and current_page.height > overlap_px:
                current_page = current_page.crop((0, 0, current_page.width, current_page.height - overlap_px))
                overlap_events.append((page_idx, overlap_px))

        out_path = output_folder / f"page_{page_idx:03d}.jpg"
        current_page.save(out_path, "JPEG", quality=args.jpeg_quality)
        saved_paths.append(out_path)

        if next_page is None:
            break

        current_page = next_page
        page_idx += 1

    return saved_paths, overlap_events


def create_cbz(output_folder: Path, page_paths, cbz_name: str):
    cbz_path = output_folder / cbz_name
    with ZipFile(cbz_path, "w") as cbz:
        for page_path in page_paths:
            cbz.write(page_path, arcname=page_path.name)
    return cbz_path


def delete_files(paths, verbose: bool = False, label: str = "files"):
    deleted = 0
    for path in paths:
        try:
            path.unlink()
            deleted += 1
        except FileNotFoundError:
            continue
        except OSError as exc:
            print(f"Warning: cannot delete {path}: {exc}")
    if verbose and deleted > 0:
        print(f"Deleted {deleted} {label}.")
    return deleted


def prompt_text(label: str, default: str = "", allow_empty: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        raw = input(f"{label}{suffix}: ").strip()
        if raw:
            return raw
        if default:
            return default
        if allow_empty:
            return ""
        print("Value required.")


def prompt_int(label: str, default: int, min_value=None, max_value=None) -> int:
    while True:
        raw = input(f"{label} [{default}]: ").strip()
        if not raw:
            value = default
        else:
            try:
                value = int(raw)
            except ValueError:
                print("Please enter an integer.")
                continue
        if min_value is not None and value < min_value:
            print(f"Value must be >= {min_value}.")
            continue
        if max_value is not None and value > max_value:
            print(f"Value must be <= {max_value}.")
            continue
        return value


def prompt_float(label: str, default: float, min_value=None, max_value=None) -> float:
    while True:
        raw = input(f"{label} [{default}]: ").strip()
        if not raw:
            value = default
        else:
            try:
                value = float(raw)
            except ValueError:
                print("Please enter a number.")
                continue
        if min_value is not None and value < min_value:
            print(f"Value must be >= {min_value}.")
            continue
        if max_value is not None and value > max_value:
            print(f"Value must be <= {max_value}.")
            continue
        return value


def prompt_yes_no(label: str, default: bool) -> bool:
    default_char = "Y/n" if default else "y/N"
    while True:
        raw = input(f"{label} [{default_char}]: ").strip().lower()
        if not raw:
            return default
        if raw in {"y", "yes", "o", "oui"}:
            return True
        if raw in {"n", "no", "non"}:
            return False
        print("Please answer yes or no.")


def prompt_choice(label: str, choices, default: str) -> str:
    values = [str(c).strip() for c in choices if str(c).strip()]
    if not values:
        raise ValueError("choices cannot be empty")
    if default not in values:
        default = values[0]
    joined = "/".join(values)
    while True:
        raw = input(f"{label} [{joined}] ({default}): ").strip().lower()
        if not raw:
            return default
        if raw in values:
            return raw
        print(f"Please choose one of: {joined}.")


def prompt_mode(default_mode: str) -> str:
    mode_to_choice = {"images": "1", "cbz": "2", "both": "3"}
    choice_to_mode = {"1": "images", "2": "cbz", "3": "both"}
    default_choice = mode_to_choice.get(default_mode, "3")
    while True:
        print("Output mode: 1) images  2) cbz  3) both")
        raw = input(f"Choose mode [{default_choice}]: ").strip()
        choice = raw or default_choice
        if choice in choice_to_mode:
            return choice_to_mode[choice]
        print("Please choose 1, 2 or 3.")


def resolve_output_mode(args) -> str:
    if args.mode:
        return args.mode
    if args.cbz:
        return "both"
    return "images"


def configure_interactive(args):
    print("Interactive mode enabled.")
    source_default = args.input_folder or str(Path.cwd())

    while True:
        source_raw = prompt_text("Source folder", source_default)
        source_path = Path(source_raw).expanduser()
        if source_path.exists() and source_path.is_dir():
            break
        print("Source folder is invalid.")

    source_path = source_path.resolve()
    args.input_folder = str(source_path)

    default_output = build_default_output_folder(source_path)
    output_default = Path(args.output_folder).expanduser() if args.output_folder else default_output
    output_raw = prompt_text("Output folder", str(output_default))
    args.output_folder = str(Path(output_raw).expanduser().resolve())

    args.page_height = prompt_int("Page height (0 = auto)", args.page_height, min_value=0)
    args.split_mode = prompt_choice("Split mode", ("fixed", "equal", "smart"), args.split_mode)
    args.carry_next_top_px = prompt_int(
        "Carry top pixels from page N+1 to page N (fixed shift)",
        args.carry_next_top_px,
        min_value=0,
        max_value=500,
    )
    if args.split_mode == "smart":
        args.smart_search_px = prompt_int(
            "Smart split search radius (px)",
            args.smart_search_px,
            min_value=16,
            max_value=2000,
        )
        args.smart_scan_width = prompt_int(
            "Smart split scan width (px)",
            args.smart_scan_width,
            min_value=16,
            max_value=2048,
        )
        args.smart_min_ratio = prompt_float(
            "Smart split minimum page-height ratio",
            args.smart_min_ratio,
            min_value=0.1,
            max_value=2.0,
        )
        args.smart_max_ratio = prompt_float(
            "Smart split maximum page-height ratio",
            args.smart_max_ratio,
            min_value=args.smart_min_ratio + 0.01,
            max_value=3.0,
        )
    args.width_mode = prompt_choice("Width mode", ("auto", "max", "min", "mode"), args.width_mode)
    args.auto_crop_padded_pages = prompt_yes_no("Auto-crop padded half pages", args.auto_crop_padded_pages)
    args.auto_banner_detect = prompt_yes_no("Auto-detect top/bottom orange banners", args.auto_banner_detect)
    args.trim_first_top = prompt_int("Trim top of first image", args.trim_first_top, min_value=0)
    args.trim_last_bottom = prompt_int("Trim bottom of last image", args.trim_last_bottom, min_value=0)
    args.page_bottom_trim = prompt_int("Trim bottom of each output page", args.page_bottom_trim, min_value=0)
    args.jpeg_quality = prompt_int("JPEG quality", args.jpeg_quality, min_value=1, max_value=100)

    mode = prompt_mode(resolve_output_mode(args))
    args.mode = mode
    args.cbz = mode in {"cbz", "both"}

    args.verbose = prompt_yes_no("Verbose logs", args.verbose)
    args.save_strip = prompt_yes_no("Save full concatenated strip (_strip.jpg/.png)", args.save_strip)

    args.overlap_detector = prompt_choice("Overlap detector", ("cv", "pil"), args.overlap_detector)
    args.overlap_scan_width = prompt_int("Overlap scan width (px)", args.overlap_scan_width, min_value=64, max_value=2048)
    args.overlap_scan_step = prompt_int("Overlap scan step (px)", args.overlap_scan_step, min_value=1, max_value=64)

    args.fix_source_overlap = prompt_yes_no("Auto-fix overlap between source images", args.fix_source_overlap)
    if args.fix_source_overlap:
        args.source_overlap_method = prompt_choice(
            "Source overlap method",
            ("auto", "cv", "constant"),
            args.source_overlap_method,
        )
        args.source_overlap_constant_ratio = prompt_float(
            "Source overlap constant ratio (<=0 means auto in method=auto)",
            args.source_overlap_constant_ratio,
            min_value=-1.0,
            max_value=0.49,
        )
        args.max_source_overlap_px = prompt_int(
            "Max overlap to remove between source images",
            args.max_source_overlap_px,
            min_value=1,
            max_value=2000,
        )
        args.max_source_overlap_ratio = prompt_float(
            "Max overlap ratio per source boundary (0.1..0.9)",
            args.max_source_overlap_ratio,
            min_value=0.1,
            max_value=0.9,
        )
        args.skip_first_source_overlap = prompt_yes_no(
            "Skip overlap removal on first source boundary (001->002)",
            getattr(args, "skip_first_source_overlap", True),
        )
        args.source_overlap_skip_uniform = prompt_yes_no(
            "Skip source overlap detection on mostly white/dark boundaries",
            args.source_overlap_skip_uniform,
        )
        if args.source_overlap_skip_uniform:
            args.source_overlap_uniform_ratio_threshold = prompt_float(
                "Mostly-white/dark ratio threshold for source overlap skip",
                args.source_overlap_uniform_ratio_threshold,
                min_value=0.8,
                max_value=1.0,
            )
        args.source_overlap_threshold = prompt_float(
            "Source overlap match threshold",
            args.source_overlap_threshold,
            min_value=0.0,
            max_value=30.0,
        )
        args.source_overlap_min_std = prompt_float(
            "Source overlap min texture stddev",
            args.source_overlap_min_std,
            min_value=0.0,
            max_value=100.0,
        )

    args.fix_bottom_overlap = prompt_yes_no("Auto-fix overlap between output pages", args.fix_bottom_overlap)
    if args.fix_bottom_overlap:
        args.max_overlap_fix_px = prompt_int(
            "Max overlap to remove at output page boundary",
            args.max_overlap_fix_px,
            min_value=1,
            max_value=2000,
        )
        args.overlap_fix_threshold = prompt_float(
            "Output boundary match threshold",
            args.overlap_fix_threshold,
            min_value=0.0,
            max_value=30.0,
        )
        args.overlap_fix_min_std = prompt_float(
            "Output boundary min texture stddev",
            args.overlap_fix_min_std,
            min_value=0.0,
            max_value=100.0,
        )
        args.overlap_fix_skip_white = prompt_yes_no(
            "Skip output overlap fix on mostly white boundaries",
            args.overlap_fix_skip_white,
        )
        if args.overlap_fix_skip_white:
            args.overlap_fix_white_ratio_threshold = prompt_float(
                "Mostly-white ratio threshold for output overlap skip",
                args.overlap_fix_white_ratio_threshold,
                min_value=0.8,
                max_value=1.0,
            )

    args.skip_mostly_white_pages = prompt_yes_no("Skip mostly white pages", args.skip_mostly_white_pages)

    if mode in {"cbz", "both"}:
        cbz_default = args.cbz_name
        args.cbz_name = prompt_text("CBZ filename (empty = source folder name)", cbz_default, allow_empty=True)
        delete_pages_default = args.delete_pages_after_cbz
        if delete_pages_default is None:
            delete_pages_default = mode == "cbz"
        args.delete_pages_after_cbz = prompt_yes_no("Delete cut image files after CBZ creation", delete_pages_default)
        args.delete_source_after_cbz = prompt_yes_no(
            "Delete source image files after CBZ creation",
            args.delete_source_after_cbz,
        )
    else:
        args.cbz_name = ""
        args.delete_pages_after_cbz = False
        args.delete_source_after_cbz = False

    return args


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Rebuild pages by stacking all source images vertically, after trimming "
            "the first-top and last-bottom banners."
        )
    )
    parser.add_argument("input_folder", nargs="?", default=None, help="Input folder with JPG/WEBP/PNG images.")
    parser.add_argument("--interactive", action="store_true", help="Launch interactive prompts.")
    parser.add_argument("--output-folder", default="", help="Output folder. Default: <source>/<source_name>_cut.")
    parser.add_argument(
        "--split-mode",
        choices=("fixed", "equal", "smart"),
        default="equal",
        help="Page split strategy: fixed height, equal-sized parts, or smart local boundary search.",
    )
    parser.add_argument(
        "--width-mode",
        choices=("auto", "max", "min", "mode"),
        default="auto",
        help="Target width strategy for mixed-width sources (auto=smart: x2-mixed=>max, else min).",
    )

    parser.add_argument("--trim-first-top", type=int, default=786, help="Pixels removed from the top of the first image.")
    parser.add_argument("--trim-last-bottom", type=int, default=786, help="Pixels removed from the bottom of the last image.")
    parser.add_argument(
        "--auto-banner-detect",
        dest="auto_banner_detect",
        action="store_true",
        help="Auto-adjust first-top and last-bottom trims by detecting orange SushiScan banners.",
    )
    parser.add_argument(
        "--no-auto-banner-detect",
        dest="auto_banner_detect",
        action="store_false",
        help="Disable auto banner trim adjustment.",
    )
    parser.set_defaults(auto_banner_detect=True)

    parser.add_argument(
        "--page-height",
        type=int,
        default=0,
        help="Output page height in px. Use 0 to auto-detect from source images.",
    )
    parser.add_argument(
        "--carry-next-top-px",
        type=int,
        default=0,
        help=(
            "Fixed boundary shift: moves N px from top of page N+1 to bottom of page N "
            "(applies by extending first page by N px)."
        ),
    )
    parser.add_argument(
        "--smart-search-px",
        type=int,
        default=320,
        help="Smart split: search radius around each rough boundary.",
    )
    parser.add_argument(
        "--smart-scan-width",
        type=int,
        default=96,
        help="Smart split: temporary grayscale width for row scoring (smaller=faster).",
    )
    parser.add_argument(
        "--smart-min-ratio",
        type=float,
        default=0.97,
        help="Smart split: minimum page height ratio relative to --page-height.",
    )
    parser.add_argument(
        "--smart-max-ratio",
        type=float,
        default=1.03,
        help="Smart split: maximum page height ratio relative to --page-height.",
    )
    parser.add_argument(
        "--smart-distance-weight",
        type=float,
        default=0.03,
        help="Smart split: penalty weight for distance to rough boundary.",
    )
    parser.add_argument(
        "--smart-topk",
        type=int,
        default=12,
        help="Smart split: number of lowest-score candidate rows evaluated per boundary.",
    )
    parser.add_argument("--page-bottom-trim", type=int, default=0, help="Pixels removed from the bottom of each output page.")
    parser.add_argument("--jpeg-quality", type=int, default=95, help="JPEG quality for output pages.")

    parser.add_argument("--skip-mostly-white-pages", action="store_true", help="Skip pages considered mostly white.")
    parser.add_argument("--white-ratio-threshold", type=float, default=0.98, help="Mostly-white threshold ratio.")
    parser.add_argument("--white-cutoff", type=int, default=245, help="Luma cutoff for white pixels.")

    parser.add_argument(
        "--fix-bottom-overlap",
        dest="fix_bottom_overlap",
        action="store_true",
        help="Detect and remove small duplicated bands (pixels parasites) at page boundaries.",
    )
    parser.add_argument(
        "--no-fix-bottom-overlap",
        dest="fix_bottom_overlap",
        action="store_false",
        help="Disable bottom overlap cleanup.",
    )
    parser.set_defaults(fix_bottom_overlap=False)
    parser.add_argument("--max-overlap-fix-px", type=int, default=24, help="Max pixels to remove per output page boundary.")
    parser.add_argument("--overlap-fix-threshold", type=float, default=4.0, help="Max grayscale MAD to match output page boundary overlap.")
    parser.add_argument(
        "--overlap-fix-min-std",
        type=float,
        default=2.5,
        help="Min grayscale stddev for output page boundary matching (0 disables texture filtering).",
    )
    parser.add_argument(
        "--overlap-fix-skip-white",
        dest="overlap_fix_skip_white",
        action="store_true",
        help="Skip output-page overlap trimming when boundary is mostly white.",
    )
    parser.add_argument(
        "--no-overlap-fix-skip-white",
        dest="overlap_fix_skip_white",
        action="store_false",
        help="Do not skip overlap trimming on white output-page boundaries.",
    )
    parser.set_defaults(overlap_fix_skip_white=True)
    parser.add_argument(
        "--overlap-fix-white-ratio-threshold",
        type=float,
        default=0.985,
        help="Boundary white ratio threshold used when --overlap-fix-skip-white is enabled.",
    )
    parser.add_argument(
        "--fix-source-overlap",
        dest="fix_source_overlap",
        action="store_true",
        help="Detect and remove duplicated overlap between source images before concatenation.",
    )
    parser.add_argument(
        "--no-fix-source-overlap",
        dest="fix_source_overlap",
        action="store_false",
        help="Disable source-image overlap cleanup.",
    )
    parser.set_defaults(fix_source_overlap=True)
    parser.add_argument(
        "--max-source-overlap-px",
        type=int,
        default=300,
        help="Max pixels to remove at each source-image boundary.",
    )
    parser.add_argument(
        "--max-source-overlap-ratio",
        type=float,
        default=0.4,
        help="Safety cap ratio for source overlap removal relative to source image height.",
    )
    parser.add_argument(
        "--source-overlap-threshold",
        type=float,
        default=5.0,
        help="Max grayscale MAD to match source-image overlap.",
    )
    parser.add_argument(
        "--source-overlap-min-std",
        type=float,
        default=1.5,
        help="Min grayscale stddev for source-image overlap matching (0 disables texture filtering).",
    )
    parser.add_argument(
        "--source-overlap-skip-uniform",
        dest="source_overlap_skip_uniform",
        action="store_true",
        help="Skip source overlap detection when boundary is mostly white or dark.",
    )
    parser.add_argument(
        "--no-source-overlap-skip-uniform",
        dest="source_overlap_skip_uniform",
        action="store_false",
        help="Do not skip source overlap detection on mostly white/dark boundaries.",
    )
    parser.set_defaults(source_overlap_skip_uniform=True)
    parser.add_argument(
        "--source-overlap-uniform-ratio-threshold",
        type=float,
        default=0.985,
        help="White/dark ratio threshold used by source overlap uniform-boundary skip.",
    )
    parser.add_argument(
        "--source-overlap-method",
        choices=("auto", "cv", "constant"),
        default="auto",
        help="Source overlap strategy (auto uses learned constant ratio for SushiScan-like sources).",
    )
    parser.add_argument(
        "--source-overlap-constant-ratio",
        type=float,
        default=-1.0,
        help="Constant overlap ratio applied to each source boundary (e.g. 0.11). <=0 means auto if method=auto.",
    )
    parser.add_argument(
        "--source-overlap-local-max-delta-px",
        type=int,
        default=140,
        help=(
            "Max allowed local refinement delta (in px) versus constant source-overlap estimate. "
            "Increase if seams remain visible."
        ),
    )
    parser.add_argument(
        "--skip-first-source-overlap",
        dest="skip_first_source_overlap",
        action="store_true",
        help="Do not remove overlap on first source boundary (001->002).",
    )
    parser.add_argument(
        "--no-skip-first-source-overlap",
        dest="skip_first_source_overlap",
        action="store_false",
        help="Allow overlap removal on first source boundary.",
    )
    parser.set_defaults(skip_first_source_overlap=True)
    parser.add_argument(
        "--auto-crop-padded-pages",
        dest="auto_crop_padded_pages",
        action="store_true",
        help="Auto-crop padded half when target width uses max mode and one side is uniform.",
    )
    parser.add_argument(
        "--no-auto-crop-padded-pages",
        dest="auto_crop_padded_pages",
        action="store_false",
        help="Disable auto-crop of padded page halves.",
    )
    parser.set_defaults(auto_crop_padded_pages=True)
    parser.add_argument(
        "--overlap-detector",
        choices=("cv", "pil"),
        default="cv" if HAS_OPENCV else "pil",
        help="Overlap detector backend.",
    )
    parser.add_argument(
        "--overlap-scan-width",
        type=int,
        default=384,
        help="Resize width used during overlap detection (OpenCV mode).",
    )
    parser.add_argument(
        "--overlap-scan-step",
        type=int,
        default=4,
        help="Step in pixels for coarse overlap search (OpenCV mode).",
    )

    parser.add_argument("--save-strip", action="store_true", help="Also save the huge concatenated image as _strip.jpg/_strip.png.")
    parser.add_argument("--mode", choices=OUTPUT_MODES, default="", help="Output mode: images, cbz or both.")
    parser.add_argument("--cbz", action="store_true", help="Backward compatible shortcut for mode=both.")
    parser.add_argument("--cbz-name", default="", help="CBZ filename. Default: <input_folder_name>.cbz")

    parser.add_argument(
        "--delete-pages-after-cbz",
        dest="delete_pages_after_cbz",
        action="store_true",
        help="Delete generated page images after CBZ creation.",
    )
    parser.add_argument(
        "--keep-pages-after-cbz",
        dest="delete_pages_after_cbz",
        action="store_false",
        help="Keep generated page images after CBZ creation.",
    )
    parser.set_defaults(delete_pages_after_cbz=None)
    parser.add_argument(
        "--delete-source-after-cbz",
        action="store_true",
        help="Delete source image files after successful CBZ creation.",
    )

    parser.add_argument("--verbose", action="store_true", help="Print detailed processing info.")
    return parser.parse_args()


def main():
    args = parse_args()

    if args.interactive or args.input_folder is None:
        args = configure_interactive(args)

    mode = resolve_output_mode(args)
    if args.delete_pages_after_cbz is None:
        args.delete_pages_after_cbz = mode == "cbz"

    input_folder = Path(args.input_folder).expanduser().resolve()
    if args.output_folder:
        output_folder = Path(args.output_folder).expanduser().resolve()
    else:
        output_folder = build_default_output_folder(input_folder).resolve()

    if not input_folder.exists() or not input_folder.is_dir():
        raise FileNotFoundError(f"Input folder not found: {input_folder}")
    if args.trim_first_top < 0 or args.trim_last_bottom < 0:
        raise ValueError("--trim-first-top and --trim-last-bottom must be >= 0.")
    if args.page_bottom_trim < 0:
        raise ValueError("--page-bottom-trim must be >= 0.")
    if args.page_height < 0:
        raise ValueError("--page-height must be >= 0.")
    if args.carry_next_top_px < 0:
        raise ValueError("--carry-next-top-px must be >= 0.")
    if args.max_overlap_fix_px < 1:
        raise ValueError("--max-overlap-fix-px must be >= 1.")
    if args.max_source_overlap_px < 1:
        raise ValueError("--max-source-overlap-px must be >= 1.")
    if args.max_source_overlap_ratio <= 0 or args.max_source_overlap_ratio >= 1:
        raise ValueError("--max-source-overlap-ratio must be in (0, 1).")
    if args.source_overlap_uniform_ratio_threshold <= 0 or args.source_overlap_uniform_ratio_threshold > 1:
        raise ValueError("--source-overlap-uniform-ratio-threshold must be in (0, 1].")
    if args.source_overlap_constant_ratio > 0 and args.source_overlap_constant_ratio >= 0.5:
        raise ValueError("--source-overlap-constant-ratio must be < 0.5.")
    if args.source_overlap_local_max_delta_px < 0:
        raise ValueError("--source-overlap-local-max-delta-px must be >= 0.")
    if args.overlap_fix_white_ratio_threshold <= 0 or args.overlap_fix_white_ratio_threshold > 1:
        raise ValueError("--overlap-fix-white-ratio-threshold must be in (0, 1].")
    if args.overlap_scan_width < 16:
        raise ValueError("--overlap-scan-width must be >= 16.")
    if args.overlap_scan_step < 1:
        raise ValueError("--overlap-scan-step must be >= 1.")
    if args.smart_search_px < 16:
        raise ValueError("--smart-search-px must be >= 16.")
    if args.smart_scan_width < 16:
        raise ValueError("--smart-scan-width must be >= 16.")
    if args.smart_min_ratio <= 0:
        raise ValueError("--smart-min-ratio must be > 0.")
    if args.smart_max_ratio <= args.smart_min_ratio:
        raise ValueError("--smart-max-ratio must be > --smart-min-ratio.")
    if args.smart_distance_weight < 0:
        raise ValueError("--smart-distance-weight must be >= 0.")
    if args.smart_topk < 1:
        raise ValueError("--smart-topk must be >= 1.")

    image_paths, images = load_images(input_folder)
    source_widths = Counter(img.width for img in images)
    args.base_page_width = min(source_widths) if source_widths else 0
    prepared_images, target_width, applied_trim_first, applied_trim_last, normalize_stats = prepare_images(
        images,
        args.trim_first_top,
        args.trim_last_bottom,
        width_mode=args.width_mode,
        auto_banner_detect=args.auto_banner_detect,
    )
    inferred_page_height_for_tuning = args.page_height if args.page_height > 0 else infer_page_height(prepared_images)

    ref_folder = find_reference_ok_folder(input_folder)
    ref_count = 0
    ref_page_height = 0
    if ref_folder is not None:
        ref_count, ref_page_height = get_reference_page_stats(ref_folder)
        if args.page_height == 0 and ref_page_height > 0:
            args.page_height = ref_page_height
        if (
            args.fix_source_overlap
            and args.source_overlap_method == "auto"
            and args.source_overlap_constant_ratio <= 0
            and ref_count > 0
        ):
            hint_height = args.page_height if args.page_height > 0 else ref_page_height
            if hint_height <= 0:
                hint_height = infer_page_height(prepared_images)
            learned_ratio = estimate_constant_overlap_ratio_for_target_count(
                prepared_images,
                target_count=ref_count,
                page_height_hint=hint_height,
            )
            if learned_ratio > 0:
                args.source_overlap_method = "constant"
                args.source_overlap_constant_ratio = learned_ratio
        args._reference_ok_folder = str(ref_folder)
        args._reference_ok_count = ref_count
        args._reference_ok_page_height = ref_page_height

    # No reference folder: still reduce split-mode=equal drift by snapping
    # overlap ratio to the nearest stable page grid.
    if (
        args.fix_source_overlap
        and args.source_overlap_method == "auto"
        and args.source_overlap_constant_ratio <= 0
    ):
        base_ratio = infer_constant_overlap_ratio(prepared_images, source_widths)
        if base_ratio > 0:
            tuned_ratio = estimate_constant_overlap_ratio_for_page_grid(
                prepared_images,
                page_height_hint=inferred_page_height_for_tuning,
                base_ratio=base_ratio,
            )
            if tuned_ratio > 0:
                args.source_overlap_method = "constant"
                args.source_overlap_constant_ratio = tuned_ratio

    source_overlap_events = []
    if args.fix_source_overlap:
        prepared_images, source_overlap_events = remove_source_overlaps(
            prepared_images,
            args,
            source_widths=source_widths,
        )

    if not prepared_images:
        print("No valid image to process.")
        return

    if args.page_height == 0:
        args.page_height = infer_page_height(prepared_images)
    if args.page_height <= 0:
        raise ValueError("Auto-detected page height is invalid. Set --page-height manually.")

    big_img = concatenate_images(prepared_images, target_width)
    if args.save_strip:
        output_folder.mkdir(parents=True, exist_ok=True)
        strip_as_png = big_img.width > 65000 or big_img.height > 65000
        strip_path = output_folder / ("_strip.png" if strip_as_png else "_strip.jpg")
        if strip_as_png:
            big_img.save(strip_path, "PNG")
        else:
            try:
                big_img.save(strip_path, "JPEG", quality=args.jpeg_quality)
            except OSError:
                strip_path = output_folder / "_strip.png"
                big_img.save(strip_path, "PNG")

    page_paths, overlap_events = save_pages_from_strip(big_img, output_folder, args)
    if not page_paths:
        print("No output page generated.")
        return

    print(f"Pages generated: {len(page_paths)} in {output_folder}")

    if args.verbose:
        print(f"Input images: {len(image_paths)}")
        print(f"Source widths: {dict(sorted(source_widths.items()))}")
        print(f"Width mode: {args.width_mode}")
        print(f"Target width: {target_width}px")
        print(f"Base page width: {args.base_page_width}px")
        print(f"Auto crop padded pages: {args.auto_crop_padded_pages}")
        print(f"Normalize stats: {normalize_stats}")
        print(f"Trim first top: {applied_trim_first}px (requested: {args.trim_first_top}px)")
        print(f"Trim last bottom: {applied_trim_last}px (requested: {args.trim_last_bottom}px)")
        print(f"Auto banner detect: {args.auto_banner_detect}")
        print(f"Page height: {args.page_height}px")
        print(f"Split mode: {args.split_mode}")
        if args.split_mode == "smart":
            print(
                "Smart split params: "
                f"search={args.smart_search_px}px, "
                f"scan_width={args.smart_scan_width}px, "
                f"min_ratio={args.smart_min_ratio:.2f}, "
                f"max_ratio={args.smart_max_ratio:.2f}, "
                f"distance_weight={args.smart_distance_weight:.3f}, "
                f"topk={args.smart_topk}"
            )
        print(f"Carry next-top pixels: {args.carry_next_top_px}px")
        print(f"Big strip size: {big_img.width}x{big_img.height}")
        print(f"Output mode: {mode}")
        if ref_folder is not None and ref_count > 0:
            print(
                f"Reference *_ok: {ref_folder} "
                f"(pages={ref_count}, page_height={ref_page_height})"
            )
        print(
            f"Overlap detector: {args.overlap_detector} "
            f"(scan_width={args.overlap_scan_width}, step={args.overlap_scan_step})"
        )
        if args.fix_source_overlap:
            print(f"Skip first source boundary overlap: {bool(getattr(args, 'skip_first_source_overlap', True))}")
            total_source_px = sum(px for _, px in source_overlap_events)
            overlap_runtime = getattr(args, "_source_overlap_runtime", args.source_overlap_method)
            overlap_ratio = float(getattr(args, "_source_overlap_runtime_ratio", 0.0))
            if overlap_runtime == "constant":
                print(f"Source overlap runtime: constant (ratio={overlap_ratio:.4f})")
            else:
                print(f"Source overlap runtime: {overlap_runtime}")
            print(
                f"Source overlap fix: {len(source_overlap_events)} boundaries, {total_source_px}px removed "
                f"(max_px={args.max_source_overlap_px}, ratio_cap={args.max_source_overlap_ratio:.2f})."
            )
            for source_idx, px in source_overlap_events[:20]:
                print(f"  source_{source_idx:03d} boundary: -{px}px")
            if len(source_overlap_events) > 20:
                print(f"  ... {len(source_overlap_events) - 20} more boundaries")
        if args.fix_bottom_overlap:
            total_px = sum(px for _, px in overlap_events)
            print(f"Bottom artifact fix: {len(overlap_events)} boundaries, {total_px}px removed.")
            for page_idx, px in overlap_events[:20]:
                print(f"  page_{page_idx:03d} -> page_{page_idx+1:03d}: -{px}px")
            if len(overlap_events) > 20:
                print(f"  ... {len(overlap_events) - 20} more boundaries")

    cbz_path = None
    if mode in {"cbz", "both"}:
        cbz_name = args.cbz_name.strip() or f"{input_folder.name}.cbz"
        cbz_path = create_cbz(output_folder, page_paths, cbz_name)
        print(f"CBZ generated: {cbz_path}")

        if args.delete_pages_after_cbz:
            delete_files(page_paths, verbose=args.verbose, label="cut page files")

        if args.delete_source_after_cbz:
            delete_files(image_paths, verbose=args.verbose, label="source image files")

    if mode == "images":
        print("Output contains image files only.")
    elif mode == "cbz":
        if args.delete_pages_after_cbz:
            print("Output contains CBZ only (cut images deleted).")
        else:
            print("Output contains CBZ and cut images.")
    elif mode == "both":
        print("Output contains cut images and CBZ.")


if __name__ == "__main__":
    main()
