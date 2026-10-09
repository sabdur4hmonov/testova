"""Export a Hugging Face AI-text detector to INT8 ONNX for the detector service.

One-off, run on a dev machine or in Colab — NOT in the service image (it needs
torch + transformers, the service needs neither):

    pip install -r detector/requirements-export.txt
    python -m detector.scripts.export_onnx \
        --model-id Oxidane/tmr-ai-text-detector --out detector/models/en-tmr \
        --languages en

    # the A2 model (a local fine-tuned xlm-roberta checkpoint):
    python -m detector.scripts.export_onnx --model-id path/to/checkpoint \
        --out detector/models/uzru-xlmr --languages uz uz-cyrl ru \
        --threshold 0.63

Writes model.onnx (INT8), tokenizer.json and detector_meta.json, then checks
the INT8 model against the original on a few texts and prints the largest
probability difference.

--kind desklib handles desklib/ai-text-detector-v1.01, whose head is custom
(mean pooling + one linear logit) and so is not an AutoModelFor... class.
"""
from __future__ import annotations

import argparse
import json
import shutil
import tempfile
from pathlib import Path

SAMPLES = [
    "The mitochondria is the powerhouse of the cell, and in this essay I will "
    "explain why it matters for every living organism on our planet.",
    "honestly i didnt think the trip would be that fun but my cousin made "
    "everyone laugh the whole way there and we stopped for ice cream twice",
    "In conclusion, it is important to note that technology has both positive "
    "and negative impacts on society, and a balanced approach is essential.",
]


def _ai_index(id2label: dict, override: int | None) -> int:
    if override is not None:
        return override
    for idx, label in id2label.items():
        if str(label).lower() in {"ai", "fake", "machine", "generated", "ai-generated", "label_1"}:
            return int(idx)
    raise SystemExit(
        f"cannot tell which label is AI from {id2label}; pass --ai-index"
    )


def build_model(kind: str, model_id: str):
    import torch
    from transformers import AutoConfig, AutoModel, AutoModelForSequenceClassification

    if kind == "seqcls":
        model = AutoModelForSequenceClassification.from_pretrained(model_id)
        model.eval()

        class Wrap(torch.nn.Module):
            def __init__(self, m):
                super().__init__()
                self.m = m

            def forward(self, input_ids, attention_mask):
                return self.m(input_ids=input_ids, attention_mask=attention_mask).logits

        return Wrap(model).eval(), "softmax2", dict(model.config.id2label)

    if kind == "desklib":
        from huggingface_hub import hf_hub_download
        from safetensors.torch import load_file

        config = AutoConfig.from_pretrained(model_id)

        class Desklib(torch.nn.Module):
            def __init__(self):
                super().__init__()
                self.model = AutoModel.from_config(config)
                self.classifier = torch.nn.Linear(config.hidden_size, 1)

            def forward(self, input_ids, attention_mask):
                hidden = self.model(input_ids=input_ids, attention_mask=attention_mask)[0]
                mask = attention_mask.unsqueeze(-1).expand(hidden.size()).float()
                pooled = (hidden * mask).sum(1) / mask.sum(1).clamp(min=1e-9)
                return self.classifier(pooled)

        model = Desklib()
        weights = Path(model_id) / "model.safetensors"
        if not weights.exists():
            weights = Path(hf_hub_download(model_id, "model.safetensors"))
        missing, unexpected = model.load_state_dict(load_file(str(weights)), strict=False)
        if missing or unexpected:
            raise SystemExit(f"desklib weights mismatch: missing={missing} unexpected={unexpected}")
        return model.eval(), "sigmoid1", {}

    raise SystemExit(f"unknown --kind {kind}")


