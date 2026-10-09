from aiogram.fsm.state import State, StatesGroup


class UploadStates(StatesGroup):
    waiting_for_test_name = State()  # name FIRST, before file/format (held in FSM)
    waiting_for_file = State()
    waiting_for_format = State()  # choose Oddiy / Ixcham before extraction
    waiting_for_answers = State()
    waiting_for_delete_confirm = State()  # "23: -" → confirm delete of that question
    waiting_for_dup_resolution = State()
    waiting_for_variant_count = State()


class CheckingStates(StatesGroup):
    # Saved-project grading flow (unchanged).
    waiting_for_project = State()
    # CP-5: the project has more than one printed edition — which one is graded.
    waiting_for_edition = State()
    waiting_for_answer_sheet = State()
    waiting_for_variant_number = State()
    # Manual "Javob orqali tekshirish" flow.
    choosing_check_mode = State()      # picking Saqlangan vs Javob orqali
    waiting_for_key = State()          # teacher types the answer key
    waiting_for_key_confirm = State()  # echo shown, confirm / re-enter
    waiting_for_manual_test_name = State()  # name this checking session (or /skip)
    waiting_for_manual_sheet = State() # awaiting a student answer-sheet photo
    # Optional per-sheet student-name prompt when the photo has no caption.
    waiting_for_saved_name = State()   # saved flow: student name before grading
    waiting_for_manual_name = State()  # manual flow: student name before grading
    # Per-question To'g'ri/Xato confirmation for reads the AI was unsure about.
    waiting_for_confirm = State()      # manual flow: confirm an unsure answer


class BuilderStates(StatesGroup):
    """Multi-Source Test Builder."""
    waiting_for_test_name = State()  # name FIRST, before any file
    waiting_for_file = State()
    waiting_for_answers = State()
    waiting_for_delete_confirm = State()  # "23: -" → confirm delete (Piece 3b)
    waiting_for_next_action = State()
    waiting_for_builder_format = State()  # Oddiy / Ixcham, asked at finish
    waiting_for_variant_count = State()
    waiting_for_question_count = State()
    waiting_for_reuse_confirm = State()
    waiting_for_save_choice = State()


class ExamTimerStates(StatesGroup):
    """Optional exam-timer offer after variants are sent (Variant yaratish)."""
    choosing_offer = State()        # "set an exam time?" Ha / Yo'q
    choosing_mode = State()         # start+duration / end-time
    waiting_for_start_time = State()
    waiting_for_duration = State()
    waiting_for_end_time = State()
    waiting_for_confirm = State()   # echo computed end, confirm / cancel


class SettingsStates(StatesGroup):
    waiting_for_language = State()


class BroadcastStates(StatesGroup):
    """Admin broadcast: confirm before sending to everyone."""
    confirming = State()  # gate shown; pending text held in FSM data


