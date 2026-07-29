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
- ~~Compact format is currently only offered in the single-upload flow~~ **DONE**
  — `multi_source.py` asks Oddiy/Ixcham at finish (line ~897) and routes to
  `build_variants_pdf_compact` when `pdf_format == "compact"` (line ~1078), the
  same FSM key as the single-upload flow.

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

### Defect 3 — `docx_to_images` drops OMML equations & VML shapes — **FIXED + SHIPPED v0.30 (Approach B)**

**Root cause (confirmed):** `docx_to_images` renders only `paragraph.text` +
tables + inline `<a:blip>` rasters. OMML (`m:oMath`) is not in `para.runs`, and
VML strokes (`v:shape`/`w:pict`) carry no image relationship — both are skipped
before Gemini, so a segment diagram printed as `[Rasm]` text and a stacked
subtraction vanished. Proven DOCX-only: the PDF version of the same test crops
the figure perfectly via the live path; only DOCX failed.

**Fix — Approach B, headless LibreOffice (chosen over an in-house linearizer):**
`process_file` probes a DOCX for `<w:pict>`/`<v:shape>`/`<m:oMath>`; when present
it renders the DOCX → PDF with `soffice` (`file_processor.docx_to_pdf`) and
treats it as a PDF for the rest of the pipeline, so the shapes become real
pixels that flow through the proven `attach_images_to_questions` crop path. A
shape-free DOCX never enters this path (byte-identical text render); a conversion
failure falls back to that text render — **visible degradation, never silent**.
Approach A (OMML→text linearizer) was rejected: it silently corrupts *positional*
math (the vertical-subtraction puzzle is stacked/right-aligned with blanks — a
text form is plausible-but-wrong) and carries unbounded maintenance.
`Dockerfile` installs `libreoffice-writer` **and `libreoffice-math`** — the
Math component is required, proven empirically: writer alone renders the VML
segment diagram but leaves embedded OMML equations blank (the vertical-
subtraction puzzle vanished until math was added). Supersedes Defect 5's
VML-figure finding (same root, now rendered).

#### Deferred (NOT done — recorded here on purpose)
1. **2000-user conversion hardening.** The simple version spawns one `soffice`
   subprocess per shape-bearing DOCX at upload time (no Gemini cost, grading
   path untouched). Under heavy *concurrent* upload load add: a conversion
   pool / worker cap, per-process memory limits, and a concurrency gate. Not
   needed at the current user count; revisit in the scaling session.
2. **Lifecycle-tied figure-crop reaper.** `prune_debug_crops` only reaps
   diagnostic `debug_*` files. The `q*`/`docximg_` figure crops in
   `temp_images/` are the de-facto permanent image store (variant generation
   reads them back at build time, repeatedly), so they cannot be reaped per-run.
   A proper reaper must key off project lifecycle (`projects.expires_at`) —
   delete a project's crops when it expires. Until then `temp_images/` still
   grows (slowly) with real figure crops.
3. **File 3 raster inconsistency (different defect).** `attach_docx_inline_images`
   uses an EXACT count-guard (`len(flagged) != len(images) → attach nothing`).
   When Gemini's `has_image` count varies run-to-run against a DOCX's embedded
   raster count, a raster-figure DOCX (DrawingML `pic:pic`/`a:blip`, e.g. the
   orange-rectangle/tree/car test) inconsistently gets all-or-no images. Not the
   VML/OMML drop; needs its own fix (tolerant pairing or per-question anchoring).
4. **`wps:` DrawingML shapes not gated.** The shape probe triggers on VML
   (`v:shape`/`w:pict`) and OMML (`m:oMath`) only — the classes proven to drop in
   the corpus. Modern DrawingML *non-picture* shapes (`<wps:...>`, WordprocessingML
   drawing canvas) are not gated and would still drop. None present in the corpus;
   add `<wps:` (and re-verify File 3 stays out) if such a file appears.

