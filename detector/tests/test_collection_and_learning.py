"""Consent, provenance, fresh validation, withdrawal and release safety."""
import asyncio
import json
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from detector.service.api import create_app
from detector.service.corpus import CorpusStore, DuplicateSample, CorpusFull, normalized_hash
from detector.service.engine import Registry
from detector.service.releases import ReleaseRegistry, validate_bundle, set_active, file_hash
from detector.integrations.bot_adapter import DetectorGateway, result_for_bot
from detector.training import continuous as learning
from detector.tests.conftest import FakeScorer

TEXT = ("O‘zbek maktabida o‘quvchilar uchun yangi kutubxona ochildi va biz kitoblar haqida birga suhbat qildik. " * 14).strip()

def add(store, text=TEXT, label="human", topic="kutubxona"):
    return store.add(text=text, lang="uz", genre="insho", topic=topic, claimed_label=label,
                     claimed_model="", analysis={"ai_probability":.99,"model_version":"test"})

@pytest.fixture
def store(tmp_path): return CorpusStore(tmp_path/"feedback.sqlite3")

@pytest.fixture
def client(tmp_path,monkeypatch):
    monkeypatch.setenv("DETECTOR_COLLECTION_ENABLED","true")
    monkeypatch.setenv("DETECTOR_CORPUS_DB",str(tmp_path/"feedback.sqlite3"))
    monkeypatch.delenv("DETECTOR_API_TOKEN",raising=False)
    scorer=FakeScorer(["uz"]);scorer.min_words=150
    with TestClient(create_app(Registry([scorer]))) as c: yield c

def test_prediction_does_not_create_database(client,tmp_path):
    result=client.post("/detect",json={"text":TEXT}).json()
    assert result["human_similarity"]==1-result["ai_probability"]
    assert result["authorship_confidence"] is None
    assert result["api_contract_version"]==2
    assert not (tmp_path/"feedback.sqlite3").exists()
    assert client.get("/healthz").json()["minimum_words_by_language"]["uz"]==150

@pytest.mark.parametrize("consent",[False,None,"true",1])
def test_explicit_boolean_consent_only(client,consent):
    assert client.post("/samples",json={"text":TEXT,"consent":consent}).status_code==422

def test_missing_consent_and_short_input(client):
    assert client.post("/samples",json={"text":TEXT}).status_code==422
    assert client.post("/samples",json={"text":"va bu maktab", "consent":True}).status_code==422

def test_collect_pending_duplicate_and_withdraw(client,tmp_path):
    res=client.post("/samples",json={"text":TEXT,"consent":True,"claimed_label":"human"})
    assert res.status_code==200
    receipt=res.json(); assert receipt["used_for_training"] is False
    db=CorpusStore(tmp_path/"feedback.sqlite3")
    assert db.approved()==[] and db.stats()["pending"]==1
    duplicate=client.post("/samples",json={"text":TEXT.upper(),"consent":True})
    assert duplicate.status_code==409 and "withdrawal_token" not in duplicate.text
    bad={"sample_id":receipt["sample_id"],"withdrawal_token":"x"*32}
    assert client.post("/samples/withdraw",json=bad).status_code==404
    good={k:receipt[k] for k in bad}
    assert client.post("/samples/withdraw",json=good).json()["excluded_from_future_training"]
    assert db.show(receipt["sample_id"])["text"]=="" and db.approved()==[]

def test_collection_disabled_and_authenticated(tmp_path,monkeypatch):
    monkeypatch.setenv("DETECTOR_COLLECTION_ENABLED","false")
    monkeypatch.setenv("DETECTOR_API_TOKEN","test-secret")
    monkeypatch.setenv("DETECTOR_CORPUS_DB",str(tmp_path/"none.sqlite3"))
    with TestClient(create_app(Registry([FakeScorer(["uz"])]))) as c:
        assert c.post("/samples",json={"text":TEXT,"consent":True}).status_code==401
        assert c.post("/samples",json={"text":TEXT,"consent":True},headers={"X-Detector-Token":"test-secret"}).status_code==403
        assert c.post("/samples/review",json={}).status_code==404
    assert not (tmp_path/"none.sqlite3").exists()

def test_authorship_evidence_and_withdrawal(store):
    r=add(store)
    kwargs=dict(label="human",evidence_kind="author_confirmation",evidence="Independent author confirmation recorded",reviewer="reviewer",rights_confirmed=False)
    with pytest.raises(ValueError):store.review(r["sample_id"],**kwargs)
    kwargs.update(rights_confirmed=True,evidence_kind="prediction")
    with pytest.raises(ValueError):store.review(r["sample_id"],**kwargs)
    kwargs["evidence_kind"]="author_confirmation";store.review(r["sample_id"],**kwargs)
    assert store.approved()[0]["verified_label"]==0 # score .99 and self-claim do not label it.
    before=learning.snapshot_digest(store.approved())
    assert store.withdraw(r["sample_id"],r["withdrawal_token"])
    assert before!=learning.snapshot_digest(store.approved())
    with pytest.raises(ValueError):store.review(r["sample_id"],**kwargs)

def test_ai_requires_generator_and_actual_model(store):
    r=add(store,label="ai")
    kwargs=dict(label="ai",evidence_kind="generator_record",evidence="Provider generation record sample 42",reviewer="reviewer",rights_confirmed=True)
    with pytest.raises(ValueError):store.review(r["sample_id"],**kwargs)
    store.review(r["sample_id"],model="actual-test-generator",**kwargs)
    assert store.approved()[0]["verified_model"]=="actual-test-generator"