class BookStates(StatesGroup):
    """AI Textbook Library (Checkpoint 4).

    Covers adding a book (TOC-detected or manual theme entry), the
    "Eski kitoblardan" path (pick a saved book, pick themes, extract and save
    their text), and Phase 3 question generation from the prepared themes,
    ending in a downloadable variants PDF + answer key.
    """
    waiting_for_title = State()             # book title, asked before the file
    waiting_for_title_search = State()      # matches found for that title
    waiting_for_subject = State()           # optional, "/skip" allowed
    waiting_for_grade = State()             # optional, "/skip" allowed
    waiting_for_pdf = State()
    waiting_for_duplicate_choice = State()  # MD5/title match found in the library
    waiting_for_toc_confirm = State()       # Ha, saqlash / Tahrirlash / Bekor
    waiting_for_public_choice = State()     # umumiy kutubxonaga / faqat men
    # TOC not found: the teacher photographs the printed contents page(s)
    # (primary) or falls back to typing themes one at a time (short books only).
    waiting_for_toc_photos = State()
    # "Tahrirlash" on an ALREADY-detected list: correct it in place.
    waiting_for_toc_edit = State()
    # A split/merge sent without an inline "= Nom" — the pending op waits here
    # for its title.
    waiting_for_edit_title = State()
    # Manual theme entry, one at a time
    waiting_for_manual_title = State()
    waiting_for_manual_pages = State()      # "10-24"
    waiting_for_manual_more = State()       # add another theme, or finish
    # "📚 Eski kitoblardan"
    waiting_for_book_choice = State()
    # Item 2: "📄 By page number" — the teacher types PRINTED page ranges
    # ("1-23, 34-49"); those pages become synthetic themes fed to generation,
    # independent of any TOC. The fallback state confirms using the raw PDF file
    # page order when no printed page number can be detected.
    waiting_for_page_range = State()
    # "📚 Bir nechta kitobdan": after ONE book's page ranges are prepared,
    # the teacher either adds another book (looping back to
    # waiting_for_book_choice, accumulating themes across books) or proceeds to
    # generation. Only the multi-book flow uses this hub; the single-book flow
    # goes straight to waiting_for_generate_count as it always has.
    #
    # Rebuilt on the PAGE-RANGE architecture. CP25's version accumulated TOPIC
    # themes and died with the TOC flow; this one accumulates the synthetic
    # is_page_range themes each book's page-range prompt produces.
    waiting_for_multibook_action = State()
    waiting_for_pagerange_offset_fallback = State()
    # Phase 3: themes are extracted and ready, ask how many questions to
    # generate from them.
    waiting_for_generate_count = State()
    # CP23: how many of those questions should be WRITTEN (open-ended) vs
    # multiple-choice — asked right after the total, before generation. When
    # all questions are written (n_mc == 0) the label pickers below are skipped.
    waiting_for_written_count = State()
    # CP20: Oson/O'rta/Qiyin, asked right after the count and BEFORE the
    # Gemini call fires — difficulty shapes the generation prompt itself, so
    # (unlike the PDF format choice) it cannot be deferred to after approval.
    # CP31a: confirm the auto-detected book KIND (narrative / workbook), asked
    # once after the TOC/theme list is confirmed and before the public choice.
    waiting_for_book_kind = State()
    waiting_for_book_difficulty = State()
    # CP30: question-STEM language (source / uz / ru / en). Shapes the prompt, so
    # asked before the Gemini call, right after difficulty. "Source" (the default)
    # keeps today's behaviour; a different choice writes stems in that language
    # while options + answer stay in the source language (grounding-safe).
    waiting_for_question_language = State()
    # CP22: option-label STYLE (Latin/Cyrillic/numeric) then COUNT (3-8) — both
    # shape the generation prompt, so they are asked before the Gemini call,
    # right after difficulty.
    waiting_for_book_labelstyle = State()
    waiting_for_book_optcount = State()
    # CP24: opt-in for image-based questions. Default (no) captures nothing and
    # costs exactly what today costs — the last pre-generation step, so both the
    # normal and all-written paths funnel through it.
    waiting_for_book_images = State()
    # CP17: a generated batch is on screen; approve / regenerate / cancel.
    waiting_for_generation_review = State()
    # CP22 Part B — approved, now collect what a real saved test needs, in the
    # SAME order as the "Variant yaratish" flow: test name -> number of variants
    # -> Oddiy/Ixcham. The answer-key prompt is skipped (generation already knows
    # the answers). The final step persists a real Project + Questions + Variants
    # so the test appears in "Test tekshirish -> Saqlangan testlar".
    waiting_for_book_testname = State()
    # Approved — ask how many shuffled variants to print AND save.
    waiting_for_variant_copies = State()
    # CP19: Oddiy/Ixcham, same "fmt:" choice as the other two PDF flows. Now the
    # LAST pre-save step (CP22 moved it after name+count), so it also triggers
    # the persist.
    waiting_for_book_format = State()
    # "⚙️ Mavzuni boshqarish" — editing a SAVED book's themes. Separate from the
    # pre-save editor above: edits here are written back to the DB, so they need
    # their own save/discard step.
    waiting_for_manage_edit = State()
    waiting_for_manage_title = State()      # split/merge sent without a title
    # "🌐 Umumiy kutubxona" — browsing/filtering the public library
    waiting_for_public_browse = State()
    waiting_for_public_search = State()     # typing a title to search the library
