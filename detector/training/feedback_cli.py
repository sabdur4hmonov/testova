"""Local reviewer-only management; no reviewer endpoint exposed to bot users."""
from pathlib import Path
import argparse
import json
from detector.service.corpus import CorpusStore

DEFAULT_DB = Path(__file__).resolve().parents[1] / "data/feedback/corpus.sqlite3"

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", type=Path, default=DEFAULT_DB)
    sub = p.add_subparsers(dest="action", required=True)
    sub.add_parser("stats")
    sub.add_parser("pending")
    show = sub.add_parser("show")
    show.add_argument("sample_id")
    withdraw = sub.add_parser("withdraw")
    withdraw.add_argument("--receipt", type=Path, required=True)
    review = sub.add_parser("review")
    review.add_argument("sample_id")
    review.add_argument("--label", choices=["human", "ai"], required=True)
    review.add_argument("--evidence-kind", choices=["historical_source", "author_confirmation", "generator_record"], required=True)
    review.add_argument("--evidence", required=True)
    review.add_argument("--reviewer", required=True)
    review.add_argument("--model", default="")
    review.add_argument("--rights-confirmed", action="store_true")
    review.add_argument("--reject", action="store_true")
    args = p.parse_args()
    store = CorpusStore(args.db)
    if args.action == "withdraw":
        receipt = json.loads(args.receipt.read_text(encoding="utf-8-sig"))
        if not store.withdraw(receipt["sample_id"], receipt["withdrawal_token"]):
            raise ValueError("receipt not found")
        print(json.dumps({"status":"withdrawn", "excluded_from_future_training":True, "existing_model_weights_automatically_unlearned":False}))
        return
    if args.action in {"pending", "show"}:
        print(json.dumps(store.pending() if args.action == "pending" else store.show(args.sample_id), ensure_ascii=False))
        return
    if args.action == "review":
        store.review(args.sample_id, label=args.label, evidence_kind=args.evidence_kind,
                     evidence=args.evidence, reviewer=args.reviewer, model=args.model,
                     rights_confirmed=args.rights_confirmed, reject=args.reject)
    print(json.dumps(store.stats(), ensure_ascii=False))

if __name__ == "__main__":
    main()
