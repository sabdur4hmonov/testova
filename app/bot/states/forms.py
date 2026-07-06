from aiogram.fsm.state import State, StatesGroup


class UploadStates(StatesGroup):
    waiting_for_file = State()
    waiting_for_section_choice = State()
    waiting_for_answers = State()
    waiting_for_variant_count = State()


class CheckingStates(StatesGroup):
    waiting_for_project = State()
    waiting_for_answer_sheet = State()
    waiting_for_variant_number = State()


class SettingsStates(StatesGroup):
    waiting_for_language = State()
