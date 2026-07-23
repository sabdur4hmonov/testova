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

### Defect 5 — answer-revealing `[Rasm]` description — **CLOSED AS MEASURED (2026-07-23)**

**Original report.** A figure question with no recoverable image printed a
fallback description ("…segments AB, BC, AC") that is answer-equivalent to the
correct option — it solves the question. Policy: when a figure can't be
rendered, a description that gives away the answer should be suppressed (or the
question flagged), not printed. Distinct from the settled VML-not-extractable
finding — the issue is the *content* of the fallback, not the missing image.

**Resolution: no code change. The class is already closed for new extractions.**
Decided after a full measurement against the stored rows (DB only — zero Gemini
quota, no re-upload). Do not re-plan this without reading the reasoning below.

#### What is actually stored
2423 questions → 216 carry `image_description` → 177 of those have a real
`image_path`. **39 rows actually print a `[Rasm]` box** (28 distinct texts):

| class | rows | example |
|---|---|---|
| ESSENTIAL content | ~20 | number lines with the distances; six-reaction chemistry tables (to 465 chars); reaction chains |
| decorative | 9 | "A simple red car with two black wheels on a light blue background." |
| generic, not revealing | ~7 | "A diagram showing points A, B, C on a line." |
| **answer-revealing** | **4** | "A diagram showing points A, B, C and segments AB, BC, AC." |

The ESSENTIAL set is ~20× the problem — those descriptions ARE the missing
figure, and suppressing them makes the questions unanswerable. **Any blanket
suppression of fallback descriptions is disqualified by that ratio alone.**

#### Why it is closed: 3 of the 4 are already handled
`_is_meta_desc` / `_META_DESC_RE` in `ai_analyzer.py` already blocks three of
the four. Measured, per alternative:

- "Question 7 asks to write the notation of segments…" → fires `^question \d+`
- "Question 7 does not contain a scheme/diagram…" → fires `does-not-<verb>` **and** `^question \d+`
- **"A diagram showing points A, B, C and segments AB, BC, AC." → fires NOTHING**

Those three are **malformed** descriptions — prose *about the question* rather
than *of a figure* — and are wrong unconditionally, whatever the question asks.
They are stale rows predating that guard (commit `c97d657`). The guard catches
them incidentally; not one is caught *because* it leaks.

The fourth is **well-formed**: a faithful description of the figure. It is a
leak only *relative to this question's answer*, because this question happens to
ask the student to name the segments the figure contains. **That is a category
difference, not a pattern gap** — and it needs a genuine semantic coincidence
(a figure question whose faithful description names the answer, AND an
unrecoverable figure). **One observed instance, in stale pre-guard data, with
zero recurrence since.**

#### DO NOT "fix" this by extending `_is_meta_desc`
Measured against all 28 distinct descriptions:

- `>=2` bare 2-cap tokens → catches it, but **9 false positives**, six of them
  ESSENTIAL chemistry (`KOH`, `CO2`, `NaOH`, `Zn`, `O2` all yield 2-cap tokens).
- `>=3` bare 2-cap tokens → still **6 false positives**, all essential tables.
- `segments?\s+[A-Z]{2}` → catches it with **0 false positives on this corpus**,
  and was still **REJECTED**: it is a *content* rule wearing a *shape* guard's
  clothes. It does not generalise past the word "segment" (the same leak in
  angles, or a table transcription, is untouched), and it would suppress that
  exact sentence for a question where the description is legitimate and NEEDED
  ("How many segments are shown?" / "What is the length of AB?"). It would also
  contaminate a guard whose whole value is a clean shape-only contract —
  "this description is malformed" — verifiable without knowing the question.

#### If a real leak ever recurs, this is the mechanism (Option D)
Only an **answer-comparison** detector actually closes the residual class,
because the answer is the only thing separating the leak from the 8 innocent
near-misses ("A line segment with points A, B, C marked on it.").

