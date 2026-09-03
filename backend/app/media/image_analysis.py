"""Image forensics & OCR engine (spec #14, #15, #17, #18, #49, #50).

Pipeline: IMAGE -> preprocessing -> OCR -> text -> (claims handled upstream)

Detectors (all heuristic, all honest about limitations — spec #14 forbids
claiming definitive authenticity from these signals alone):
  * OCR            — pytesseract + OpenCV preprocessing (binarize, denoise, scale)
  * EXIF/metadata  — Pillow: camera, software, timestamps, GPS presence,
                     missing-metadata anomaly (screenshots/social re-encodes)
  * Manipulation   — OpenCV: error-level-style resave-difference, noise
                     inconsistency (local variance map), edge-sharpness
                     disparity, potential resampling hints
  * AI-generation  — pluggable: provider vision signal (sidecar) only when
                     available; heuristics never claim certainty (spec #18)
  * Screenshot UI  — platform-UI consistency cues + OCR text quality (spec #49)
"""
from __future__ import annotations

import hashlib
import io
import json
from dataclasses import dataclass, field
from typing import Any, Optional

from app.core.logging import get_logger

logger = get_logger("prvision.media.image")

try:
    import numpy as np
    import cv2
    HAS_CV = True
except Exception:  # pragma: no cover
    HAS_CV = False

from PIL import Image, ImageOps, ExifTags

Image.MAX_IMAGE_PIXELS = 50_000_000  # decompression-bomb guard


@dataclass
class ImageAnalysisResult:
    sha256: str
    size_bytes: int
    width: int
    height: int
    format: str
    ocr_text: str = ""
    ocr_confidence: Optional[float] = None
    ocr_word_count: int = 0
    exif: dict[str, Any] = field(default_factory=dict)
    metadata_anomalies: list[str] = field(default_factory=list)
    manipulation_signals: list[dict[str, Any]] = field(default_factory=list)
    manipulation_risk: Optional[float] = None
    ai_generation_signal: Optional[float] = None
    ai_signal_confidence: str = "LOW"
    ai_note: Optional[str] = None
    screenshot_hints: list[str] = field(default_factory=list)
    authenticity_note: str = ""
    detectors_run: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sha256": self.sha256, "size_bytes": self.size_bytes,
            "width": self.width, "height": self.height, "format": self.format,
            "ocr_text": self.ocr_text, "ocr_confidence": self.ocr_confidence,
            "ocr_word_count": self.ocr_word_count,
            "exif": self.exif, "metadata_anomalies": self.metadata_anomalies,
            "manipulation_signals": self.manipulation_signals,
            "manipulation_risk": self.manipulation_risk,
            "ai_generation_signal": self.ai_generation_signal,
            "ai_signal_confidence": self.ai_signal_confidence,
            "ai_note": self.ai_note,
            "screenshot_hints": self.screenshot_hints,
            "authenticity_note": self.authenticity_note,
            "detectors_run": self.detectors_run,
        }


def _exif_extract(img: Image.Image) -> dict[str, Any]:
    out: dict[str, Any] = {}
    try:
        raw = img.getexif()
        tagmap = {tag: name for tag, name in ExifTags.TAGS.items()}
        for tag_id, value in raw.items():
            name = tagmap.get(tag_id, str(tag_id))
            if isinstance(value, bytes):
                value = value[:64].hex()
            elif isinstance(value, tuple):
                value = [float(v) if isinstance(v, (int, float)) else str(v) for v in value][:6]
            out[str(name)] = value if isinstance(value, (int, float, str)) else str(value)
    except Exception:
        pass
    return out


def _analyze_metadata(img: Image.Image, fmt: str) -> tuple[dict, list[str]]:
    exif = _exif_extract(img)
    anomalies: list[str] = []
    camera = exif.get("Make") or exif.get("Model")
    software = str(exif.get("Software", "")).lower()
    if not exif:
        anomalies.append("no_exif_metadata (common for screenshots/social re-encodes — not proof of editing)")
    else:
        if not camera:
            anomalies.append("no_camera_identifier (metadata stripped or synthetic origin)")
        if any(s in software for s in ("photoshop", "gimp", "lightroom", "snapseed", "canva", "pixelmator", "affinity")):
            anomalies.append(f"editing_software_tag: {exif.get('Software')}")
        date_orig = str(exif.get("DateTimeOriginal") or exif.get("DateTime") or "")
        if date_orig and software and any(s in software for s in ("photoshop", "gimp")):
            anomalies.append("editing_software_with_capture_date (verify timeline)")
    if fmt in ("PNG",) and "Software" not in exif and not exif:
        anomalies.append("png_without_text_chunks")
    return exif, anomalies