def test_bounded_storage_and_normalization(tmp_path):
    s=CorpusStore(tmp_path/"a.sqlite3",max_samples=1)
    add(s)
    with pytest.raises(CorpusFull):add(s,text=TEXT+" new text")
    assert normalized_hash("OʻZBEK   matni")==normalized_hash("o'zbek matni")

def test_fixed_task_groups_and_fresh_gate():
    a={"lang":"uz","topic":"Same topic","content_hash":"1"}
    b={**a,"content_hash":"2","topic":"  SAME   TOPIC "}
    assert learning.group_for(a)==learning.group_for(b)
    assert learning.holdout_group(learning.group_for(a))==learning.holdout_group(learning.group_for(b))
    positive=dict(human=100,ai=50,fpr_95_ci=[0,.0363],tpr=.8,fp=0,tp=40)
    assert learning.acceptance_gate(positive,positive)[0]
    for override in ({"human":99},{"ai":49},{"fpr_95_ci":[0,.051]}, {"tpr":.79},{"fp":1},{"tp":39}):
        assert not learning.acceptance_gate({**positive,**override},positive)[0]

def test_unreviewed_data_does_not_trigger_training(store,tmp_path,monkeypatch):
    monkeypatch.setattr(learning,"WORK",tmp_path/"work")
    add(store)
    state=learning.attempt(store,True)
    assert state["approved"]==0 and not state["active_model_changed"]
    assert state["state"]=="waiting_for_verified_pairs"
    assert learning.attempt(store,True)["state"]=="unchanged"

def test_near_duplicate_holdout_excluded():
    row=dict(id="1",lang="uz",topic="topic",content_hash="hash",text=TEXT,genre="insho",verified_label=0,verified_model=None,evidence_kind="author_confirmation")
    accepted,excluded=learning.prepare([row],[{"text":TEXT.upper()}])
    assert accepted==[] and excluded
    accepted,excluded=learning.prepare([row],[])
    assert accepted and not excluded

def make_bundle(root,name):
    folder=root/name; models=folder/"models";models.mkdir(parents=True)
    (models/"artifact").write_text(name)
    (folder/"manifest.json").write_text(json.dumps({"gate_passed":True,"artifacts":[{"file":"models/artifact","sha256":file_hash(models/"artifact")}]}))
    return folder

def test_bundle_checksums_traversal_and_unlisted(tmp_path):
    folder=make_bundle(tmp_path,"v1")
    assert validate_bundle(folder)["gate_passed"]
    (folder/"models"/"artifact").write_text("tampered")
    with pytest.raises(ValueError):validate_bundle(folder)
    folder=make_bundle(tmp_path,"v2")
    (folder/"models"/"extra").write_text("unlisted")
    with pytest.raises(ValueError):validate_bundle(folder)
    with pytest.raises(ValueError):set_active(tmp_path,"../outside")

def test_last_good_registry_reload_and_rollback(tmp_path,monkeypatch):
    root=tmp_path/"releases";baseline=tmp_path/"baseline"
    def load(path,threads,languages):return Registry([FakeScorer(["uz"],name=path.parent.name if path.name=="models" else "baseline")])
    monkeypatch.setattr(Registry,"from_dir",load)
    manager=ReleaseRegistry(baseline,root,languages={"uz"})
    first=manager.get();assert manager.active_release=="baseline"
    make_bundle(root,"v1");set_active(root,"v1")
    new=manager.get();assert new is not first and manager.active_release=="v1"
    (root/"active.json").write_text('{"release":"../bad"}')
    assert manager.get() is new and manager.active_release=="v1"
    assert manager.get() is new
    (root/"active.json").unlink()
    assert manager.get().for_language("uz").name=="baseline"

def test_incomplete_release_keeps_baseline(tmp_path,monkeypatch):
    monkeypatch.setattr(Registry,"from_dir",lambda *a:Registry([FakeScorer(["uz"])]))
    m=ReleaseRegistry(tmp_path/"baseline",tmp_path/"releases",languages={"uz","ru"})
    original=m.get();make_bundle(tmp_path/"releases","v1");set_active(tmp_path/"releases","v1")
    assert m.get() is original and m.active_release=="baseline"

def test_gateway_consent_and_presentation(monkeypatch):
    calls=[]
    async def request(self,path,payload=None):calls.append((path,payload));return {"status":"pending"}
    monkeypatch.setattr(DetectorGateway,"request",request)
    g=DetectorGateway("http://local/")
    with pytest.raises(ValueError):asyncio.run(g.collect(TEXT,consent=False))
    asyncio.run(g.collect(TEXT,consent=True,topic="topic"))
    assert calls[0][0]=="/samples" and calls[0][1]["consent"] is True
    view=result_for_bot(dict(lang="uz",verdict="likely_human",ai_probability=.02,model_version="v1"))
    assert view["human_similarity_percent"]==98 and view["authorship_confidence_percent"] is None

def test_short_response_has_no_false_percentage(client):
    data=client.post("/detect",json={"text":"va bu maktab uchun kitob"}).json()
    assert data["human_similarity"] is None and data["authorship_confidence"] is None
