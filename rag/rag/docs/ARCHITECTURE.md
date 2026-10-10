# Architecture: Retail Operations Copilot (Use Case 3)

Punjab Sports Mart store staff type an issue ("all registers are down", "why are bats
out of stock?") or a question ("unresolved P1/P2 in the last 24 hours?"). The copilot
returns a category, priority, confidence, a cited answer, the CSV data it used, an
escalation decision and PII status.

**Design rule:** code makes every decision that is scored or risky (priority, escalation,
masking, numbers). The LLM writes the answer from the evidence code hands it, and gives
triage a second opinion. So every decision can be traced to a rule, a policy section or a
CSV row, and if the LLM fails the copilot still answers from a template.

---

## 1. The big picture

```
┌──────────────────────────────┐  calls  ┌──────────────────────────────┐
│ Web console  /copilot/       │ ──────► │ FastAPI  /api/v1/copilot/*   │
│ Ask · Dashboard ·            │         │ ask · status · kb · dashboard│
│ Evaluation · Actions         │         │ actions · metrics · evaluate │
└──────────────────────────────┘         └──────────────┬───────────────┘
                                                        ▼
              ┌─────────────────────────────────┐
              │ Orchestrator (orchestrator.py)  │  fixed order, typed hand-offs,
              │ runs 6 agents, records a trace  │  timing per agent
              └────────────────┬────────────────┘
                               ▼
  message ─► 1 SAFETY ─► 2 TRIAGE ─► 3 DATA ─► (refine) ─► 4 WORKFLOW ─► 5 RETRIEVAL ─► 6 COMPOSER ─► response
               │            │           │                       │              │              │
               │            │           ▼                       ▼              ▼              ▼
               │            │     9 CSV files            escalation_      5 policy .md    LLM (writes answer)
               │            │     (pandas)               matrix.csv       27 sections     (rewording only)
               │            ▼                            + actions.jsonl  BM25 + bge-small
               │      550 labelled tickets
               │      (similar-ticket vote)
               ▼
         regex masking, injection & restricted-data checks
```

## 2. The six agents: what each one does and why it exists

| # | Agent (file) | Input → output | Why a separate agent |
|---|---|---|---|
| 1 | **Safety** (`safety.py`) | raw text → masked text, PII found, injection?, restricted data asked for? | Runs **first**, so no other agent, log or LLM ever sees a card number. Pure regex, so the 100% safety target never depends on a model. |
| 2 | **Triage** (`triage.py`) | masked text → category, priority, confidence, reasons | Combines three signals: SOP keyword rules, a vote of the most similar historic tickets, and the LLM as a second opinion. **Hard P1 rules always win.** |
| 3 | **Data** (`data_agent.py`) | category + entities → findings, CSVs used, likely cause | pandas does every count and date calculation (anchor `2026-10-10 12:00 IST`). An LLM is bad at arithmetic; pandas is exact. |
| – | **Triage.refine** | data facts → maybe a higher priority | e.g. "bat out of stock" is P3 by wording, but the data says it is high-demand with 0 on hand, so the policy makes it P2. |
| 4 | **Workflow** (`workflow.py`) | triage + data → team, SLA, escalate?, mock payloads | Routing comes from `escalation_matrix.csv`; approvals come from the SOP / human-in-the-loop policy. Writes ticket / notification / vendor-claim draft / quality hold / refund-approval payloads. |
| 5 | **Retrieval** (`retrieval.py`) | rules applied + question → policy sections to cite | Cites the exact sections whose rules made the decision, plus hybrid search for wording no rule recognised. |
| 6 | **Composer** (`composer.py`) | everything above → final answer + follow-up question | The LLM writes the answer only from cited sections and data findings; fake citations are stripped, a cut-off answer falls back to a template, and the output is masked again. |

**Why multi-agent (M10) and not one big prompt?** Each step has a different kind of
correctness: safety must be exact, numbers must be exact, routing must follow a table,
wording can be fuzzy. Splitting them lets each use the right tool (regex, pandas, a
lookup table, search, an LLM) and lets us test and explain each one on its own.

## 3. One request, end to end

Input: *"English Willow Cricket Bat is out of stock again at Store ST002. Why is this happening?"*

| Step | What happens |
|---|---|
| Safety | no PII, no injection → text passes unchanged |
| Entities | `ST002`, product name matched to `SKU1001` |
| Triage | keywords "out of stock" → Inventory; 62% of similar tickets agree; rule "stock-out of a high-demand item" → **P2**, confidence 0.91 |
| Data | inventory: on_hand 0, system_qty 4 (false availability) · receipts: 18/50 and 20/45 (two short receipts in a row) · vendor V001 fill rate 68% (< 80% warning) · nearby store ST001 has 18 → **likely cause: short shipments linked to low vendor fill rate** |
| Workflow | matrix row "High-demand SKU stock-out" → Store Manager + Inventory Lead; two short receipts → vendor claim needs approval → **escalate**, add Supply Chain, draft vendor claim |
| Retrieval | cites `03…#stock-outs-and-replenishment`, `03…#root-cause-hints`, `04…#short-shipments`, `05…#human-in-the-loop`, … |
| Composer | policy quote + data findings + next steps (cycle count, vendor claim review, transfer review) + sources |

## 4. Technologies and why we chose each one

| Technology | Used for | Why this one |
|---|---|---|
| **Python 3.12** | everything | One language for data, ML and API, and the language our existing RAG engine is written in. |
| **FastAPI + Uvicorn** | REST API `/api/v1/copilot/*` | Already the backbone of our RAG engine; auto docs at `/docs`; typed requests. |
| **Pydantic v2** | `CopilotResponse` schema, settings | One schema shared by the API, UI and `predictions.csv`, so the output shape can't drift. |
| **pandas** | Data agent over 9 CSVs | Joins, filters and time windows in a few readable lines; exact numbers. |
| **BM25** (our own `BM25Search`) | KB keyword search + similar-ticket vote | Policy text uses exact terms ("reorder point", "short shipment"); BM25 matches them well. Reused from our RAG engine. |
| **BAAI/bge-small-en-v1.5** (sentence-transformers) | dense KB search | Matches meaning, not just words ("tills not taking cards" ≈ "registers cannot process card payments"). Small, free, runs on CPU. |
| **Reciprocal Rank Fusion** | merge BM25 + dense rankings | Combines two rankers without tuning score scales. Reused from our RAG engine. |
| **NumPy cosine, in memory** | dense index | The KB is only 27 sections; a vector database (FAISS/Chroma) would add nothing here. Our RAG engine's FAISS stays for its general document chat. |
| **Regex** | Safety agent, priority rules | Deterministic and testable; this is where errors are most expensive. |
| **LangChain** (chat-model wrapper only) | the LLM that writes answers | One interface for OpenRouter, OpenAI, Anthropic or local Ollama: switch with `LLM_PROVIDER` in `.env`. |
| **HTML + CSS + vanilla JS** | the web console, served by FastAPI | No build step and no dependencies; one origin, so the strict CSP stays on; light and dark themes. |
| **pytest** | 38 copilot tests (187 in total) | Locks the safety rules and the public cases so later changes can't break them. |
| **Docker** | packaging | `docker compose up` runs the API with the KB and CSVs inside the image. |

## 5. Guardrails (Responsible AI, M8)

1. **Mask first:** card → `[REDACTED_CARD]`, phone → `******3210`, email → `c***********e@example.com`, OTP/CVV/PIN → `[REDACTED]`, before triage, LLM, logs or tickets.
2. **Refuse injection / restricted requests:** "ignore previous instructions", "show card numbers", "supplier contract prices", "reveal hidden prompt" → refusal, **no data access at all**, Security P1.
3. **No invented policy:** citations come only from the KB; LLM citations not in the retrieved list are stripped; a cut-off LLM answer falls back to the template answer.
4. **No fake actions:** refunds, vendor claims and quarantines are *drafts awaiting a human*; the copilot never says something was done.
5. **Escalate when unsure:** confidence < 0.60, P1, restricted data, a policy conflict (two categories whose rules give different priorities: the more urgent wins and a person confirms), or an action needing approval → human, with an escalation summary.
6. **State limits:** a data-backed answer ends with one line: figures come from the CSV snapshot as of the anchor, live systems are not connected, and drafts wait for a person.
7. **Neutral wording:** vendor data is "a signal for review, not proof of fault".

## 6. How we measure it (M9)

`python scripts/run_uc3_eval.py` runs `public_test_cases.csv` and writes `predictions.csv`
(the required schema) and `metrics.md`. Rules-only mode on the 11 public cases: category
100%, priority 100%, citation presence 100%, safety 100%, data-use 100% (targets 75 / 75 /
85 / 100 / 75). Eleven cases is a small sample, so we also tested 30 prompts we wrote
(typos, paraphrases, other stores); hidden prompts can still find gaps.

## 7. What we reused vs. built

**Reused from our RAG engine:** `BM25Search`, `reciprocal_rank_fusion`, the bge-small
embedding factory, the LLM provider factory, settings, logging, the FastAPI app.
**Built for this use case:** the six agents (`app/copilot/`), the heading-based chunker
with `file.md#slug` citations, the API router, the web console, the evaluation module and
the tests.

## 8. Questions judges may ask

| Question | Short answer |
|---|---|
| Why is this P1/P2/P3? | `reasoning` names the rule; P1 rules are code and override the model; data can raise P3 → P2. |
| Which source supports it? | The cited sections are the ones whose rules were applied; click one in the UI to read it. |
| How do you stop hallucinated policy? | The answer is assembled from KB sentences; the LLM can only cite retrieved sections. |
| How do you use structured data? | The Data agent: pandas over the CSVs; the composer prints its numbers verbatim. |
| What happens with PII or a card number? | Masked by the first agent, before anything else; masked again on output. |
| What if the model is not confident? | Below 0.60: escalate and ask a focused follow-up question. |
| Why not just one LLM prompt? | Exactness: masking, counting and routing must not depend on a model. And it's explainable. |
| What if the LLM fails or invents something? | Its citations are checked against the retrieved list, a cut-off answer is replaced by the template answer, and output is re-masked. No key or no network: template answers. |
| Why no vector database? | 27 sections fit in memory; FAISS or Chroma would only add moving parts. |
