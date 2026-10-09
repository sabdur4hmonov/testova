"""AUDIT B-1 — the deskew quarter-turns every answer sheet.

`preprocess_image` fed `cv2.minAreaRect`'s angle to

    if angle < -45: angle = -(90 + angle)
    else:           angle = -angle

which assumes OpenCV's PRE-4.5 angle range of [-90, 0). Installed is 4.11,
whose range is (0, 90] — so the first branch is dead and an already-straight
page (raw 90.0) becomes applied -90.0, clears the `abs(angle) > 0.5` gate, and
`warpAffine` turns the page a quarter-turn while keeping the ORIGINAL portrait
(w, h): content cropped, the gaps filled with BORDER_REPLICATE smear.

Two test layers, because the real corpus is untracked local data:
  * synthetic pages with a KNOWN skew — always run, and are the permanent
    regression guard;
  * the real answer-sheet photos in storage/temp/answer_sheets/ — the evidence
    the audit was proven on, skipped cleanly when that directory is absent.

TWO metrics were tried and REJECTED before the one used here, both recorded so
nobody re-derives them:
  * an angle-based residual cannot see this bug at all — a rectangle's
    orientation is only defined modulo 90, so a quarter-turned page measures
    as perfectly straight (residual -0.00 for every sheet, bug present).
  * a row-vs-column "is the content horizontal" ratio scored the BUGGY output
    as horizontal on 16 of 18 sheets: the quarter-turn crops the content away
    and fills the frame with BORDER_REPLICATE bands, which are themselves
    horizontal.

What IS decisive is comparing the output against the same function with the
deskew neutralized: correlation 0.132-0.366 with the bug, 1.000 without it.
"""
from __future__ import annotations

import glob
import os

import numpy as np
import pytest
from PIL import Image, ImageDraw

cv2 = pytest.importorskip("cv2")

from app.services import file_processor as fp  # noqa: E402
from app.services.file_processor import (  # noqa: E402
    PREPROCESS_MAX_DIM, deskew_correction, preprocess_image,
)

_SHEET_DIR = os.path.join("storage", "temp", "answer_sheets")


def _sheets() -> list[str]:
    return sorted(glob.glob(os.path.join(_SHEET_DIR, "*.jpg")))


def _clean_page(skew_deg: float) -> Image.Image:
    """A scan-like page: white ground, black text bars, rotated by a KNOWN
    angle. Unlike a phone photo, `gray < 200` here selects only the ink, so
    minAreaRect genuinely measures the skew."""
    img = Image.new("RGB", (900, 1200), "white")
    d = ImageDraw.Draw(img)
    for i in range(18):
        y = 120 + i * 55
        d.rectangle([150, y, 150 + 560 - (i % 4) * 70, y + 22], fill="black")
    return img.rotate(skew_deg, resample=Image.BICUBIC, fillcolor=(255, 255, 255))


def _raw_angle(img: Image.Image) -> float:
    """The raw minAreaRect angle preprocess_image works from — same threshold,
    same (row, col) coordinate order."""
    gray = cv2.cvtColor(np.array(img.convert("RGB")), cv2.COLOR_RGB2GRAY)
    coords = np.column_stack(np.where(gray < 200))
    return cv2.minAreaRect(coords)[-1]


def _undeskewed(monkeypatch, img: Image.Image) -> Image.Image:
    """The SAME function with the deskew neutralized — the reference an output
    must match whenever no rotation is warranted. Uses preprocess_image itself
    on both sides, so the comparison cannot drift if CLAHE is ever retuned."""
    monkeypatch.setattr(fp, "deskew_correction", lambda _raw: 0.0)
    try:
        return preprocess_image(img)
    finally:
        monkeypatch.undo()


def _corr(a: Image.Image, b: Image.Image) -> float:
    x = np.asarray(a.convert("L"), dtype=np.float64).ravel()
    y = np.asarray(b.convert("L"), dtype=np.float64).ravel()
    return float(np.corrcoef(x, y)[0, 1])


def _loaded(path: str) -> Image.Image:
    img = Image.open(path).convert("RGB")
    img.thumbnail((PREPROCESS_MAX_DIM, PREPROCESS_MAX_DIM))
    return img