### Defect 2 — `math_render` mis-scopes a `^` on a fraction denominator — **FIXED + SHIPPED v0.21**
Fixed by splitting `_term` into `_power` (factor + glued scripts) and `_term`
(division over `_power` operands) in `math_render.py`, so a script binds tighter
than fraction division: `a/b^2` now renders `a/b^2`, not `(a/b)^2`. Tag v0.21.
Original report below, kept for context.

`3,5x/0.7`-style stems and, more sharply, **an exponent on a fraction
denominator**: `a/b^2` → renders `(a/b)^2` (WRONG, should be `a/b^2`); `a^2/b^2`
→ `(a^2/b)^2`. A parser-precedence bug in `math_render`, independent of source
(hits PDF too). Found via adversarial testing of the Defect 1 caret synthesis —
NOTE: the flat form (`a2/b2`) is *also* mis-typeset today, so Defect 1 does not
regress it (wrong→wrong), but this bug should be fixed so DOCX superscript
fractions render correctly. Visible-wrong, single question, no silent key
corruption → post-merge. Contrast: `(2,15+a)/2` (parens) renders correctly.

### Defect 4 — capital label leaks into printed options — **FIXED + SHIPPED v0.22**
Fixed by `flag_mixed_case_labels` in `option_label_recovery.py`: a label set that
mixes a cased-upper and cased-lower label raises the existing `label_doubt` flag
(surfaced in the extraction summary), source-independent so it covers DOCX. It
FLAGS, never rewrites — verbatim storage of real gapped/Cyrillic labels is
preserved. Tag v0.22. Original report below, kept for context.

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

### `^N.` superscript-stem strip — SHIPPED v0.27

The intersection of two shipped passes. `_para_scripted_text` (Defect 1 fix)
surfaces a DOCX superscript as caret notation, so a source question number typed
as a superscript becomes `^11`; Gemini transcribes the stem as `^11.Hisoblang:`.
`_strip_own_number` then failed to remove it because `^` was not in its leading
artifact class — the leading own-number token survived into the printed stem
(rendered as `13. ^11.Hisoblang:` after the position prefix).

**Fix:** one optional `\^?` before `{n}` in `_strip_own_number`
(`ai_analyzer.py`). Both gates unchanged — it strips the caret form ONLY when
the number is the question's OWN number AND is immediately followed by a `.`/`)`
terminator.

**Boundary, proven by running the shipped pass over every stored leading-caret
stem (11 rows):** strips exactly the **1** own-number bleed (qn=11), touches
**0** of the 10 legitimate leading superscripts — isotope mass numbers in
nuclear-reaction equations (`^210_82 Pb`, `^254_102 No`, `^A_Z E`, `^56_26 Fe`,
…). Those are double-protected: the number is not the question's own AND `^210`
is followed by `_`, never a terminator. Source-agnostic fix in the shared strip
(a PDF whose leading number is visually superscript could yield the same shape),
safe everywhere because of the two gates. Tests in
`tests/test_strip_own_number.py`.

## PDF variant layout — Group B (options reflow) — SHIPPED v0.25

Group A (compact header, reachable write-in line, tighter spacing) shipped in
**v0.23**. Group B shipped in **v0.25**: options now share a line when they fit
on one, and a question is held together across a page break.

### What shipped
`_option_flowables` in `pdf_generator.py`, a width ladder driven by the WIDEST
option in the set:

1. **all N on ONE row** (N columns) — 4-across, but also 3- and 5-across;
2. **2 columns × ceil(N/2) rows**, filled ROW-MAJOR so reading order is stored
   order;
3. **one per line** — the pre-v0.25 layout, kept as the fallback.

Blanket `KeepTogether` per question was folded in (it was deferred INTO this
work, see below).

### Measured, against all 546 stored option sets
| tier | rows | share |
|---|---|---|
| all N on one row | 542 | 99.3% |
| 2-column grid | 4 | 0.7% |
| one per line | 0 | **0.0%** |

