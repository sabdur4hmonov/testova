from aiogram.fsm.state import State, StatesGroup


class UploadStates(StatesGroup):
    waiting_for_file = State()
    waiting_for_format = State()  # choose Oddiy / Ixcham before extraction
    waiting_for_answers = State()
    waiting_for_dup_resolution = State()
    waiting_for_variant_count = State()


class CheckingStates(StatesGroup):
    # Saved-project grading flow (unchanged).
    waiting_for_project = State()
    waiting_for_answer_sheet = State()
    waiting_for_variant_number = State()
    # Manual "Javob orqali tekshirish" flow.
    choosing_check_mode = State()      # picking Saqlangan vs Javob orqali
    waiting_for_key = State()          # teacher types the answer key
    waiting_for_key_confirm = State()  # echo shown, confirm / re-enter
    waiting_for_manual_sheet = State() # awaiting a student answer-sheet photo


class BuilderStates(StatesGroup):
    """Multi-Source Test Builder."""
    waiting_for_file = State()
    waiting_for_answers = State()
    waiting_for_next_action = State()
    waiting_for_builder_format = State()  # Oddiy / Ixcham, asked at finish
    waiting_for_variant_count = State()
    waiting_for_question_count = State()
    waiting_for_reuse_confirm = State()
    waiting_for_save_choice = State()


class SettingsStates(StatesGroup):
    waiting_for_language = State()