# ── the pure angle helper ────────────────────────────────────────────────────

def test_a_straight_page_is_not_rotated():
    """The whole bug in one line: OpenCV >=4.5 reports a straight page as 90.0,
    and the shipped logic turned that into a -90 degree rotation."""
    assert deskew_correction(90.0) == pytest.approx(0.0, abs=1e-9)


def test_the_correction_undoes_a_small_positive_skew():
    assert deskew_correction(3.0) == pytest.approx(-3.0)


def test_the_correction_undoes_a_small_negative_skew():
    """A page skewed the OTHER way reports 87.0, not -3.0, under 4.5+. The
    shipped code turned that into -87 — the same quarter-turn, mirrored."""
    assert deskew_correction(87.0) == pytest.approx(3.0)


def test_the_pre_4_5_convention_is_also_handled():
    """The root cause was a hard-coded version assumption, so the fold is
    modulo 90 rather than a swap to 4.5+'s convention: -87.0, what pre-4.5
    reported for a 3-degree skew, must give the same answer as 3.0 does."""
    assert deskew_correction(-87.0) == pytest.approx(deskew_correction(3.0))


def test_no_input_angle_can_produce_a_quarter_turn():
    """The invariant that rules out the whole CLASS of bug, not just this
    instance: whatever OpenCV reports, the correction stays within +-45."""
    for raw in np.arange(-180.0, 180.01, 0.25):
        assert abs(deskew_correction(float(raw))) <= 45.0, raw


# ── synthetic pages with a known skew (always run) ───────────────────────────

def test_a_straight_synthetic_page_is_passed_through_unrotated(monkeypatch):
    page = _clean_page(0)

    out = preprocess_image(page)

    assert _corr(out, _undeskewed(monkeypatch, page)) > 0.999


@pytest.mark.parametrize("skew", [3.0, -3.0, 7.0, -7.0])
def test_a_genuinely_skewed_page_is_actually_straightened(skew, monkeypatch):
    """Proves the fix is not merely 'never rotate'. A real skew must still be
    corrected — the residual after preprocessing is near zero, and the output
    genuinely differs from the un-deskewed reference."""
    page = _clean_page(skew)
    assert abs(deskew_correction(_raw_angle(page))) > 1.0, (
        "the fixture must present a real skew to correct")

    out = preprocess_image(page)

    residual = deskew_correction(_raw_angle(out))
    assert abs(residual) < 1.0, (
        f"a {skew} degree skew was left at {residual:.2f} degrees")
    assert _corr(out, _undeskewed(monkeypatch, page)) < 0.999, (
        "the deskew did not actually rotate anything")


# ── the real answer sheets the audit was proven on ───────────────────────────

@pytest.mark.skipif(not _sheets(), reason=f"no real sheets in {_SHEET_DIR}")
def test_every_real_sheet_is_left_unrotated():
    """These are phone photos: `gray < 200` selects most of the frame, so
    minAreaRect returns the frame itself — raw is exactly 90.0 on every one.
    The shipped code turned that into -90.0 for all of them."""
    bad = []
    for path in _sheets():
        raw = _raw_angle(Image.open(path))
        applied = deskew_correction(raw)
        if abs(applied) > 0.5:
            bad.append((os.path.basename(path), raw, applied))
    assert bad == [], f"{len(bad)} real sheets would still be rotated: {bad[:3]}"


@pytest.mark.skipif(not _sheets(), reason=f"no real sheets in {_SHEET_DIR}")
def test_every_real_sheet_survives_preprocess_intact(monkeypatch):
    """End to end through the real function, on the real photos, measured on
    the PIXELS. Correlation against the deskew-neutralized reference was
    0.132-0.366 with the bug present and 1.000 without it, so this threshold
    sits far from both."""
    bad = []
    for path in _sheets():
        img = _loaded(path)
        c = _corr(preprocess_image(img), _undeskewed(monkeypatch, img))
        if c < 0.99:
            bad.append((os.path.basename(path), round(c, 3)))
    assert bad == [], (
        f"{len(bad)} of {len(_sheets())} real sheets were corrupted by the "
        f"deskew: {bad[:3]}")
