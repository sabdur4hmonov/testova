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