- Sizes are **not** all four: `n=3` 47 rows, `n=4` 480, `n=5` 19 — 12% is
  non-4, which is why the ladder generalises to N-across instead of hard-coding
  a 2x2.
- Real-project effect: proj40 **5 pages → 3**, proj25 **5 pages → 3**. Option
  block height −74% (2422→618pt) and −81% (1927→358pt).

### Why the width test is a GUARD, not a cosmetic nicety
An inline typeset-math `<img>` is atomic — ReportLab neither wraps nor shrinks
it — so an option wider than its cell **draws over the next column**. Measured
on stored row `868dbdbc`: a 137.5pt formula in a 117.5pt four-across cell
overflows its neighbour by ~38pt, and the fourth crosses the right margin. That
is printed math detached from the letter that owns it — the option-alignment
bug class, reintroduced as a layout defect. `_markup_width` counts image widths
so the ladder steps down to a wider cell. `tests/test_variants_pdf_options_reflow.py`
reads image bounding boxes back out of the rendered PDF to pin this.

### DO NOT assume tier 3 is battle-tested
**Zero** of the 546 stored option sets reach the one-per-line tier (widest real
option = 152.3pt against a 228.9pt two-column cell). Only the synthetic case in
`test_very_long_options_fall_to_one_per_line` exercises that branch. The code
says so at the branch itself. Re-measure the corpus before relying on its
behaviour, and do not delete it as dead — it is the overflow backstop.

### The alignment constraint Group B lived or died on
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
  must render `a/b/d/e`, never relabelled to `a/b/c/d`. **Done — see Verified
  at ship time below.**
- Prove the width thresholds with a long-option case that forces 2x2 and a
  very-long case that forces one-per-line, rather than guessing them. **Done,
  from stored data for the 2x2 case.**
- Kept true by construction in v0.25: a wrong width estimate makes a cell WRAP
  to a taller row. Degradation costs vertical space; it cannot misalign a label.

### The page-break option orphan — RESOLVED in v0.25 (blanket KeepTogether)
Observed on the Group A sample render — a variant's last option (`D) …`) landed
alone at the top of the next page, split from its question. **Pre-existing, not
a Group A regression:** `build_variants_pdf` never wrapped a question block in
`KeepTogether`, while `build_variants_pdf_compact` always did.

The stated worry was that an over-eager `KeepTogether` on a tall block pushes
whole questions to the next page and wastes more than the orphan costs. **That
does not hold for this corpus, measured rather than assumed:** the tallest
possible question block anywhere in the stored data (a 395pt figure plus stem
and options) is **461pt against a 714pt frame — 64%**, and **0 of 177**
image-bearing questions exceed 75% of a frame. So `KeepTogether` can always
place a real block and never reaches ReportLab's "flowable too large" path.

Against that, orphaning was happening on **every** page break: before the fix
proj40 split 3 questions (q26 left *zero* options on the stem's page and one
printed alone overleaf; q35 moved four) and proj25 split 4. After: **0 splits,
0 orphaned options, on both projects.**

### Verified at ship time
- Gapped `a,b,d,e` in the 2-column grid: all four print, **no `c` appears**,
  row-major (`a b` / `d e`), and the two rows' column edges line up.
- Cyrillic `АБВГ` 4-across; mixed-case `abDe` printed as stored.
- Real row `868dbdbc` demoted to 2 columns; **0 image overflows** past a cell
  edge and none past the right margin (bounding boxes read back from the PDF).
- Synthetic very-long set falls to one per line, still in stored order.
- Answer-key and compact PDFs **byte-identical** to master
  (`4437a249…` / `70ebacee…`, built with `rl_config.invariant`) — the proof
  that `STYLES` did not leak.
- Rendered proof sheet + real-project before/after kept out of the repo
  (`sardorbek/groupb_proof/`), regenerable from `tests/` fixtures.

## PDF variant layout — compact ("Ixcham") port of Groups A + B — SHIPPED v0.26

