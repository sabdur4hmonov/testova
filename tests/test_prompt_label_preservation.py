"""
Guard: the extraction prompts must keep their option-LABEL-preservation rule.

BUG (Q18): a paper with options a,b,d,e (inline on one line, with inner "1)...2)..."
numbering inside the option text) was non-deterministically relabelled to A,B,C,D
by Gemini, which then rejected a valid "E" at key entry. The DB stores option
labels verbatim from Gemini, so the only defence is the prompt wording. This
guard fails if that wording is ever dropped or weakened, so the fix can't
silently regress.
"""
from app.services.ai_analyzer import VISION_PROMPT, RECOVER_OPTIONS_PROMPT


def test_vision_prompt_preserves_gapped_labels():
    # the exact gapped-labels rule (a,b,d,e with no c) must remain
    assert 'a) b) d) e)' in VISION_PROMPT
    assert '"a","b","d","e"' in VISION_PROMPT


def test_vision_prompt_forbids_abcd_renumbering_of_inline_options():
    # the inline / inner-numbering clause added for the Q18 bug
    assert "inner numbering" in VISION_PROMPT
    assert "is NOT a label" in VISION_PROMPT
    assert "renumber" in VISION_PROMPT and "A,B,C,D" in VISION_PROMPT


def test_recover_options_prompt_preserves_labels():
    # the latent relabeller (flat A,B,C,D schema) must carry the same rule
    assert "renumber" in RECOVER_OPTIONS_PROMPT
    assert "A,B,C,D" in RECOVER_OPTIONS_PROMPT
    assert "a,b,d,e" in RECOVER_OPTIONS_PROMPT
