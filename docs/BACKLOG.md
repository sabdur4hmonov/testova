# Backlog

## Compact 2-column PDF + API cost optimization

### FEATURE — Compact 2-column PDF
- New function in `pdf_generator.py`: `build_variants_pdf_compact(variants, exam_title)`.
- 2-column layout via ReportLab `Frame` + `PageTemplate`.
- Each variant MUST start on a new **page** (`PageBreak`), never mid-page.
- Column width = `(PAGE_WIDTH - 3*MARGIN) / 2`.
- Wide images span both columns (`KeepTogether`).
- Smaller fonts: question **9pt**, option **8pt**.
- `build_variants_pdf()` and `build_answer_key_pdf()` stay **UNCHANGED** (the answer key is always single-column).
- Ask the teacher the format choice **BEFORE** the Gemini call, not after — one API call, no regeneration.
- Flow: teacher sends PDF → bot asks *"Variantlarni qanday formatda olmoqchisiz?"* with **[Oddiy] / [Ixcham]** → teacher picks → **THEN** extract → generate in chosen format.
- Touches: `upload.py`, `keyboards/inline.py`, `states/forms.py` (new `FORMAT_CHOICE` state), `pdf_generator.py`.

**Shipped simplifications (revisit later):**
- Compact PDF: wide figures currently scale to column width. If a real exam needs a
  true full-page-width figure, add a second `PageTemplate` with a full-width frame and
  switch templates mid-document.
- Compact format is currently only offered in the single-upload flow (`upload.py`). The
  multi-source builder (`multi_source.py`) always produces standard single-column output.
  Wire the format choice into `multi_source` later.

### FEATURE — Gemini API cost optimization (`ai_analyzer.py`)
1. **Skip blank pages**: if a page image is >90% white pixels (PIL), skip the Gemini call.
2. **Skip header-only pages**: if the PDF page text < 100 chars, skip the Gemini call.
3. **Cache duplicate pages**: `hashlib.md5` of the page image bytes; reuse the result if that page was already processed this session.
4. **Lower DPI for text-only pages**: 150 instead of 200 when the page has no embedded images; keep 200 for pages that do.

### NICE-TO-HAVE — image keyword safety net (`ai_analyzer.py`)
- If Gemini returns `has_image=False` but the question text contains any of
  `rasmda` / `rasmga` / `jadvalda` / `diagrammada` / `grafikda` / `shakl` /
  `ko'rsatilgan` / `tasvirlangan` / `sxemada` → force `has_image=True`.
- Also mention these keywords in `VISION_PROMPT` so Gemini sets `img=true` itself.

### Follow-ups
- Per-user attribution for `gemini_usage`: thread `user_id` through to `AIAnalyzer`
  (currently logged as NULL) so `/usage` can break cost down per teacher.
- `/usage` merges output + thinking into one figure. Split them into separate
  columns so the thinking-token share of cost is visible (2.5 Flash thinks by
  default and bills thinking at the $2.50/1M output rate). Low priority — total
  cost is currently ~200 so'm per extraction.

### Known extraction risks
Gemini extraction of radicals is non-deterministic. On 1 of 6 T-108 runs it
nested "4sqrt(3) + 2" as "4sqrt(sqrt(3) + 2)" — a meaning error (7.46 vs 8.93).
The VISION_PROMPT sqrt rule is now **bidirectional** to guard both directions
(a term must neither escape the radical nor be pulled into it). The renderer is
faithful by design and will NOT un-nest a corrupt source (un-nesting would be a
regex-on-math fix that corrupts genuinely nested expressions). If a wrong
radical ever appears in an exported PDF, check the **DB source first** — it is
almost certainly extraction, not rendering.

### DO NOT IMPLEMENT — poisoned items from an old pre-T-108 planning doc
- ~~"Answer options have digits bleeding from the adjacent column (`B) 1, 2` → `B) 1, 25`)"~~
- ~~"Gemini invents numbers (`y = x - 0,2` → `y = x - 0,212`)"~~

These are **NOT bugs**. They are the **GHOST BUG**: a trailing digit in the extracted
**text layer** is the **PAGE NUMBER**. The printed PDF is correct — verified visually
on T-108 multiple times. Adding trailing-digit stripping, or a `VISION_PROMPT` rule to
"stop at the correct boundary" on trailing digits, would corrupt real answers like
`148`, `12800`, `-12`, `-15`. **NEVER FIX THIS.**

Also obsolete in that doc (already implemented and committed, do not re-plan):
- ~~"2-column layout support"~~
- ~~"question ordering across columns"~~

## DOCX extraction / rendering defects (from live run, 2026-07-22)

Found on a real DOCX math exam (`5-sinf_matematika_test_1-variant.docx`). All
pre-existing (git-confirmed: `docx_to_images` and `math_render` unchanged on the
grading-unification branch). Defect 1 (superscript loss) is being handled
pre-merge; these are the carve-offs.

### Defect 3 — `docx_to_images` drops OMML equations & VML shapes (BIG)
A whole question's content vanished (a vertical subtraction puzzle → only the
stem stored). `docx_to_images` renders only `paragraph.text` + tables; Word
equation objects (`m:oMath` / `oMathPara`) and drawing shapes (`v:shape` /
`w:pict`) are in separate XML namespaces and are silently dropped before Gemini.
Fix needs an OMML→text linearizer (mini-parser over ~15 element types: `m:f`,
`m:rad`, `m:sSup`, `m:sSub`, `m:d`, `m:nary`, …) or an OMML→image render (headless
Word/LibreOffice dep). LARGE — genuinely separate from the Defect 1 run-walk.
Lower severity than Defect 1: affected puzzles are OPEN (no answer key), so a
missing puzzle is a *visibly incomplete* question, ungraded — not a silently
wrong key. Same root also explains the un-extractable figure (Defect 5, a VML
shape).

### Defect 2 — `math_render` mis-scopes a `^` on a fraction denominator (MEDIUM)
`3,5x/0.7`-style stems and, more sharply, **an exponent on a fraction
denominator**: `a/b^2` → renders `(a/b)^2` (WRONG, should be `a/b^2`); `a^2/b^2`
→ `(a^2/b)^2`. A parser-precedence bug in `math_render`, independent of source
(hits PDF too). Found via adversarial testing of the Defect 1 caret synthesis —
NOTE: the flat form (`a2/b2`) is *also* mis-typeset today, so Defect 1 does not
regress it (wrong→wrong), but this bug should be fixed so DOCX superscript
fractions render correctly. Visible-wrong, single question, no silent key
corruption → post-merge. Contrast: `(2,15+a)/2` (parens) renders correctly.

### Defect 4 — capital label leaks into printed options; no display normalization (SMALL)
A teacher typo (`D=8`) stored the option label as capital `D` among lowercase
`a,b,e`, and it prints verbatim. Verbatim storage is CORRECT for real gapped /
Cyrillic labels, and the PDF-only backstop rightly never claimed DOCX — but a
LONE capital among an otherwise-lowercase label set is almost certainly a typo
and should be flaggable (or display-normalized) even without the text-layer
backstop. Backlog: a cheap heuristic flag on mixed-case label sets, surfaced in
the extraction summary like `label_doubt`.

### Defect 5 — answer-revealing `[Rasm]` description (SMALL, policy)
A figure question with no recoverable image printed a fallback description
("…segments AB, BC, AC") that is answer-equivalent to option d — it solves the
question. Policy: when a figure can't be rendered, a description that gives away
the answer should be suppressed (or the question flagged), not printed. Distinct
from the settled VML-not-extractable finding — the issue is the *content* of the
fallback, not the missing image.