**Why this was needed:** Groups A (v0.23) and B (v0.25) were both scoped to
`build_variants_pdf` (the standard / "Oddiy" builder). A teacher who picks
"📋 Ixcham" is routed to `build_variants_pdf_compact` (`upload.py:1332`,
`multi_source.py:1074`), which still ran the pre-Group-A/B layout — so none of
that work showed on the compact path. This port brings the compact builder up
to parity.

**Scope was 2 of 4 items, not 4** (confirmed by reading the code, not the
brief): the compact open-ended write-in line already shipped in **v0.24**, and
compact **always** had `KeepTogether`. Only the Group A header and the Group B
options reflow were actually missing.

### What shipped (three commits)
1. **Header** — `Ball:` dropped, `Variant N` centered between two rules, `c_q`
   re-parented to `question_variant` for the tighter gap. **The one-line
   fill-in row did NOT port literally:** a 212.6pt column leaves `Ism familiya:`
   a 7.1pt rule (one underscore, unwritable), so the fields split over **two**
   rows — still half the old four-row header, every rule ≥3 underscores.
2. **Shared ladder** — `_option_flowables` is now builder-agnostic: the tier
   decision and alignment contract live once, and only the cell CONTENT differs
   via `cell`/`line` callbacks. Proven a no-op for the standard builder (see
   below).
3. **Compact reflow** — the compact cell renderer routes each option through
   `_compact_flowables`, so tall stacked fractions are still promoted to their
   own Image **inside the cell**.

### The structural difference from the standard builder
The compact builder promotes tall math to a standalone flowable. Inside a grid
that means a cell holds a **list** of flowables, and `_prefix_img_row` builds a
nested table to keep a lone `A)` on the promoted image's line. Two consequences,
both handled: `_compact_flowables`' `total_w` must be the **cell** width (not
the column width, or the nested table exceeds its cell), and a promoted image is
capped to `cell_w − label_width`. Verified by geometry on a real gapped
`a,b,d,e` set of stacked fractions: every label prints on the same row as its
own fraction (~5.5pt away), zero image overflow past a cell edge.

### Measured (compact column, 8pt / 198.6pt option area, all 546 stored sets)
| tier | cell | rows | share |
|---|---|---|---|
| 4-across | 45.6pt | 276 | 50.5% |
| 2-column | 95.3pt | 204 | 37.4% |
| 3-across | 62.2pt | 46 | 8.4% |
| 5-across | 35.7pt | 17 | 3.1% |
| one-per-line | 194.6pt | **3** | 0.5% |

- **Tier 3 IS reached here (3 real rows)**, unlike the standard builder where
  zero rows reach it. The "corpus-unexercised" note is standard-builder-only
  and is NOT repeated on the shared path.
- **No column floor** on the 5-across 35.7pt tier — those 17 rows are short
  numerics that measurably fit. Revisit only if a real set looks bad.
- **Image legibility:** required image scale at the chosen tier is 100% across
  all 31 math-carrying sets — the width test routes a wide formula to a
  wide-enough tier before any shrink. No legibility floor.
- Real projects: compact proj40 **4 → 2 pages**, proj25 **3 → 2 pages**; 0
  questions split across a column break.

### Byte-identity (the non-negotiable proof)
Built with `rl_config.invariant` from the same fixture, across all three
commits:
- **standard `variants` PDF** `ee00eb8c…` — **identical to v0.25**. The refactor
  did not touch the working standard builder.
- **answer key** `4437a249…` — **identical to v0.25**. `STYLES` did not leak;
  `c_o_cell` is a child of `c_o` (itself a child of shared `STYLES["option"]`).
- The **compact** PDF hash intentionally changes (`70ebacee…` → new) — that is
  the port. Supersedes the v0.25 note above that the compact PDF was
  byte-identical to master (true at v0.25, no longer true by design).

