# Bot uchun tayyor detektor, ulash keyingi bosqichda

Hozir bot handlerlari, flaglari, `.env`, Docker va ishlab turgan Postgres bazasi **o‘zgartirilmagan**. Quyidagi kod mustaqil localhost servisga ulash uchun tayyor; Telegram xabari yubormaydi. O‘zbek lotin/kirill, rus va ingliz matni uchun **150+ so‘z** kerak.

## Adapter va xabar formati

Adapter uchun `requirements-adapter.txt` (`httpx`), servis uchun `requirements.txt` yetarli. Servis va bot bir mashinada bo‘lsa URL `http://127.0.0.1:8010`; keyin compose ichida bot ishlatilsa uning tarmoqdagi servis manzili tanlanadi. `localhost` boshqa konteynerni anglatmaydi.

```python
from detector.integrations.bot_adapter import DetectorGateway, result_for_bot
from detector.integrations.presentation import messages_for_bot

gateway = DetectorGateway("http://127.0.0.1:8010", token="", timeout=60)
result = await gateway.detect(essay_text, explain=True)
view = result_for_bot(result)  # localised presentation contract, not authorship certainty
messages = messages_for_bot(result, language="uz")
# Later: send each plain-text message with parse_mode=None.
# If existing handlers use HTML/Markdown, explicitly escape all student text.
```

`messages_for_bot` to‘rt tilda xulosa/ikki o‘xshashlik bahosi, ko‘pi bilan 3 ta bo‘lak va sabablarni qaytaradi. Xabar ≤3500 belgi; uzun bo‘lakda qisqartirish belgisi bor. Handlerga ulash hozir bajarilmaydi. Eski handlerning 80-so‘z chegarasi keyin **150**ga moslashtiriladi; “AI ehtimoli” matni “SI uslubiga o‘xshashlik”ga almashtiriladi.

## HTTP contract v2

`GET /healthz`: tillar/modellar/versiyalar, `minimum_words_by_language`, `collection_enabled`, `automatic_learning_enabled`, `collection_counts`, `active_release`, contract version.

`POST /detect`: `{"text":"...","explain":true}`. Eski maydonlar saqlangan. Yangi maydonlar: `human_similarity`, `authorship_confidence` (**null**), `score_kind`, `decision_thresholds`, `explanation`.

`explanation.parts`: tozalangan va tekshirilgan matndagi `start/end/text`, 150+ so‘zli bo‘lakning bahosi, `highlight` va sabablar. Ko‘pi bilan 8 ta bo‘lak; uzun matnda qamrov/`partial` ochiq ko‘rsatiladi. Bu gapma-gap mualliflik hukmi emas. API bo‘lak bahosini butun matn bahosidan ko‘ra kamroq tekshirilgan deb belgilaydi.

- Mahalliy TF-IDF modellarida haqiqiy musbat so‘z/birikma vaznlari ko‘rsatiladi; qiymat kalibrlangan logit hissasi, mualliflik ehtimoli emas. Rus ansamblida bu **faqat mahalliy tarkibiy model** hissasi.
- Ingliz neyron modelida so‘zga sun’iy “sabab” o‘ylab topilmaydi. Bo‘lakning bahosi va ko‘pi bilan 3 bo‘lakni olib tashlagandagi matn bahosi o‘lchanadi. Removal solishtirish faqat 1200 so‘zgacha bajariladi; uzun matnda bo‘lak bahosi va o‘tkazib yuborish sababi qaytadi. Izohli so‘rovda qisqa gaplarni qo‘shimcha skor qilish o‘chiriladi. Farq kontekst sezgirligi, sababiy isbot emas; farq manfiy ham bo‘lishi mumkin.
- Rus neyron komponenti ilk 512 tokenni ko‘radi. Izohda bir bo‘lakning yuqori bahosi butun matn yoki har bir so‘z SI yozganini isbotlamaydi.

