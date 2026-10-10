# Retail Operations Copilot (HCLTech Hackathon, Use Case 3)

A copilot for Punjab Sports Mart store staff. It takes a free-text store issue or an
analytics question and returns category, priority, confidence, a cited answer, the CSV
data it used, an escalation decision, PII status and draft workflow payloads.

It is built on our existing RAG engine (this repository); the reused parts are listed
under [What we reused](#what-we-reused-from-the-rag-engine).

## Run it

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # set LLM_PROVIDER and its key (openrouter, openai, anthropic or ollama)

python -m uvicorn app.main:app --port 8000
# open http://localhost:8000/        → the console (Copilot, Dashboard, Evaluation, Drafts)
#      http://localhost:8000/docs    → the API
```

Use `python -m uvicorn` rather than the `uvicorn` script: if the venv was copied from
another folder, its scripts still point at the old Python.

```bash
python scripts/run_uc3_eval.py           # public test cases with the LLM → usecase3/evaluation/
python scripts/run_uc3_eval.py --no-llm  # offline: rules + retrieval + template answers, ~0.05 s per case
python scripts/run_uc3_eval.py --cases path/to/hidden_cases.csv   # same columns as public_test_cases.csv
pytest tests/test_copilot -q
```

Docker: `docker compose up --build`, then the console is at `http://localhost:8000/`.

### Settings (env vars or `.env`)

| Variable | Default | Meaning |
|---|---|---|
| `COPILOT_DIR` | `./usecase3` | holds `knowledge_base/`, `data/`, `evaluation/` |
| `COPILOT_USE_LLM` | `true` | the configured LLM writes the answer |
| `COPILOT_LLM_TRIAGE` | `false` | also ask the LLM for a triage second opinion (one extra call) |
| `COPILOT_LLM_TIMEOUT_SECONDS` | `60` | per call; on timeout the template answer is used |
| `COPILOT_LLM_MAX_RETRIES` | `1` | low on purpose, so a rate-limited model falls back fast |
| `COPILOT_LLM_MAX_TOKENS` | `6000` | output budget; reasoning models spend part of it thinking |
| `COPILOT_USE_EMBEDDINGS` | `true` | add bge-small dense search to BM25 |
| `COPILOT_ANCHOR_TS` | `2026-10-10 12:00:00` | "now" for last-24-hours / last-30-days |
| `COPILOT_CONFIDENCE_THRESHOLD` | `0.60` | below this the copilot escalates (SOP escalation policy) |

The LLM writes every answer. Every decision that is scored (category, priority,
escalation, masking, numbers) is made in code, so each one traces to a rule or a CSV row.
The LLM's reply is checked: citations outside the retrieved sections are removed, a
cut-off reply is replaced by the template answer, and personal data is masked again. If
the LLM is down, rate-limited or has no key, the copilot answers from the template.

## The console

A static client of the API (`app/web/copilot/`: one HTML file, one stylesheet, one script,
no build step, no dependencies), served by FastAPI at `/copilot/`. A Punjab Sports Mart app
shell: sidebar with persona and store pickers, model status and theme; four views.

- **Copilot:** a conversation.
  - **Home per persona.** Cashier, Store Manager, Inventory Associate and Customer Service
    get one-tap common issues and their recent tickets; the Store Manager also sees open
    P1/P2 cases; the Regional Manager starts on KPIs and the cases that need attention.
  - **Live agents.** `/ask/stream` (Server-Sent Events) shows each agent finishing as it
    runs; a masked card or phone number turns into a redaction chip in the user's own
    message as soon as the Safety agent returns.
  - **Ticket card.** Priority block, category, ticket ID, store, confidence ring, and an
    escalation bar with team, queue and SLA. Tabs: *Answer* (key visual, the policy rule,
    a tick-off checklist of next steps, the full ticket text), *Data* (visuals, findings,
    files read), *Policy* (each cited section with the sentence that applies), *Drafts*,
    *Trace*.
  - **Data as pictures.** Receipts as "18 of 50" bars against the short-shipment line,
    vendor fill and on-time rates on gauges with the warning line, root cause as a numbered
    chain in policy check order, shelf vs system stock, shelf vs POS price, rankings, tables.
  - **Citations in a side panel.** Any citation opens the section with the exact sentence
    highlighted.
  - **Follow-ups as quick replies.** "Which store?" becomes store chips (busiest first);
    "anyone hurt?" becomes two buttons. A reply, tapped or typed, is added to the open issue.
  - **Refusals** get their own card: what was detected, what was not done, and the
    Security draft.
- **Dashboard (bonus B2):** KPIs, open cases by priority, category and region, weekly new
  cases per region as small multiples on one scale, and the open P1/P2 cases with a
  *Triage* button that sends a case to the copilot.
- **Evaluation (M9):** the five target metrics as score rings with the target marked, and
  per-case results; runs the public test cases in the background with or without the LLM.
- **Drafts (bonus B3):** every ticket, notification, vendor-claim draft, quality hold,
  refund approval request and security incident, written to `data/copilot_actions.jsonl`,
  filterable by type. Nothing is sent.

Light and dark themes; Enter sends, Shift + Enter adds a line, `/` focuses the composer,
Esc closes the panel; works from phone width up.

## Architecture: six cooperating agents

```
message
  → Safety agent     regex masking of card/phone/email/OTP/IDs; injection + restricted-data detection
  → Triage agent     category, priority, confidence
                     = SOP keyword rules + vote of the most similar of 550 labelled tickets (BM25);
                       hard P1 rules always win; a request spanning two categories takes the one
                       with the immediate risk and is flagged as a policy conflict
  → Data agent       pandas tools over the 9 CSVs: stock-out root cause, unresolved incidents,
                     vendor quality ranking, quality complaints, short shipments, price check
  → Triage.refine    data facts can raise priority (e.g. high-demand SKU at zero on hand → P2)
  → Workflow agent   escalation_matrix.csv routing, approval rules, draft ticket / notification /
                     vendor claim / quality hold / refund approval request
  → Retrieval agent  the KB sections whose rules were applied, + hybrid search (BM25 + bge-small, RRF)
  → Composer         the LLM writes the answer from those sections and the data findings;
                     checked, re-masked, template fallback; ends with what it cannot know
```

Code: `app/copilot/` (`safety.py`, `triage.py`, `data_agent.py`, `workflow.py`,
`retrieval.py`, `composer.py`, `orchestrator.py`, `evaluation.py`), the API in
`app/api/copilot.py`, the console in `app/web/copilot/`. More detail:
[ARCHITECTURE.md](ARCHITECTURE.md).

### How each requirement is met

| Req | Where |
|---|---|
| M1 natural language | typo-tolerant keyword rules, fuzzy product-name matching, similar-ticket vote |
| M2 RAG | `knowledge_base/*.md` chunked one section per `##` heading (`chunk_markdown_by_heading`) |
| M3 citations | `file.md#section-slug`; each rule that fires names its section; search fills gaps |
| M4 classification | `triage.py`; `reasoning` lists the signals and the rule that set the priority |
| M5 actions + follow-up | policy "Recommended steps" + tool actions; follow-up asks for missing SOP triage fields |
| M6 structured data | `data_agent.py`; `data_used` lists the CSVs actually read |
| M7 escalation | P1, confidence < 0.60, restricted data, policy conflict, matrix approval, refund threshold, vendor claim, quarantine; summary has category, priority, store, scope, policy, team and data |
| M8 responsible AI | mask before anything else runs; refusals get no data access; answer re-masked; no fake completions; data-backed answers state their limits |
| M9 evaluation | `scripts/run_uc3_eval.py` or the Evaluation tab → `predictions.csv` (schema columns) + metrics table |
| M10 multi-agent | the six agents above, each with one job and a typed result |
| B1 root cause | follows the check order in `03_…#root-cause-hints` (inventory → reorder → receipts → warehouse → vendor → cases → transfer) |
| B2 dashboard | Dashboard view |
| B3 mock workflow | Drafts view / `data/copilot_actions.jsonl` |

## Results on the public test cases

From `usecase3/evaluation/metrics.md`:

| Metric | Result | Target |
|---|---|---|
| Category accuracy | 100% (11/11) | 75% |
| Priority accuracy | 100% (11/11) | 75% |
| Citation presence | 100% (11/11) | 85% |
| Safety pass rate | 100% (2/2) | 100% |
| Data-use correctness | 100% (5/5) | 75% |

The five metrics are decided in code, so they are the same with or without the LLM.
Diagnostics: 17/17 expected sources cited; hybrid search alone ranks an expected section
first for 9/11 prompts. `metrics.md` also records how many answers the LLM wrote: with a
free OpenRouter model that is rate-limited upstream, most calls return HTTP 429 and those
answers come from the template. With the paid `z-ai/glm-5.3-flash` the LLM wrote 9 of 11
answers at about 25 s each (the injection refusal always uses a fixed template, and one
reply was cut off and replaced).

Eleven public cases are a small sample. We also checked 30 prompts we wrote ourselves
(typos, paraphrases, other stores and SKUs, injection variants); all pass, but we set
their expected labels. 44 unit tests cover the copilot.

## Demo script (guide, section 18)

1. **Normal triage:** "One card reader down" → POS, P3, runbook steps, no escalation, POS ticket draft.
2. **Data-backed root cause:** "Bat out of stock again" → short shipments 18/50 and 20/45, vendor V001 fill rate 68%, system 4 vs shelf 0, transfer option from ST001, vendor-claim draft. Or "Tennis balls for an academy" → warehouse WH001 at zero.
3. **Safety:** "Prompt injection" → refusal, no data accessed, Security P1. Then "Card number in a ticket" → masked, explained, not stored.
4. **Flow:** the live agent steps while it runs, then the Trace tab.
5. **Evaluation:** Evaluation view → Run with LLM.

### Answers to the judges' likely questions

- **Why P1/P2/P3?** The `reasoning` field names the rule; P1 rules (store-wide outage, injury/recall, payment data, injection) are code and override the model. Data facts can raise P3 to P2.
- **Which source supports this?** The cited sections are the ones whose rules were applied; click a citation to read it.
- **How do you prevent hallucinated policy?** The LLM gets only the retrieved sections and data findings, may cite only those sections, and anything else it cites is removed. A cut-off reply falls back to the template.
- **How do you use structured data?** pandas computes every number; the LLM is told to use those numbers and compute none.
- **PII or payment data?** Masked before triage, before the LLM, before logs and tickets, and again on the final answer.
- **Not confident?** Below 0.60 the copilot escalates, says so, and asks a focused follow-up question.
- **Two policies apply?** The SOP's rule: the category with the immediate risk wins, and a person confirms it.

## What we reused from the RAG engine

| Reused | For |
|---|---|
| `app/retriever/bm25_search.py` (`BM25Search`, `reciprocal_rank_fusion`) | KB search and the similar-ticket vote |
| `app/embeddings/factory.py` (bge-small) | dense KB search |
| `app/llm/factory.py` | the LLM (OpenAI, Anthropic, OpenRouter, Ollama); now with a configurable token budget |
| `app/config/settings.py`, logging, security middleware, FastAPI app | config, logs, API |
| `app/ingestion/chunking.py` | new `chunk_markdown_by_heading` beside the recursive chunker |

New for Use Case 3: `app/copilot/`, `app/api/copilot.py`, `app/web/copilot/`,
`scripts/run_uc3_eval.py`, `tests/test_copilot/`, `usecase3/`. The general
document-chat features (upload, `/query`, `/chat`, the Next.js dashboard) still work but
are not part of this demo.

## Limitations and what we would do next

- **Labels in `support_cases.csv` are noisy** beyond the ~30 seeded tickets (the same "safety concern" text appears as P2, P3 and P4), so the similar-ticket vote is used for category only, never priority.
- **The similar-ticket vote sees near-copies of the public prompts** (RET-001…RET-031 paraphrase them), which inflates confidence on the public set. Category and priority come from the rules, which we also tested on prompts we wrote.
- **Keyword rules have limits.** Unusual wording falls back to the similar-ticket vote; with no signal at all the copilot asks for details (confidence 0.35, escalated) instead of guessing. Next: a small classifier trained on the cleaned seeded tickets.
- **Policy conflicts** are detected when two categories with strong signals give different priorities; a conflict inside one policy text is not.
- **Vendor-quality ranking** counts complaints per vendor via SKU → vendor without normalising by sales volume, so it says where to look, not who is at fault (the answer says this too).
- **`employee_master.csv`** is loaded but not used; the next step is checking the requester's escalation level.
- **Drafts only.** Nothing is sent; payloads go to `data/copilot_actions.jsonl`.
- **LLM availability and latency** depend on the provider: free OpenRouter models are often rate-limited (template fallback), and a reasoning model takes about 25 s per answer.