### Compact stem math-promotion split — FIXED + SHIPPED v0.28
`_compact_flowables` promoted every tall stem image to its own line, shattering
prose around inline math (`Agar A = a/b va C = c/d bolsa` → five lines;
`2. Soddalashtiring:` / fraction / `?` on three). The stem now renders as ONE
autoLeading paragraph — exactly what the standard builder does — so math stays
inline and prose wraps naturally. `_compact_flowables` is UNTOUCHED and still
serves the options (whose narrow cells need the promotion); this is a stem-only
change to the one call site.

**The subtlety, measured:** autoLeading grows a line to its tallest fragment,
but a tall image on the NEXT line can still reach UP into the base-leading gap
the previous (text) line reserved. A 15pt leading floor on the stem style
(`c_q`) closes it — measured over all 379 stored stems that carry tall math, the
worst cross-line image/text gap goes from −1.5pt (overlap) to +1.3pt (clean);
14pt already clears it. Local child style, never touches `STYLES`.

**Proof:** answer key (`4437a249…`) and standard `variants` (`ee00eb8c…`)
byte-identical to v0.27 — the change is compact-stem-only. proj40/proj25 stay
2 pages each. `test_bug1` was rewritten from a solid-rectangle bbox check (which
false-positived on wide inline math whose tall ink is offset from the prefix —
verified clean by a +7pt ink gap directly under `10.`) to a line-aware check
with a padding tolerance.

## Grading vision read is NONDETERMINISTIC — measured v0.33 (investigate later)

**Finding (from the Fix 1 preprocess accuracy proof, 17 real answer sheets):**
the answer-sheet grading read (`sheet_reader.read_answer_sheet` → Gemini,
temperature 0.0) is NOT deterministic. Feeding the SAME preprocessed PNG bytes
twice, the parsed marked answers diverge on ~6/17 sheets (OLD-vs-OLD 6/17,
NEW-vs-NEW 5/17). The instability concentrates on **mixed 25-question sheets**
(marked options interleaved with hand-written short answers), where an ENTIRE
sheet can flip between a full read and an EMPTY read on identical pixels.

**Two consequences:**

1. **Any future accuracy proof MUST measure self-noise as the baseline.**
   "Identical input → identical output" is false here, so you cannot judge a
   preprocess/prompt/model change by OLD-vs-NEW diffs alone — you must run
   OLD-vs-OLD and NEW-vs-NEW first to establish the noise floor, then ask
   whether the change moved reads *beyond* that floor and in a *systematic*
   direction. (Fix 1 did not: OLD-vs-NEW 12/17 ≈ two independent ~6/17 noisy
   draws; NEW was strictly more accurate on the one clean noise-stable sheet.)

2. **A whole-sheet empty↔flip on the same image is a real grading-reliability
   concern, independent of any optimization.** A student could be graded 0 on a
   fully-answered sheet on an unlucky read. Worth investigating on its own:
   candidate directions — retry-and-vote (read N times, take the majority /
   flag disagreement), a "did you read fewer answers than questions?" sanity
   re-prompt, or splitting the mixed-sheet read into marked-options and
   written-answers passes. Touches VISION_PROMPT / model call → gated work,
   separate from this speed stack.

## Two-read cross-check — BUILT, MEASURED, REJECTED (v0.37)

**Idea:** read every answer sheet TWICE in parallel and flag any question where
the two reads disagree, so a CONFIDENT misread (which no single read can
self-report) gets asked instead of silently graded.

**Why it was rejected, measured on the real SARDOR sheet:**

1. **Weak catch rate.** It caught the target failure (a Q3/Q4 row swap that
   silently credited a wrong answer) in only **1 of 3 runs**.
2. **Correlated reads.** Two calls on identical pixels are NOT independent — they
   tend to be wrong the SAME way, so agreement is not evidence of correctness.
   The independence assumption the design rests on does not hold.
3. **An empty read poisoned the whole sheet.** When one of the two reads came
   back empty (a known truncation/timeout mode), EVERY question "disagreed" and
   the entire sheet was flagged — worse than not having the check.
