"""Bounded local explanations: observed segment scores and actual linear contributions."""
import math
import re
from collections import Counter
import numpy as np
from detector.service.lang import detect_language
from detector.service.linear import feature_tokens

def positive_features(model,text,limit=5):
    linear=getattr(model,"linear",model)
    if not hasattr(linear,"vocabulary"):return []
    counts=Counter(feature_tokens(text))
    pairs=[(token,linear.vocabulary[token],count) for token,count in counts.items() if token in linear.vocabulary]
    values=np.array([(1+math.log(count))*linear.idf[index] for _,index,count in pairs])
    norm=float(np.linalg.norm(values))
    if not norm:return []
    output=[]
    for (token,index,_),value in zip(pairs,values):
        weight=float(value*linear.weights[index]*linear.calibration_a/norm)
        if token.startswith(("w1:","w2:")) and weight>0:
            output.append({"text":token[3:],"calibrated_margin_contribution":round(weight,6)})
    return sorted(output,key=lambda r:r["calibrated_margin_contribution"],reverse=True)[:limit]

def explain_parts(text,model,lang,whole_score,max_parts=8):
    matches=list(re.finditer(r"[^\W\d_]+(?:['ʻʼ‘’\x60][^\W\d_]+)*",text))
    n=len(matches)
    if n<150:return None
    count=max(1,math.ceil(n/300))
    boundaries=[round(i*n/count) for i in range(count+1)]
    indices=list(range(count)) if count<=max_parts else sorted({round(i*(count-1)/(max_parts-1)) for i in range(max_parts)})
    parts=[]
    for i in indices:
        a,b=boundaries[i:i+2]
        start=0 if a==0 else matches[a].start()
        end=len(text) if b==n else matches[b].start()
        fragment=text[start:end]
        if detect_language(fragment)!=lang:continue
        score=whole_score if count==1 else model.score_document(fragment)[0]
        reasons=[{"kind":"segment_model_score","ai_similarity":round(score,4)}]
        features=positive_features(model,fragment)
        if features:reasons.append({"kind":"positive_linear_features","component":"local_linear" if hasattr(model,"linear") else "whole_linear","features":features})
        parts.append({"text":fragment,"start":start,"end":end,"words":b-a,"ai_similarity":round(score,4),"highlight":score>=model.threshold,"reasons":reasons})
    # Neural models have no faithful token-weight explanation here. Removing up
    # to three high-scoring segments measures their observed contextual influence.
    if not hasattr(model,"vocabulary") and not hasattr(model,"linear") and count>1 and n<=1200:
        for part in sorted(parts,key=lambda r:r["ai_similarity"],reverse=True)[:3]:
            if not part["highlight"]:continue
            remaining=text[:part["start"]]+text[part["end"]:]
            if detect_language(remaining)!=lang:continue
            without=model.score_document(remaining)[0]
            part["reasons"].append({"kind":"removal_score_change","ai_score_without_part":round(without,4),"whole_minus_without":round(whole_score-without,4)})
    return {"method":"segment_scores_and_positive_linear_weights_or_bounded_removal","parts":parts,
            "minimum_part_words":150,"maximum_parts":max_parts,"removal_maximum_document_words":1200,"removal_skipped_reason":"text_over_1200_words" if n>1200 and not hasattr(model,"vocabulary") and not hasattr(model,"linear") else None,"coverage_words":sum(p["words"] for p in parts),
            "analysed_words":n,"partial":len(parts)<count,"scope":"cleaned_analysed_text",
            "warning":"Model associations and removal sensitivity are not authorship proof or universal AI markers. Segment scores are less validated than full-document scores; Russian segment scores include its head-token neural component."}
