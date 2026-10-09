"""Step 4: synthetic OCR noise, so photographed texts do not fool the detector.

    python -m detector.training.augment_ocr [--fraction 0.3 --intensity 0.05]

Texts that reach the detector through /ocr carry Tesseract's mistakes. If the
model only ever saw clean text it could learn "noisy = human" (or the
reverse) — a shortcut that would turn every photographed essay into a
verdict about the camera. So the SAME share of human and of AI training
texts gets a noisy copy:

* o' / g' written every way people and OCR write them: o' oʻ o’ o` o‘, or
  the apostrophe lost entirely ("ogil"), plus lost tutuq belgisi (ma'no -> mano)
* letter confusions: rn<->m, l<->1<->I, O<->0, e<->c, h<->b, ...
* Uzbek Cyrillic letters read as Russian ones: ў->у, қ->к, ғ->г, ҳ->х
* Latin/Cyrillic look-alikes swapped (a/а, e/е, o/о, p/р, c/с, x/х)
* line-end hyphenation ("mak-\\ntab"), merged and split words

Outputs (work/split/):
    train_aug.jsonl  train + noisy copies (id suffix "~ocr", same group)
    test_ocr.jsonl   a noisy copy of the WHOLE test set — for measuring
                     robustness only; it is derived from sealed test groups and
                     never goes into training.
"""
from __future__ import annotations

import argparse
import random
import re
from pathlib import Path

from detector.training.common import read_jsonl, work_dir, write_jsonl

APOSTROPHES = ["'", "ʻ", "’", "`", "‘", ""]
LATIN_CONFUSIONS = {
    "rn": "m", "m": "rn", "l": "1", "I": "l", "1": "l", "O": "0", "0": "O",
    "e": "c", "c": "e", "h": "b", "b": "h", "S": "5", "B": "8", "q": "g",
    "g": "q", "u": "v", "i": "í",
}
CYRILLIC_CONFUSIONS = {
    "ў": "у", "қ": "к", "ғ": "г", "ҳ": "х", "Ў": "У", "Қ": "К", "Ғ": "Г", "Ҳ": "Х",
    "и": "н", "н": "и", "ш": "щ", "е": "с", "п": "л", "ы": "ь", "з": "э",
}
HOMOGLYPHS = {"a": "а", "e": "е", "o": "о", "p": "р", "c": "с", "x": "х", "y": "у",
              "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "х": "x", "у": "y"}
_OG = re.compile(r"([oOgG])['ʻʼ‘’`]")
_TUTUQ = re.compile(r"(?<=[a-z])['ʻʼ‘’`](?=[a-z])")


def _confuse_word(word: str, rng: random.Random) -> str:
    table = CYRILLIC_CONFUSIONS if re.search(r"[а-яёўқғҳ]", word, re.I) else LATIN_CONFUSIONS
    keys = [k for k in table if k in word]
    if not keys:
        return word
    k = rng.choice(keys)
    pos = [m.start() for m in re.finditer(re.escape(k), word)]
    i = rng.choice(pos)
    return word[:i] + table[k] + word[i + len(k):]


def add_noise(text: str, rng: random.Random, intensity: float = 0.05) -> str:
    # 1. o'/g' variants: one style per document (a scanner is consistent),
    #    sometimes mixed with occasional losses.
    style = rng.choice(APOSTROPHES)
    text = _OG.sub(lambda m: m.group(1) + (style if rng.random() > intensity else ""), text)
    if rng.random() < 0.5:
        text = _TUTUQ.sub(lambda m: "" if rng.random() < 0.5 else m.group(0), text)

    out: list[str] = []
    tokens = re.split(r"(\s+)", text)
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if not tok or tok.isspace():
            out.append(tok)
            i += 1
            continue
        r = rng.random()
        if r < intensity:
            tok = _confuse_word(tok, rng)
        elif r < intensity * 1.4:
            tok = "".join(HOMOGLYPHS.get(ch, ch) if rng.random() < 0.3 else ch for ch in tok)
        elif r < intensity * 1.7 and len(tok) > 6:
            cut = rng.randint(3, len(tok) - 3)
            tok = tok[:cut] + "-\n" + tok[cut:]
        elif r < intensity * 1.9 and i + 2 < len(tokens):
            tok = tok + tokens[i + 2]  # merged with the next word
            i += 2
        elif r < intensity * 2.1 and len(tok) > 5:
            cut = rng.randint(2, len(tok) - 2)
            tok = tok[:cut] + " " + tok[cut:]
        out.append(tok)
        i += 1
    return "".join(out)


def run(data_dir: Path | None, fraction: float, intensity: float, seed: int) -> dict:
    split = work_dir(data_dir) / "split"
    rng = random.Random(seed)
    train = list(read_jsonl(split / "train.jsonl"))
    noisy = []
    for label in (0, 1):
        items = [r for r in train if r["label"] == label]
        k = round(len(items) * fraction)
        for r in rng.sample(items, k):
            noisy.append({**r, "id": r["id"] + "~ocr", "text": add_noise(r["text"], rng, intensity),
                          "source": r["source"] + "+ocr"})
    write_jsonl(split / "train_aug.jsonl", train + noisy)
    test = list(read_jsonl(split / "test.jsonl"))
    test_rng = random.Random(seed + 1)
    write_jsonl(split / "test_ocr.jsonl", [
        {**r, "id": r["id"] + "~ocr", "text": add_noise(r["text"], test_rng, intensity),
         "source": r["source"] + "+ocr"}
        for r in test
    ])
    return {"train": len(train), "noisy_added": len(noisy),
            "noisy_human": sum(1 for r in noisy if r["label"] == 0),
            "noisy_ai": sum(1 for r in noisy if r["label"] == 1),
            "test_ocr": len(test)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", type=Path)
    ap.add_argument("--fraction", type=float, default=0.3)
    ap.add_argument("--intensity", type=float, default=0.05)
    ap.add_argument("--seed", type=int, default=29)
    args = ap.parse_args()
    print(run(args.data_dir, args.fraction, args.intensity, args.seed))


if __name__ == "__main__":
    main()
