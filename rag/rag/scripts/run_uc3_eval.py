"""Run the Use Case 3 test cases and write predictions.csv + a metrics table.

    python scripts/run_uc3_eval.py                      # full pipeline with the LLM from .env
    python scripts/run_uc3_eval.py --no-llm             # rules + retrieval only (offline, ~0.05 s/case)
    python scripts/run_uc3_eval.py --cases other.csv    # hidden/extra cases, same columns

Writes, next to the cases file (or into --out):
    predictions.csv   columns from evaluation/predictions_schema.csv
    metrics.md        the five target metrics plus retrieval, LLM use and latency
    metrics.json      the same numbers for the console
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.copilot.evaluation import evaluate  # noqa: E402
from app.copilot.orchestrator import Copilot  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cases", default="usecase3/evaluation/public_test_cases.csv")
    parser.add_argument("--no-llm", action="store_true", help="Skip the LLM: rules + retrieval + template answers")
    parser.add_argument("--out", default=None, help="Output folder (default: next to the cases file)")
    args = parser.parse_args()

    copilot = Copilot(use_llm=not args.no_llm)
    doc = evaluate(
        copilot,
        args.cases,
        args.out,
        progress=lambda i, n, test_id: print(f"[{i}/{n}] {test_id}", file=sys.stderr),
    )
    print(f"Mode: {doc['mode']}. Mean latency {doc['mean_latency_s']}s.")
    if doc["llm"]["enabled"]:
        print(f"LLM answers: {doc['llm']['answers_written']}/{len(doc['cases'])}")
    for m in doc["metrics"].values():
        print(f"  {m['label']:<22} {m['value']:>5.0%} ({m['hits']}/{m['total']})  target {m['target']:.0%}  {'PASS' if m['passed'] else 'FAIL'}")
    out = Path(args.out) if args.out else Path(args.cases).parent
    print(f"Wrote {out / 'predictions.csv'}, metrics.md, metrics.json")


if __name__ == "__main__":
    main()
