"""Valid JSON from a refused response must never become a question/grade."""
import json
import pytest
from app.config import settings
from app.services import ai_analyzer as aa, book_generator as bg, sheet_reader as sr
from app.services.finish_policy import GeminiResponseRejected

Q={'q':'Which number is one?', 'opts':{'A':'one','B':'two'}, 'ans':'A'}

@pytest.mark.parametrize('reason',[0,3,4,99])
def test_generation_refuses_valid_json_with_disallowed_finish(monkeypatch,reason):
    monkeypatch.setattr(bg.gemini_rest,'call_generation',lambda *a,**k:(json.dumps([Q]),reason))
    questions,errored,error=bg._call_and_parse(theme_title='Numbers',theme_text='One is first',n_questions=1,n_written=0,option_labels=['A','B'],sample_question=None,language='en',user_id=None)
    assert questions==[] and errored and 'finish_reason=' in error

@pytest.mark.parametrize('reason',[1,2])
def test_stop_and_truncated_generation_keep_recovery_contract(monkeypatch,reason):
    monkeypatch.setattr(bg.gemini_rest,'call_generation',lambda *a,**k:(json.dumps([Q]),reason))
    questions,errored,error=bg._call_and_parse(theme_title='Numbers',theme_text='One is first',n_questions=1,n_written=0,option_labels=['A','B'],sample_question=None,language='en',user_id=None)
    assert len(questions)==1 and not errored and error is None

@pytest.mark.parametrize('reason',[0,3,99])
def test_extraction_refuses_unknown_and_safety_content(monkeypatch,reason):
    monkeypatch.setattr(aa.gemini_rest,'call',lambda *a,**k:('[]',reason))
    with pytest.raises(GeminiResponseRejected):aa.AIAnalyzer()._call_sync_multi('mock',[b'mock'])

@pytest.mark.parametrize('reason',[0,3,4,99])
async def test_grading_refuses_apparently_complete_answers(monkeypatch,reason):
    monkeypatch.setattr(settings,'GEMINI_GRADING_MAX_RETRIES',1)
    monkeypatch.setattr(sr,'_call_sync',lambda *a:('{"answers":{"1":"A"}}',reason))
    result=await sr._read_once('mock',b'mock',expected=[1])
    assert result['answers']=={} and 1 in result['unread']
