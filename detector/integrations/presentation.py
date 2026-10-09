"""Plain-text bot messages, bounded for later wiring; no Telegram calls."""
LABELS={
 "uz":dict(score="SI uslubiga o‘xshashlik",human="Odam uslubiga o‘xshashlik",confidence="Mualliflik ishonchi aniqlanmagan",warning="Bu mualliflik isboti emas.",parts="Baholangan bo‘laklar",features="Modelga ta’sir qilgan so‘z/birikmalar",association="Belgilar faqat shu modelga bog‘liq; so‘zning o‘zi SI isboti emas.",removal="Bo‘lakni olib tashlagandagi SI bahosi",partial="Matn qisman tekshirildi",short="Bu matnga baho berilmadi. Kamida 150 so‘zli, bir tildagi matn yuboring."),
 "uz-cyrl":dict(score="СИ услубига ўхшашлик",human="Одам услубига ўхшашлик",confidence="Муаллифлик ишончи аниқланмаган",warning="Бу муаллифлик исботи эмас.",parts="Баҳоланган бўлаклар",features="Моделга таъсир қилган сўз/бирикмалар",association="Белгилар фақат шу моделга боғлиқ; сўзнинг ўзи СИ исботи эмас.",removal="Бўлакни олиб ташлагандаги СИ баҳоси",partial="Матн қисман текширилди",short="Бу матнга баҳо берилмади. Камида 150 сўзли, бир тилдаги матн юборинг."),
 "ru":dict(score="Сходство со стилем ИИ",human="Сходство с человеческим стилем",confidence="Уверенность в авторстве не установлена",warning="Это не доказательство авторства.",parts="Оценённые фрагменты",features="Слова/сочетания с вкладом в модель",association="Признаки зависят от модели; само слово не доказывает использование ИИ.",removal="Оценка ИИ после удаления фрагмента",partial="Текст проверен частично",short="Текст не оценён. Отправьте не менее 150 слов на одном языке."),
 "en":dict(score="AI style similarity",human="Human style similarity",confidence="Authorship confidence not established",warning="This is not proof of authorship.",parts="Analysed segments",features="Words/phrases contributing to the model",association="Associations depend on the model; a word alone is not an AI marker.",removal="AI score after removing this segment",partial="Only part of the text was analysed",short="No score. Send at least 150 words in one language.")}
VERDICTS={
 "uz":dict(likely_ai="SI uslubiga o‘xshaydi",likely_human="Odam uslubiga o‘xshaydi",mixed="Natija noaniq"),
 "uz-cyrl":dict(likely_ai="СИ услубига ўхшайди",likely_human="Одам услубига ўхшайди",mixed="Натижа ноаниқ"),
 "ru":dict(likely_ai="Похож на стиль ИИ",likely_human="Похож на человеческий стиль",mixed="Неоднозначный результат"),
 "en":dict(likely_ai="Resembles AI style",likely_human="Resembles human style",mixed="Uncertain result")}

def messages_for_bot(result, language=None, maximum_parts=3):
    """Send later with parse_mode=None, or explicitly escape if using HTML/Markdown."""
    language=language or result.get("lang")
    language=language if language in LABELS else "uz"
    labels=LABELS[language]
    score=result.get("ai_probability")
    if score is None:return [labels["short"]]
    header=f'{labels["score"]}: {score*100:.1f}%\n{labels["human"]}: {(1-score)*100:.1f}%\n{labels["confidence"]}.\n{labels["warning"]}'
    header=VERDICTS[language].get(result.get("verdict"),"")+"\n"+header
    if result.get("truncated"):header+='\n'+labels["partial"]
    output=[header]
    explanation=result.get("explanation") or {}
    parts=sorted(explanation.get("parts",[]),key=lambda p:p["ai_similarity"],reverse=True)[:max(0,min(3,maximum_parts))]
    for part in parts:
        text=f'{labels["parts"]}: {part["ai_similarity"]*100:.1f}%\n'+part["text"][:1800]+("…" if len(part["text"])>1800 else "")
        for reason in part.get("reasons",[]):
            if reason["kind"]=="positive_linear_features":
                text+='\n'+labels["features"]+': '+', '.join(f['text'][:80] for f in reason['features'][:5])+'.\n'+labels["association"]
            elif reason["kind"]=="removal_score_change":text+=f'\n{labels["removal"]}: {reason["ai_score_without_part"]*100:.1f}%'
        text+='\n'+labels["warning"]
        output.append(text[:3500])
    return output