Ixtiyoriy `DETECTOR_API_TOKEN` o‘rnatilsa POSTlarda `X-Detector-Token` kerak. Kalitni chatga/logga/ZIPga qo‘ymang; mavjud `.env` bu ishda o‘zgarmagan.

## Rozilik bilan corpus yig‘ish

Oddiy `/detect` matnni saqlamaydi. Alohida rozilik tugmasi yoki botdagi alohida tanlovdan keyin:

```python
receipt = await gateway.collect(essay_text, consent=True,
    claimed_label="human", topic="Vazifa mavzusi", genre="insho")
# Save the returned withdrawal receipt for this contributor, without logging its token.
await gateway.withdraw(receipt["sample_id"], receipt["withdrawal_token"])
```

`POST /samples`: `consent` aynan boolean **true** bo‘lishi kerak. `claimed_label` human/ai/unknown — hali tekshirilmagan da’vo. SI uchun actual model ma’lum bo‘lsa `claimed_model` kiriting. Janr va mavzu alohida saqlanadi. Takror 409; bazaning bandligi 503, hajm chegarasi 507, qisqa/tilga mos kelmagan matn 422. Roziliksiz avtomatik yig‘ishga adapter yo‘l qo‘ymaydi.

Mahalliy corpus: `detector/data/feedback/corpus.sqlite3`. Faqat rozilikli matn va minimal metadata; Telegram ID, ism, telefon, `.env` va boshqa baza ma’lumoti yuborilmaydi. Shaxsiy ma’lumotni **yuborishdan oldin** olib tashlang. Maksimum 10000 yozuv / 100 mln matn belgisi. Takrorlar normallashtirilgan hash bilan to‘siladi, oldingi yuboruvchining kvitansiyasi oshkor qilinmaydi.

Rozilikli xom matn faqat SQLite corpusda saqlanadi; o‘rganuvchi work papkasiga uni ko‘chirmaydi, faqat takror/split uchun bo‘lak hashlarini yozadi. Withdrawal xom matnning alohida work nusxalarini qoldirmaydi.

Matn `pending` holatda saqlanadi; detektor bahosi yoki foydalanuvchining “odam yozgan” da’vosi avtomatik toza yorliq bo‘lmaydi. Bog‘liq foydalanuvchi roziligi uchun kelajakdagi bot oqimi kvitansiyani o‘sha foydalanuvchiga yopiq qaytarishi kerak. Brauzerda kvitansiyani yuklab olgach boshqa matn saqlash mumkin. Receipt capability’ni yo‘qotsangiz uni server hashidan tiklab bo‘lmaydi.

`POST /samples/withdraw` yangi yig‘ish o‘chirilganda ham ishlaydi. To‘g‘ri kvitansiya bilan matn va mazmunli metadata o‘chadi, kelajakdagi o‘rgatishdan chiqadi; takror/audit uchun hash qoladi. **Oldin o‘rgatilgan vaznlardan avtomatik unlearning qilinmaydi.** Buning uchun bekor qilingan namunalar chiqarilgan qayta o‘rgatish yoki boshlang‘ich modelga qaytish kerak.

## Mahalliy tekshiruvchi va o‘rganuvchi

Loyiha ota papkasidan, venv Python bilan:

```powershell
& detector/.venv/Scripts/python.exe -B -m detector.training.feedback_cli stats
& detector/.venv/Scripts/python.exe -B -m detector.training.feedback_cli pending
& detector/.venv/Scripts/python.exe -B -m detector.training.feedback_cli show SAMPLE_ID
& detector/.venv/Scripts/python.exe -B -m detector.training.feedback_cli review SAMPLE_ID --label human --evidence-kind author_confirmation --evidence 'Muallif tasdig‘i va matn tarixi alohida tekshirildi' --reviewer OWNER --rights-confirmed
& detector/.venv/Scripts/python.exe -B -m detector.training.feedback_cli review SAMPLE_ID --label ai --evidence-kind generator_record --evidence 'Provayderning haqiqiy generatsiya yozuvi tekshirildi' --reviewer OWNER --rights-confirmed --model ACTUAL_MODEL_NAME
& detector/.venv/Scripts/python.exe -B -m detector.training.feedback_cli withdraw --receipt 'C:/path/receipt.json'
```

