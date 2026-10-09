"""Exercise real offline fitting and publication with synthetic data, never student corpus."""
import hashlib
import json
import random
import pytest
from types import SimpleNamespace
import psutil
from detector.service.corpus import CorpusStore
from detector.service.engine import Registry
from detector.service.linear import LinearDetector
from detector.service.releases import validate_bundle
from detector.tests.conftest import FakeScorer
from detector.training import continuous as flow

def body(seed,label,language="uz"):
    rng=random.Random(seed)
    vocab=["".join(random.Random(i).choices("abcdefghijklmnoprstuvxyz",k=7)) for i in range(300)]
    anchors=(("mahalla ustoz daftar maktab kitob", "tahlil xulosa istiqbol taraqqiyot delve") if language=="uz" else ("village school teacher books personal", "analysis summary future solution delve"))[label].split()
    prefix="O‘zbek tilida bu matn uchun biz quyidagi misollarni ko‘rib chiqamiz. " if language=="uz" else "The teachers and the children were in the school and they read books together. "
    return prefix+" ".join(rng.choice(anchors if i%3==0 else vocab) for i in range(180))+"."

def baseline_models(root):
    for lang,name in flow.FOLDERS.items():
        folder=root/name;folder.mkdir(parents=True);(folder/"fixture.txt").write_text(lang)

@pytest.mark.parametrize("language",["uz","en"])
def test_real_fit_freeze_publish_and_new_cohort_only(tmp_path,monkeypatch,language):
    d=tmp_path/"detector";split=d/"split";split.mkdir(parents=True)
    monkeypatch.setattr(flow,"D",d);monkeypatch.setattr(flow,"SPLIT",split)
    monkeypatch.setattr(flow,"WORK",d/"work");monkeypatch.setattr(flow,"RELEASES",d/"releases")
    monkeypatch.setattr(psutil,"virtual_memory",lambda:SimpleNamespace(available=4*(1<<30)))
    rows=[]
    for label in (0,1):
        for i in range(12):
            text=body(10000+label*100+i,label,language)
            # English must obtain its VAL from the fixed feedback split, not a fixture VAL.
            if language=="uz":rows.append(dict(id=f"val-{label}-{i}",doc_id=f"val-{label}-{i}",group=f"v{label}-{i}",lang=language,label=label,genre="insho",words=195,text=text))
    for name,data in (("val.jsonl",rows),("test.jsonl",[]),("train_aug.jsonl",[])):
        (split/name).write_text("".join(json.dumps(r,ensure_ascii=False)+"\n" for r in data),encoding="utf8")
    baseline_models(d/"models")
    class Champion(FakeScorer):
        def __init__(self,lang):super().__init__([lang]);self.dir=d/"models"/flow.FOLDERS[lang]
    monkeypatch.setattr(flow,"ReleaseRegistry",lambda *a,**k:SimpleNamespace(get=lambda:Registry([Champion(lang) for lang in flow.FOLDERS])))
    store=CorpusStore(d/"corpus.sqlite3")
    # Same split rules as real use; holdout is large enough for the predeclared gate.
    counts={(label,role):0 for label in (0,1) for role in ("test","train","val")}
    targets={(0,"test"):100,(1,"test"):50,(0,"train"):30,(1,"train"):30,(0,"val"):8 if language=="en" else 0,(1,"val"):8 if language=="en" else 0}
    for label in (0,1):
        i=0
        while any(counts[label,h]<targets[label,h] for h in ("test","train","val")):
            topic=f"independent synthetic task {label} {i}";i+=1
            row={"lang":language,"topic":topic,"content_hash":""};group=flow.group_for(row)
            role="test" if flow.holdout_group(group) else "val" if language=="en" and flow.validation_group(group) else "train"
            if counts[label,role]>=targets[label,role]:continue
            receipt=store.add(text=body(label*100000+i,label,language),lang=language,genre="insho",topic=topic,claimed_label="unknown",claimed_model="",analysis={})
            store.review(receipt["sample_id"],label="ai" if label else "human",evidence_kind="generator_record" if label else "author_confirmation",evidence="Synthetic software test fixture only",reviewer="test",rights_confirmed=True,model="synthetic-fixture" if label else "")
            counts[label,role]+=1
    result=flow.attempt(store,auto_promote=True)
    assert result["state"]=="promoted" and result["active_model_changed"]
    release=flow.RELEASES/result["candidate"];manifest=validate_bundle(release)
    assert manifest["changed_languages"]==[language]
    candidate=LinearDetector(release/"models"/flow.FOLDERS[language])
    assert candidate.probability(body(900001,1,language))>candidate.threshold
    assert candidate.probability(body(900002,0,language))<.35
    report=json.loads((flow.WORK/result["candidate"]/"report.json").read_text())
    assert report["languages"][language]["fresh_group_holdout"]["human"]==100
    assert report["languages"][language]["fresh_group_holdout"]["ai"]==50
    assert len(json.loads((flow.WORK/"evaluated_holdout_groups.json").read_text()))==150
    for name in ("train.jsonl","fresh_holdout.jsonl","validation.jsonl"):
        for line in (flow.WORK/result["candidate"]/name).read_text().splitlines():
            record=json.loads(line)
            assert "text" not in record and "topic" not in record
            assert "shingle_hashes" in record
    before=(flow.RELEASES/"active.json").read_bytes()
    # One extra training example cannot reuse the 150 previously evaluated tasks.
    r=store.add(text=body(800000,0,language),lang=language,genre="insho",topic="new task after promotion",claimed_label="unknown",claimed_model="",analysis={})
    store.review(r["sample_id"],label="human",evidence_kind="author_confirmation",evidence="Synthetic software test fixture only",reviewer="test",rights_confirmed=True)
    later=flow.attempt(store,auto_promote=True)
    assert later["state"]=="waiting_for_fresh_independent_groups" and not later["active_model_changed"]
    assert (flow.RELEASES/"active.json").read_bytes()==before
