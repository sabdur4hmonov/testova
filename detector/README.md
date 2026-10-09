# Testova: to‘rt tilda mahalliy SI matn detektori

O‘zbek lotin, o‘zbek kirill, ruscha va inglizcha matnlarni tekshiradigan mustaqil servis va brauzer sahifasi. Kamida **150 so‘z** kerak. Natija statistik o‘xshashlik bahosi; odam yoki SI muallifligini isbotlamaydi. Maktab esselaridagi mustaqil aniqlik hali tasdiqlanmagan.

Model vaznlari, corpus va xom matnlar Git’ga kirmaydi. Ushbu noutbukdagi
`detector/models/` saqlanadi; yangi Git checkout uchun model bundle’lari
alohida ta’minlanishi yoki huquqi tasdiqlangan ma’lumot bilan qayta yaratilishi
kerak. Kod testlarining yashilligi model bundle mavjudligini anglatmaydi.

## Ishga tushirish

Hozirgi worktree uchun PowerShell:

```powershell
& '<repo>\detector\start_local.ps1'
```

Brauzer: **http://127.0.0.1:8010**. TXT fayl ochish yoki matn joylash mumkin. Interfeysning to‘rt tili bor. To‘xtatish: terminalda Ctrl+C. Boshqa port: `start_local.ps1 -Port 8011`.

ZIP yangi joyga ochilsa, `detector/` papkasining ota papkasida Python 3.11 bilan:

```powershell
py -3.11 -m venv detector/.venv
& detector/.venv/Scripts/python.exe -m pip install -r detector/requirements.txt
& detector/start_local.ps1
```

Birinchi o‘rnatishda kutubxonalarni yuklash uchun internet kerak. Keyingi matn tekshiruvlari **mahalliy, API kalitisiz va tekshiruv uchun to‘lovsiz** ishlaydi. Kompyuter/server va elektr xarajati bundan alohida. Ishga tushirish `.env` o‘qimaydi, bot yoki Docker/bazani yoqmaydi. Faqat loopback manziliga ulanadi.

## Faol modellar

| Til | Papka | Ishlash usuli |
|---|---|---|
| O‘zbek lotin | `models/uz-latin-linear` | Mahalliy TF-IDF + logistik klassifikator; esse TRAIN juftlari qo‘shilgan |
| O‘zbek kirill | `models/uz-cyrillic-linear` | Mahalliy TF-IDF + logistik klassifikator |
| Rus | `models/ru-ensemble` | Mahalliy klassifikator va MIT litsenziyali Mini-FRIDA ONNX ansambli |
| Ingliz | `models/en-tmr` | Mavjud MIT TMR INT8 ONNX modeli; mahalliy mualliflik kalibrlashi tasdiqlanmagan |

Ruscha neyron model `toiletsandpaper/russian-ai-text-detector-mini-frida`, revision `2dad4ea50f8b2c5eb3b08d64c2472ba52bab179c`. Nashriyotning normallashtirishi qo‘llanadi. Neyron qism faqat dastlabki **512 token**ni ko‘radi; mahalliy qism uzun matn bo‘laklarini jamlaydi. Javob va sahifa qisman tekshirishni ko‘rsatadi.

Matn qisqa/aralash yozuvli bo‘lsa foiz berilmaydi. Qisqa gaplarga alohida ball berilmaydi. `likely_human` — past SI bahosi, `mixed` — noaniq, `likely_ai` — muzlatilgan chegaradan yuqori. Ball real dunyodagi mualliflik ehtimoli sifatida kalibrlanmagan. ONNX va NumPy yetarli; inference uchun Torch, GPU, Gemini yoki sklearn kerak emas.

## Ma’lumot va sinov chegaralari

Asl to‘plam: **1007 odam TXT**, shundan 900 tasi tarixiy/manba sharti tekshirilgan, 107 tasi muallifligi mustaqil tekshirilmagan Telegram matni; **300 haqiqiy SI TXT**, har tilda 100. Asl fayllar o‘zgarmagan. Yangi esse SI namunalari `data/ai_essay_supplement/`, alohida metadata va autentifikatsiyalangan provayder dalili bilan saqlanadi.

Telegram esse juftlari zaif inson yorlig‘iga ega va **faqat TRAIN**da. Uch bo‘lakdagi mavzu guruhlari ajratilgan; VAL/TESTga noma’lum mualliflik yoki OCR qoralamasi qo‘shilmagan. 357 rasm OCR qilindi, lekin 196 birlashtirilgan qoralama vizual tekshiruvsiz o‘rgatishda ishlatilmaydi. OCR testlari ushbu Windows muhitida Tesseract yo‘qligi sabab o‘tkazib yuboriladi; hozir tayyor oqim — TXT/matn.

O‘rgatish uchun boshqa odam TXTlari APIga yuborilmadi: SI juftlariga faqat mavzu, janr, til, uslub va uzunlik berildi. Avvalgi OCR va SI sifat ko‘rigi alohida foydalanuvchi ruxsati bilan bajarilgan.