- **Where:** at PDF render time, not extraction. The description is stored at
  extraction (before the teacher enters the key) but printed at variant
  generation (key known). **34 of 39 rows have a resolvable key**, including
  pre-007 legacy rows via `Question.options_ordered`'s `option_a..d` fallback.
- **Rule:** every token of the **correct option** must appear **as a token** in
  the description → suppress (and report to the teacher; don't drop silently).
- **Gate at >=2 tokens.** Single-token numeric answers (`280`, `138`, `194`) are
  the false-positive risk — a transcribed table can contain that number. The
  gate keeps the real case (3 tokens) and drops every numeric row from scope.
- **Substring matching does NOT work** — the real row stores the correct option
  as `AB,AC,BC` while the description says `AB, BC, AC`: same set, different
  order, because variants shuffle options. And squashing punctuation makes
  "points A, B, C" contain `ab`/`bc` by accident. **Token boundaries are
  essential**; resolve the correct option **per variant**, not from the source row.
- **Measured:** 4/4 revealing rows caught, **0 false positives across all 34
  evaluable rows**, including the 8 near-miss segment descriptions.
- **Render sites:** `_append_img_desc` in `pdf_generator.py`, called from the
  image-failed-to-load and no-path branches, plus the compact builder's own site.

**Reopen criterion:** a leak observed on a NEW extraction (post-`_is_meta_desc`).
Until then this is a closed door, and Option D is machinery maintained forever
against a class that is not being produced.

## PDF variant layout — Group B (options reflow) considerations

Group A (compact header, reachable write-in line, tighter spacing) shipped in
**v0.23**. Group B — laying options out on fewer lines (4-across, else a 2x2
grid, else one per line) — is deliberately held for its own session.

### The alignment constraint Group B lives or dies on
Change 2 alters how an option LABEL maps onto a printed POSITION. A student
marks a sheet against those positions and the grader reads it against the
STORED labels, so a reflow that ever detaches a label from its own text — or
drops a label into the wrong grid cell — silently reintroduces the exact
option-alignment bug class the whole `option_label_recovery` backstop exists to
prevent, except as a LAYOUT bug: invisible in the code, visible only in a
printed PDF.

- Carry `"{letter}) {text}"` as **ONE table cell**. Never put the label in one
  cell and its text in another, where the grid could drift them apart.
- Pull labels from the stored option data, **whatever they are**. They are NOT
  sequential: `a, b, d, e` (gapped, 252 rows) and `А, Б, В, Г` (Cyrillic, 72
  rows) are both common — see the Defect 4 measurement. `build_variants_pdf`
  already iterates `options.items()` in stored order, and
  `tests/test_variants_pdf_layout.py` pins that gapped and Cyrillic sets print
  verbatim. Any reflow must keep those tests green.
- Prove the 2x2 grid on a GAPPED set specifically — four options with no `c`
  must render `a/b/d/e`, never relabelled to `a/b/c/d`.
- Prove the width thresholds with a long-option case that forces 2x2 and a
  very-long case that forces one-per-line, rather than guessing them.

### Deferred INTO Group B: the page-break option orphan
Observed on the Group A sample render — a variant's last option (`D) …`) landed
alone at the top of the next page, split from its question. **Pre-existing, not
a Group A regression:** `build_variants_pdf` never wraps a question block in
`KeepTogether`, while `build_variants_pdf_compact` already does. Group A's
tighter spacing only shifts WHERE breaks fall; it does not create the behaviour.

It is deferred into Group B on purpose rather than fixed separately: wrapping a
question plus its options in `KeepTogether` changes pagination AND interacts
with how the options lay out (a 4-across row, a 2x2 grid and a 1-per-line stack
each have a different block height, so each changes what fits before a break,
and an over-eager `KeepTogether` on a tall block pushes whole questions to the
next page and wastes more space than the orphan cost). Decide both together.
