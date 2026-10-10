# Retail Operations Copilot · HCLTech Hackathon, Use Case 3

A copilot for Punjab Sports Mart store staff. Staff type a store issue or a question
("all registers are down", "why is the cricket bat out of stock at ST002?"); the copilot
returns a category, a priority (P1–P4), a confidence, an answer with policy citations, the
data it used, whether a person must step in, and draft tickets or approvals for a manager.

**Design rule:** code makes every decision that is scored or risky (masking of personal
data, priority, escalation, routing, every number); the LLM only writes the answer from
evidence it is handed, and its output is checked. Without an LLM key it still answers.

## Run it

```bash
cd rag/rag
./start.sh          # Windows: start.bat
# open http://localhost:8000/
```

The first run creates a virtual environment and installs dependencies (5–10 minutes).
Optional: put an `OPENROUTER_API_KEY` in `rag/rag/.env` (created from `.env.example`) for
LLM-written answers. Never commit `.env`; it is git-ignored.

Docker with PostgreSQL: set `POSTGRES_PASSWORD` in `rag/rag/.env`, then
`docker compose up --build` in `rag/rag`.

## Where to look

| What | Where |
|---|---|
| Copilot code (six agents) | `rag/rag/app/copilot/` |
| API | `rag/rag/app/api/copilot.py` |
| Web console | `rag/rag/app/web/copilot/` |
| Guide, results, limitations | `rag/rag/docs/USE_CASE_3_COPILOT.md` |
| Architecture | `rag/rag/docs/ARCHITECTURE.md`, `LLD_data_stores.png` |
| Data and policies | `rag/rag/usecase3/` |
| Predictions on the public test cases | `rag/rag/usecase3/evaluation/predictions.csv` |
| Tests | `rag/rag/tests/` |

## Results

Public test cases: category, priority, citation presence, safety and data-use correctness
all 100% (11/11). On reworded prompts never used for tuning (typos, Hinglish, paraphrases,
indirect injections): category 16/18, priority 13/18, attacks refused 4/4, no normal request
refused. Reproduce with `python scripts/run_uc3_eval.py` and
`python scripts/run_paraphrase_eval.py` in `rag/rag`.

## Contributer's

Lovepreet Sharma
Priyanshu Baghel
Prashant Diwakar
Claude.ai
Codex

