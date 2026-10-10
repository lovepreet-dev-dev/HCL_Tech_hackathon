"""Score the copilot on reworded prompts: synonyms, typos, Hinglish, indirect injections.

The public test cases mostly use the SOP's own words. This set does not, so it shows
how far the triage and safety checks generalise. ``tune`` rows were used while
adjusting the rules; ``holdout`` rows were written at the same time but only scored
at the end, so they show whether a change generalises or only fits the tune rows.

    python scripts/run_paraphrase_eval.py              # rules + embeddings (as configured)
    python scripts/run_paraphrase_eval.py --no-semantic # keyword rules only
    python scripts/run_paraphrase_eval.py -v            # every row, not only misses

A blank expected category or priority is not scored; ``refuse=no`` rows are the
negatives: normal requests that must not be refused.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CASES = ROOT / "usecase3" / "evaluation" / "paraphrase_cases.csv"


def score(copilot, rows: list[dict[str, str]], verbose: bool = False) -> dict[str, tuple[int, int]]:  # noqa: ANN001
    hits = {k: [0, 0] for k in ("category", "priority", "refused when it should", "not refused when it should not")}
    for row in rows:
        r = copilot.ask(row["message"], persist=False)
        checks = {}
        if row["category"]:
            checks["category"] = r.category == row["category"]
        if row["priority"]:
            checks["priority"] = r.priority == row["priority"]
        key = "refused when it should" if row["refuse"] == "yes" else "not refused when it should not"
        checks[key] = r.safety_refusal == (row["refuse"] == "yes")
        for k, ok in checks.items():
            hits[k][0] += ok
            hits[k][1] += 1
        if verbose or not all(checks.values()):
            got = "REFUSED" if r.safety_refusal else f"{r.category} {r.priority}"
            want = "REFUSE" if row["refuse"] == "yes" else f"{row['category'] or '-'} {row['priority'] or '-'}"
            mark = "ok  " if all(checks.values()) else "MISS"
            print(f"  {mark} {row['id']}  got {got:34.34}  want {want:34.34}  {row['message'][:60]}")
    return {k: (v[0], v[1]) for k, v in hits.items()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--no-semantic", action="store_true", help="keyword rules only (no embeddings)")
    parser.add_argument("--llm", action="store_true", help="also use the configured LLM (triage + guard)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    from app.config.settings import get_settings
    from app.copilot.orchestrator import Copilot

    settings = get_settings().model_copy(update={
        "copilot_semantic_triage": not args.no_semantic,
        "copilot_use_embeddings": not args.no_semantic,
        "copilot_llm_triage": args.llm,
        "copilot_llm_guard": args.llm,
    })
    copilot = Copilot(settings=settings, use_llm=args.llm)
    rows = list(csv.DictReader(CASES.open(encoding="utf-8")))
    for split in ("tune", "holdout"):
        print(f"\n{split}:")
        result = score(copilot, [r for r in rows if r["split"] == split], args.verbose)
        for k, (ok, n) in result.items():
            print(f"  {k:32} {ok}/{n}")


if __name__ == "__main__":
    main()
