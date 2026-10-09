import pytest
from detector.integrations.presentation import messages_for_bot

@pytest.mark.parametrize("language",["uz","uz-cyrl","ru","en"])
def test_messages_are_bounded_plain_text_and_never_claim_certainty(language):
    result=dict(lang=language,ai_probability=.9,verdict="mixed",explanation={"parts":[dict(text="<untrusted & student text>"*300,ai_similarity=.99,reasons=[dict(kind="positive_linear_features",features=[dict(text="some phrase")])])]*10})
    messages=messages_for_bot(result)
    assert len(messages)==4 and all(len(m)<=3500 for m in messages)
    assert "90.0%" in messages[0] and "10.0%" in messages[0]
    assert "<untrusted & student text>" in messages[1] # Caller must use parse_mode=None.
    assert "some phrase" in messages[1]

def test_unsupported_text_has_no_percentage():
    messages=messages_for_bot(dict(lang="unknown",ai_probability=None))
    assert len(messages)==1 and "%" not in messages[0]