def _resave_difference(img_gray: "np.ndarray") -> float:
    """JPEG-resave difference: tampered regions often change more when the
    image is re-encoded at a fixed quality. Returns mean abs diff (0..255)."""
    ok, buf = cv2.imencode(".jpg", img_gray, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
    if not ok:
        return 0.0
    rec, _ = cv2.imdecode(buf, cv2.IMREAD_GRAYSCALE), None
    if rec is None or rec.shape != img_gray.shape:
        return 0.0
    diff = cv2.absdiff(img_gray, rec)
    return float(np.mean(diff))


def _noise_inconsistency(img_gray: "np.ndarray", blocks: int = 12) -> dict[str, Any]:
    """Local noise-variance dispersion — spliced regions can carry mismatched
    sensor noise. Returns dispersion stats; HIGH dispersion = weak signal."""
    h, w = img_gray.shape[:2]
    bh, bw = max(8, h // blocks), max(8, w // blocks)
    variances = []
    for r in range(blocks):
        for c in range(blocks):
            block = img_gray[r * bh:(r + 1) * bh, c * bw:(c + 1) * bw]
            if block.size < 64:
                continue
            variances.append(float(np.var(block)))
    if len(variances) < 6:
        return {"dispersion": 0.0, "median": 0.0}
    med = float(np.median(variances))
    mad = float(np.median(np.abs(np.array(variances) - med)))
    dispersion = mad / max(1.0, med)
    return {"dispersion": round(dispersion, 3), "median": round(med, 1)}


def _edge_sharpness_disparity(img_gray: "np.ndarray", blocks: int = 8) -> float:
    """Region-wise Laplacian energy disparity — inconsistent sharpness can
    indicate composited elements."""
    h, w = img_gray.shape[:2]
    bh, bw = max(16, h // blocks), max(16, w // blocks)
    energies = []
    for r in range(blocks):
        for c in range(blocks):
            block = img_gray[r * bh:(r + 1) * bh, c * bw:(c + 1) * bw]
            if block.size < 256:
                continue
            energies.append(float(np.var(cv2.Laplacian(block, cv2.CV_64F))))
    if not energies:
        return 0.0
    med = float(np.median(energies))
    mad = float(np.median(np.abs(np.array(energies) - med)))
    return mad / max(1.0, med)


def _ocr(img_pil: Image.Image) -> tuple[str, Optional[float]]:
    try:
        import pytesseract
    except Exception:
        return "", None
    # preprocessing: grayscale → resize (min side 1000) → Otsu-like threshold
    img = img_pil.convert("L")
    w, h = img.size
    scale = max(1.0, 1000.0 / max(1.0, min(w, h)))
    if scale > 1.0:
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    try:
        if HAS_CV:
            arr = np.array(img)
            arr = cv2.fastNlMeansDenoising(arr, h=8)
            thr = cv2.threshold(arr, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
            data = pytesseract.image_to_data(Image.fromarray(thr), output_type=pytesseract.Output.DICT)
        else:
            # Pure-PIL preprocessing (hosts without OpenCV/numpy): autocontrast +
            # luminance-median binarization approximates the Otsu path.
            img_prep = ImageOps.autocontrast(img)
            hist = img_prep.histogram()
            total = sum(hist) or 1
            acc, median_level = 0, 128
            for level, count in enumerate(hist):
                acc += count
                if acc >= total / 2:
                    median_level = level
                    break
            thr_img = img_prep.point(lambda p: 255 if p > median_level else 0)
            data = pytesseract.image_to_data(thr_img, output_type=pytesseract.Output.DICT)
    except Exception:
        try:
            data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
        except Exception:
            return "", None
    words: list[str] = []
    confs: list[float] = []
    for word, conf in zip(data.get("text", []), data.get("conf", [])):
        w_text = (word or "").strip()
        try:
            c = float(conf)
        except (TypeError, ValueError):
            continue
        if w_text and c > 20:
            words.append(w_text)
            if c > 0:
                confs.append(c)
    text = " ".join(words)
    avg_conf = round(sum(confs) / len(confs) / 100.0, 3) if confs else None
    return text, avg_conf


_SCREENSHOT_HINTS = {
    "iphone": "iOS status bar / layout cues", "android": "Android layout cues",
    "twitter for": "posted via X/Twitter client", "instagram": "Instagram client embed",
    "facebook": "Facebook client embed", "whatsapp": "WhatsApp forward layout",
    "telegram": "Telegram forward layout", "screenshot": "screenshot-labelled asset",
}


def analyze_image(data: bytes, filename: str = "image", vision_describe=None) -> ImageAnalysisResult:
    """Full image pipeline. `vision_describe`: optional async callable returning
    an LLM/vision text — used ONLY as an auxiliary AI-generation signal.

    Works on hosts WITHOUT numpy/OpenCV too (slim publish runtime): metadata,
    OCR and screenshot heuristics are pure Pillow; the cv2 manipulation
    detectors simply report themselves skipped (honest degradation)."""
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img)
    fmt = img.format or (filename.rsplit(".", 1)[-1].upper() if "." in filename else "UNKNOWN")
    width, height = img.size

    result = ImageAnalysisResult(
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
        width=width, height=height,
        format=str(fmt),
        authenticity_note="",
        detectors_run=["metadata"],
    )

    result.exif, result.metadata_anomalies = _analyze_metadata(img, str(fmt))

    # screenshot heuristic: exact device-resolution aspects + no EXIF
    common = {(w, h) for w, h in [(1170, 2532), (1179, 2556), (1080, 2400), (1440, 3200), (1284, 2778), (828, 1792), (750, 1334)]}
    if (width, height) in common and not result.exif:
        result.screenshot_hints.append("device-screen resolution with stripped metadata")

    # ---- OCR (spec #15) --------------------------------------------------
    try:
        text, conf = _ocr(img)
        result.ocr_text = text[:20_000]
        result.ocr_confidence = conf
        result.ocr_word_count = len(text.split())
        result.detectors_run.append("ocr(tesseract)")
        lowered = text.lower()
        for key, label in _SCREENSHOT_HINTS.items():
            if key in lowered:
                result.screenshot_hints.append(f"OCR text suggests {label}")
    except Exception as exc:
        logger.warning("OCR failed: %s", exc)
        result.ocr_text = ""
        result.metadata_anomalies.append("ocr_unavailable")

    # ---- manipulation heuristics (spec #14) ------------------------------
    signals: list[dict[str, Any]] = []
    risk_score = 0.0
    if HAS_CV:
        try:
            img_rgb = np.array(img.convert("RGB"))
            gray = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2GRAY)
            result.detectors_run += ["resave_difference", "noise_inconsistency", "edge_sharpness_disparity"]

            rd = _resave_difference(gray)
            if rd > 6.0:
                signals.append({"signal": "resave_difference", "value": round(rd, 2),
                                "note": "high resave sensitivity — local regions may have different edit/compression history"})
                risk_score += 0.18
            noise = _noise_inconsistency(gray)
            if noise["dispersion"] > 0.9:
                signals.append({"signal": "noise_inconsistency", "value": noise["dispersion"],
                                "note": "uneven local noise — possible spliced regions (weak signal)"})
                risk_score += 0.15
            es = _edge_sharpness_disparity(gray)
            if es > 1.6:
                signals.append({"signal": "edge_sharpness_disparity", "value": round(es, 2),
                                "note": "inconsistent sharpness across regions — possible composite (weak signal)"})
                risk_score += 0.12
        except Exception as exc:
            logger.warning("manipulation heuristics failed: %s", exc)
    else:
        result.metadata_anomalies.append("opencv_unavailable — manipulation detectors skipped")

    for anomaly in result.metadata_anomalies:
        if anomaly.startswith("editing_software_tag"):
            risk_score += 0.2
    result.manipulation_signals = signals
    result.manipulation_risk = round(min(1.0, risk_score), 3)

    # ---- AI-generation signal (spec #18) — pluggable, honest -------------
    # Only a model-based signal (sidecar vision) may populate this. Heuristics
    # must NOT guess "AI-ness" — the note explains exactly what ran.
    result.ai_note = (
        "No AI-generation detector configured beyond the optional vision-model signal. "
        "Heuristic image forensics CANNOT prove or disprove AI generation; treat any "
        "signal as indicative only (spec #18: never claim 100% AI-generated)."
    )
    if vision_describe is not None:
        try:
            text_out = vision_describe  # pre-computed string if caller resolved it
            if text_out:
                low = text_out.lower()
                hits = sum(k in low for k in ("ai-generated", "generat", "synthetic", "artifacts typical of", "likely ai"))
                if hits >= 2:
                    result.ai_generation_signal = 0.72
                    result.ai_signal_confidence = "MEDIUM"
                elif hits == 1:
                    result.ai_generation_signal = 0.55
                    result.ai_signal_confidence = "LOW"
                else:
                    result.ai_generation_signal = 0.25
                    result.ai_signal_confidence = "LOW"
                result.detectors_run.append("vision_model_signal")
                result.ai_note = "Vision-model impression combined with heuristics; model-based signal only — never definitive."
        except Exception:
            pass

    # ---- authenticity note (spec #14 honesty rule) -----------------------
    parts = []
    if result.manipulation_risk and result.manipulation_risk >= 0.3:
        parts.append("several manipulation indicators present — manual review recommended")
    elif result.manipulation_risk and result.manipulation_risk > 0:
        parts.append("minor metadata/compression anomalies only")
    else:
        parts.append("no strong manipulation evidence detected")
    if not result.exif:
        parts.append("metadata absent, which limits provenance checking (spec #17: provenance not claimable without data)")
    result.authenticity_note = "; ".join(parts) + ". This is a heuristic assessment, not proof of authenticity."

    return result