def torch_probs(model, tok, output: str, ai_index: int, texts, max_length: int):
    import torch

    enc = tok(texts, return_tensors="pt", padding=True, truncation=True, max_length=max_length)
    with torch.no_grad():
        logits = model(enc["input_ids"], enc["attention_mask"])
    if output == "sigmoid1":
        return torch.sigmoid(logits.reshape(-1)).tolist()
    return torch.softmax(logits, dim=-1)[:, ai_index].tolist()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-id", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--languages", nargs="+", required=True)
    ap.add_argument("--kind", choices=["seqcls", "desklib"], default="seqcls")
    ap.add_argument("--name")
    ap.add_argument("--version")
    ap.add_argument("--ai-index", type=int)
    ap.add_argument("--max-length", type=int, default=512)
    ap.add_argument("--threshold", type=float, default=0.5)
    ap.add_argument("--mixed-band", type=float, default=0.15)
    ap.add_argument("--license", default="")
    ap.add_argument("--opset", type=int, default=17)
    args = ap.parse_args()

    import torch
    from onnxruntime.quantization import QuantType, quantize_dynamic
    from onnxruntime.quantization.shape_inference import quant_pre_process
    from transformers import AutoTokenizer

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    tok = AutoTokenizer.from_pretrained(args.model_id, use_fast=True)
    model, output, id2label = build_model(args.kind, args.model_id)
    ai_index = 0 if output == "sigmoid1" else _ai_index(id2label, args.ai_index)

    with tempfile.TemporaryDirectory() as tmp:
        fp32 = Path(tmp) / "model_fp32.onnx"
        dummy = tok(["hello world"], return_tensors="pt")
        torch.onnx.export(
            model,
            (dummy["input_ids"], dummy["attention_mask"]),
            str(fp32),
            input_names=["input_ids", "attention_mask"],
            output_names=["logits"],
            dynamic_axes={
                "input_ids": {0: "batch", 1: "seq"},
                "attention_mask": {0: "batch", 1: "seq"},
                "logits": {0: "batch"},
            },
            opset_version=args.opset,
            dynamo=False,
        )
        # Measured on tmr-ai-text-detector over 30 texts (docs/YANGI_FUNKSIYALAR.md):
        #   plain quantize_dynamic, no pre-process  max |dp| = 0.17
        #   + quant_pre_process                     max |dp| = 0.048, 125 MB
        #   + MatMul only, per-channel (chosen)     max |dp| = 0.034, 243 MB
        # The embedding table stays fp32 — quantizing its Gather is where most
        # of the drift came from, and RAM stays far under budget either way.
        pre = Path(tmp) / "model_pre.onnx"
        quant_pre_process(str(fp32), str(pre))
        quantize_dynamic(
            str(pre), str(out / "model.onnx"),
            weight_type=QuantType.QInt8, per_channel=True,
            op_types_to_quantize=["MatMul"],
        )
        # External-data side files, if the exporter wrote any, are not needed
        # after quantization (the INT8 graph is self-contained).
        for extra in Path(tmp).glob("*.data"):
            extra.unlink()

    tok.backend_tokenizer.save(str(out / "tokenizer.json"))

    name = args.name or Path(args.model_id.rstrip("/")).name
    meta = {
        "name": name,
        "version": args.version or f"{name}-int8",
        "languages": args.languages,
        "output": output,
        "ai_index": ai_index,
        "max_length": args.max_length,
        "cls_id": tok.cls_token_id,
        "sep_id": tok.sep_token_id,
        "pad_id": tok.pad_token_id,
        "threshold": args.threshold,
        "mixed_band": args.mixed_band,
        "source": args.model_id,
        "license": args.license,
    }
    (out / "detector_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    # Parity check: INT8 ONNX vs the original torch model.
    from detector.service.engine import OnnxDetector

    det = OnnxDetector(out)
    ref = torch_probs(model, tok, output, ai_index, SAMPLES, args.max_length)
    got = [det.score_document(t)[0] for t in SAMPLES]
    diff = max(abs(a - b) for a, b in zip(ref, got))
    print(json.dumps({"meta": meta, "torch": ref, "onnx_int8": got, "max_abs_diff": diff}, indent=2))
    size_mb = (out / "model.onnx").stat().st_size / 1e6
    print(f"model.onnx: {size_mb:.0f} MB")
    if diff > 0.05:
        print("WARNING: INT8 drifts more than 0.05 from fp32 on the samples; "
              "check before deploying")
    shutil.rmtree(out / "__pycache__", ignore_errors=True)


if __name__ == "__main__":
    main()