CLI faqat ishonchli tekshiruvchi uchun. `author_confirmation` dalili haqiqatan mustaqil tekshirilgan bo‘lishi kerak; shunchaki forma belgisi yetmaydi. Odam uchun historical_source ham mumkin; SI uchun actual model va generator record kerak. Rad etish `review ... --reject`; bunday matn o‘rgatishga kirmaydi. Ochiq HTTP review endpoint yo‘q.

```powershell
& detector/start_local.ps1 -EnableCollection
& detector/start_local.ps1 -AutoLearn   # implies collection, still requires per-text consent
& detector/.venv/Scripts/python.exe -B -m detector.training.continuous once --auto-promote
& detector/.venv/Scripts/python.exe -B -m detector.training.continuous rollback --release PASSED_VERSION
& detector/.venv/Scripts/python.exe -B -m detector.training.continuous baseline
```

Oddiy launcherda yig‘ish/o‘rganuvchi o‘chiq. Hozir foydalanuvchi topshirig‘i bilan localhost namunasida ikkalasi yoqilgan, **0 approved** sabab yangi model o‘rgatilmayapti. Learner 60 soniyada tekshiradi; faqat o‘zgargan approved snapshotga ishlaydi. `.env` yoki tashqi SI xizmatini chaqirmaydi. O‘rgatish uchun `requirements-local-train.txt`, 1.25 GiB bo‘sh RAM va mavjud xususiy sealed TRAIN/VAL/TEST fayllari kerak. Paketga asl o‘quvchi matnlari qo‘shilmaydi: boshqa mashinada xususiy training split yo‘q bo‘lsa o‘rganuvchi modelni o‘zgartirmay kutadi.

O‘zbek/rus nomzodlarida asl TRAIN + approved yangi namuna; muzlatilgan VAL o‘z holicha. Ingliz nomzodi approved yangi namunalarning qat’iy mavzu guruhli TRAIN/VAL/TEST ajratmasidan kichik mahalliy model tayyorlaydi; pretrained ingliz modelini qabul sinovidan o‘tmaguncha saqlaydi. Mavzu va yaqin takrorlar guruh/split orasida aralashmaydi. Mualliflik dalili, huquq yoki rozilik o‘zgarsa eski snapshot avtomatik promote qilinmaydi.

Avtomatik almashtirish uchun **ilgari baholanmagan 100 odam + 50 SI mavzu guruhi**, 95% FP intervali yuqori chegarasi ≤5%, SI topish ≥80%, eski modeldan ko‘p odam xatosi yoki kam SI topishi bo‘lmasligi kerak. Rus uchun CoAT/LLMTrace eski regressiyalari ham yomonlashmasin. Eski tekshirilgan TEST qabul uchun yangidan ishlatilmaydi; kichik yangi guruhlar yetarli son yig‘ilguncha sarflanmaydi. Nomzod va chegara yangi TESTdan oldin muzlatiladi. Har qanday xato/yetarli dalil yo‘qligida oldingi model qoladi.

Versiyalar `data/model_updates/releases/`; hashli manifest, atomik `active.json`, so‘rovda yangi versiyani yuklash, muvaffaqiyatsiz yuklashda oxirgi yaxshi registry. Asl `models/` ustiga yozilmaydi. `rollback` faqat sinovdan o‘tgan eski bundle uchun; `baseline` dastlabki modellarga qaytaradi. Model vaznini almashtirish bilan `school_essay_accuracy_proven` avtomatik true bo‘lmaydi.

Foiz chegaralari va real cheklovlar: `data/human_confidence_report.md`. **Human >65% — uslubiy toifa; 95/99% ham haqiqiy mualliflik ishonchi emas.** Inference/o‘rganuvchi oflayn va har tekshiruv uchun API to‘lovisiz; kompyuter, elektr va kelajak hosting xarajati alohida.
