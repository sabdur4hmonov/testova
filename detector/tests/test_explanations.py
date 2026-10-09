import numpy as np
from types import SimpleNamespace
from detector.service.explain import positive_features, explain_parts
from detector.service.linear import feature_tokens
from detector.tests.conftest import FakeScorer

def test_only_actual_positive_word_features_explained():
    tokens=list(dict.fromkeys(feature_tokens("kitob va ilm")))
    weights=np.zeros(len(tokens));weights[tokens.index("w1:kitob")]=2;weights[tokens.index("w1:ilm")]=-1
    model=SimpleNamespace(vocabulary={t:i for i,t in enumerate(tokens)},idf=np.ones(len(tokens)),weights=weights,calibration_a=1)
    evidence=positive_features(model,"kitob va ilm")
    assert [f["text"] for f in evidence]==["kitob"]
    assert evidence[0]["calibrated_margin_contribution"]>0

def test_segments_are_bounded_and_have_precise_spans():
    model=FakeScorer(["en"])
    text=("The children and the teachers were in the school and they delve into their new topic together. "*60).strip()
    e=explain_parts(text,model,"en",.9,max_parts=3)
    assert len(e["parts"])==3 and e["partial"]
    assert all(p["words"]>=150 and text[p["start"]:p["end"]]==p["text"] for p in e["parts"])
    assert all(p["highlight"] for p in e["parts"])
    assert any(r["kind"]=="removal_score_change" for p in e["parts"] for r in p["reasons"])
    assert e["coverage_words"]<e["analysed_words"]
    long=explain_parts(text*3,model,"en",.9,max_parts=3)
    assert long["removal_skipped_reason"]=="text_over_1200_words"
    assert not any(r["kind"]=="removal_score_change" for p in long["parts"] for r in p["reasons"])

def test_short_fragment_never_judged():
    assert explain_parts("the children were at school",FakeScorer(["en"]),"en",.9) is None

def test_single_fragment_uses_document_score():
    class Model(FakeScorer):
        def score_document(self,text): raise AssertionError("single part must reuse original score")
    text=("The children and teachers were in the school with their books. "*15).strip()
    e=explain_parts(text,Model(["en"]),"en",.2)
    assert len(e["parts"])==1 and e["parts"][0]["ai_similarity"]==.2
    assert not e["parts"][0]["highlight"]