Yakuniy o‘lchovlar: `data/detector_ready_report.md`, `models/uz_essay_model_report.json`, `models/ru_ensemble_report.json`, `models/local_training_report.json`. Oldin ko‘rilgan TEST qayta ishlatilgani sabab bu natijalar yangi mustaqil qabul sinovi emas. Rus tashqi sinovlari turli generatorlarni qamraydi; o‘zbek modellarining boshqa generatorlar va maktab esselariga umumlashishi, populyatsiyada <5% noto‘g‘ri SI xulosasi isbotlanmagan. Sana/manba huquqlari: `data/human/open_sources.csv` va `data/human_collection_report.md`.

## API

| Yo‘l | Javob |
|---|---|
| `GET /` | Uch tildagi brauzer sahifasi |
| `GET /healthz` | Yuklangan modellar va mavjud OCR tillari |
| `POST /detect` — `{"text":"..."}` | Til, SI bahosi, xulosa, model versiyasi, so‘zlar, qisman tekshirish |
| `POST /samples` — `{text,consent:true,...}` | Yig‘ish yoqilganda explicit consent, pending receipt; avtomatik toza yorliq emas |
| `POST /samples/withdraw` | Receipt bilan kelajak trainingdan chiqarish va matnni o‘chirish |
| `POST /ocr` — multipart `files=` | Tesseract o‘rnatilgan muhitda bosma matn OCR |

Ixtiyoriy `DETECTOR_API_TOKEN` bo‘lsa, POST so‘rovi `X-Detector-Token` talab qiladi. Mahalliy sahifa tokenni faqat joriy xotirasida ushlaydi. Oddiy tekshiruv matnni saqlamaydi va tashqi xizmatga yuborilmaydi. Alohida rozilikli corpus yuborishi mahalliy SQLite bazaga saqlaydi. `DETECTOR_LANGUAGES=uz,uz-cyrl,ru,en` ishga tushirishda boshqa modellarning RAM sarfini cheklaydi. Mahalliy launcher to‘rt tilni yuklaydi.

## Qayta o‘rgatish va testlar

O‘rgatish paketlari runtime’dan alohida: `requirements-local-train.txt`. Mavjud muhrlangan `data/work/collection_input/work/split/` fayllarini qayta bo‘lish kerak emas.

```powershell
# Uch dastlabki mahalliy modelni qayta yaratish (yakuniy modellarning ustiga YOZMANG):
& detector/.venv/Scripts/python.exe -B -m detector.training.train_local --out-dir detector/data/work/reproduction/models
# Faqat lotin esse modelini moslash — metadata/provayder auditi oldin o‘tishi kerak:
& detector/.venv/Scripts/python.exe -B detector/data/adapt_essay_model.py
# Faol rus ansamblining kalibratsiya va muzlatilgan sinov skripti:
& detector/.venv/Scripts/python.exe -B detector/data/work/local_training/fit_ru_ensemble.py
# Relevant servis testlari:
& detector/.venv/Scripts/python.exe -B -m pytest detector/tests -q -p no:cacheprovider
```

`train_local` umumiy modeli ansambl yoki esse modelini almashtirishi mumkin; alohida `--out-dir` ishlating. Model fayllari gitga kirmaydi. Asl korpusni, `.env`ni, bazani yoki botni o‘zgartirish bu servisni ishga tushirish uchun kerak emas. Loyihaga commit qilinmagan.

## Bot, corpus va model yangilash

`BOT_INTEGRATION.md` — adapter, to‘rt tilli xabarlar, rozilik, tekshiruvchi CLI, oflayn learner va rollback. Botga ulash hozir bajarilmagan. Yig‘ish uchun `start_local.ps1 -EnableCollection`; approved ma’lumotdan qat’iy sinovli avtoyangilash uchun `-AutoLearn`. Oddiy launcherda ikkalasi o‘chiq. Hozir localhost demo ikkalasi yoqilgan, 0 approved sabab yangi haqiqiy model tayyorlanmagan.

`POST /detect`da `explain:true` bo‘lsa 150+ so‘zli bo‘laklar, yuqori SI bahosi va modelga ta’sir qilgan belgilar chiqadi. Ko‘pi bilan 8 bo‘lak; ingliz neyron modeli uchun ko‘pi bilan 3 removal solishtirishi. Izohlar mualliflik isboti emas. API `ai_probability` eski contract nomi saqlangan, ma’nosi model similarity; `human_similarity=1-ai_probability`, `authorship_confidence=null`. Odam uslubi >65% toifa; 95/99% ham mualliflik kafolati emas. Real cheklovlar `data/human_confidence_report.md` va `data/bot_learning_report.md`da.

Ingliz manba/litsenziyasi: https://huggingface.co/Oxidane/tmr-ai-text-detector — publisher MIT deb belgilagan; mahalliy export SHA-256 bilan muzlatilgan, oldingi exportning upstream revisioni yozilmagan. Nashriyot benchmarki mustaqil mahalliy test deb ko‘rsatilmaydi.