4. **Doubled cost and tail latency.** 2x tokens per grade, and two independent
   retry loops meant a slow call could stack to ~47s.

**What replaced it:** the reader's own `"?"` ambiguity flag (routes to the
"Aniqlanmadi — qo'lda tekshiring" list) plus the per-question option-label hint,
which removed the impossible-letter class outright.

**If it is ever revisited:** with thinking disabled (v0.37) a second read is
cheap — projected ~$427/mo at 2000 teachers vs ~$907/mo for today's single
thinking-enabled read. Affordable, but it needs a genuinely independent signal
(different prompt phrasing, different image preprocessing, or a different model)
for agreement to mean anything. Repeating the same call is not that.

## P6 JPEG upload instead of PNG — TRIED, REJECTED (v0.38)

**Idea:** the preprocessed page re-encoded as PNG is ~2x larger than the source
JPEG the phone sent (689KB from a 176KB original), because PNG compresses the
CLAHE-flattened greyscale poorly. Sending JPEG would shrink the upload.

**Measured, and rejected on accuracy.** Gemini bills images by DIMENSIONS, not
bytes, so the only benefit was upload latency — but JPEG q92 changed the read:

* Cyrillic sheet: Q8 misread `Г` -> `С` in 2 of 3 runs (PNG: `Г` in 3 of 3).
* Latin sheet: the lowercase `e` on Q3 was no longer detected, 2 of 2 runs
  (PNG: all 6 E's found).

Faint pencil and small handwriting are exactly what JPEG's chroma/DCT
quantisation smooths away. A ~2x smaller upload on a ~7s call is not worth any
accuracy risk on a grading path. If revisited, try lossless WebP (smaller than
PNG, no quantisation) and re-run the same accuracy proof — quality knobs above
q92 were not explored because the upside is only latency.

## Extraction thinking-off — IMPLEMENTED, PROVEN HARMFUL, REVERTED (v0.38+)

Grading runs with thinking disabled (v0.37: cheaper, faster, MORE accurate).
The same change was applied to EXTRACTION and measured field-by-field on four
real stored tests (Cyrillic DOCX, math PDF, gapped-label PDF, figures PDF),
2 runs per config, comparing question_text / option letters / option text /
has_image / is_open_ended. It FAILED and must not be shipped.

**1. Disqualifying: an ANSWER-KEY page becomes 40 phantom questions.**
The math test's page 4 is a "JAVOBLAR JADVALI" grid (question number -> correct
letter, marked "for the teacher"). With thinking ON the model recognises it is
not a question page and extracts NOTHING from it (40 questions total, correct).
With thinking OFF it pattern-matches the number/letter grid into 40 question
stubs with EMPTY text and NO options — 80 questions for a 40-question test.
Reasoning is exactly what is needed to tell "answer key" from "questions", and
that is what thinking buys.

**2. Math content diverged.** On the figures test, q5 came back as
`(m^2 - 6n + 3m + 2mn)` with thinking ON and `(m^2 + 6n + 3m + 2mn)` with it
OFF — a sign flip inside a formula. Silent and unverifiable without the source.

**3. Text quality dropped.** q15 "jumlalardan" (ON) -> "jumladalardan" (OFF).

**4. The non-negotiable failed on 2 of 4 files.** Option letters differed on the
Cyrillic q8 (ON mixed scripts `A,Б,В,Г`; OFF all-Cyrillic `А,Б,В,Г` — OFF is
arguably BETTER here) and on the gapped test's q18 (`a,b,d,e` -> `a,b,d,E`,
a case inconsistency).

**Honest mixed signal:** thinking-off was FASTER (8s vs 14s; 13.5s vs 29s) and
more *stable* run-to-run on two files (the gapped test dropped 2 questions in
one thinking-ON run but never with it off). None of that outweighs inventing
40 questions from an answer key.

**Conclusion:** grading is a transcription task and does not need reasoning;
extraction is a *structuring* task and does. Keep thinking ON for extraction.
Do not retry without a way to make the model reject non-question pages first.
