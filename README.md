<p align="center">
  <img src="frontend/public/logo.svg" alt="Studeal" width="48" />
</p>

<h1 align="center">Studeal</h1>
<p align="center"><em>AI deal-hunting agents for Canadian students</em></p>

<p align="center">
  <a href="#architecture">Architecture</a> ·
  <a href="#pipeline">Pipeline</a> ·
  <a href="#stack">Stack</a> ·
  <a href="#running-locally">Running Locally</a>
</p>

---

## What it does

Students overpay for tech because deal alerts are passive — you have to know what to search, remember to check, and catch the window before it closes.

Studeal inverts this. Users describe what they want in natural language. A conversational agent (Dexter) extracts their intent and deploys a persistent background worker that continuously scans the web, scores every deal it finds, and alerts the user the moment something matches their criteria.

The result: a force of AI agents working in the background, surfacing deals users would have otherwise missed.

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                    Next.js Frontend                  │
│   Daily Drops (semantic search) · My Agents (chat)  │
└───────────────────┬─────────────────────────────────┘
                    │ REST
┌───────────────────▼─────────────────────────────────┐
│                   FastAPI Backend                    │
│         Auth · Watchlists · Deals · Billing          │
└───────┬───────────────────────────┬─────────────────┘
        │                           │
┌───────▼───────┐         ┌─────────▼───────┐
│  Celery Beat  │         │  Celery Workers  │
│  (scheduler)  │         │  (parallel hunt) │
└───────┬───────┘         └─────────┬───────┘
        │                           │
        └──────────┬────────────────┘
                   │
┌──────────────────▼──────────────────────────────────┐
│                  LangGraph Pipeline                  │
│  Search → Fetch → Extract → Score → Deduplicate     │
└──────┬──────────────────────────────────┬───────────┘
       │                                  │
┌──────▼──────┐                  ┌────────▼────────┐
│ Brave Search│                  │   OpenAI API    │
│  + Sources  │                  │  (extraction,   │
│  (RSS, RSS) │                  │   scoring, NL)  │
└─────────────┘                  └─────────────────┘
       │
┌──────▼──────────────────────────────────────────────┐
│              PostgreSQL + pgvector                   │
│   Deals · Users · Agents · Embeddings (1536-dim)    │
└─────────────────────────────────────────────────────┘
```

---

## Pipeline

Each keyword triggers a full agent pipeline:

**1. Search** — Brave Search API queries multiple phrasings of the keyword in parallel, with Canadian locale preference.

**2. Fetch & Extract** — LangGraph nodes scrape result pages via Browserbase, extracting structured deal objects (title, listed price, sale price, condition, source URL) using an LLM.

**3. Score** — A scorer agent evaluates each deal against market context retrieved via RAG (pgvector cosine similarity over recent deals). Score = weighted function of discount depth, price history, recency, and condition.

**4. Deduplicate** — pgvector embedding similarity eliminates near-duplicate deals before persistence. Only novel, high-signal deals are written to the database.

**5. Alert** — Deals above a user's configured threshold trigger a push alert or are batched into a daily email digest (Resend).

Community sources (RedFlagDeals RSS, Slickdeals RSS, student deal sites) are scraped in parallel with keyword hunts, tagged `student_eligible`, and fed through the same scoring pipeline.

---

## Conversational Agent (Dexter)

Users create agents through natural conversation rather than form fields. Dexter is a stateless LLM agent that progressively extracts `WatchlistContext` — product query, budget, condition, brands, and 3–5 search keyword variants — across multiple turns.

```
User: I want gaming laptop deals
Dexter: Gaming laptops — love it! What's your budget?
User: under $1200, new only
Dexter: Got it. Deploy agent? → [Create]
```

Structured outputs (`response_format: json_object`) ensure reliable JSON extraction. The client sends full message history each turn; the server is stateless. On completion, Dexter's extracted `WatchlistContext` is embedded and persisted — the hunt begins immediately.

---

## Stack

| Layer | Technology |
|---|---|
| Frontend | Next.js 15, TypeScript, CSS Modules, Auth.js |
| Backend | FastAPI, Python 3.12, Pydantic v2, SQLAlchemy 2 |
| Agent framework | LangGraph |
| LLM | OpenAI GPT-4o (prod) · Ollama / Groq (dev) |
| Vector search | pgvector, OpenAI `text-embedding-3-small` (1536-dim) |
| Task queue | Celery + Redis |
| Database | PostgreSQL 16 |
| Web scraping | Browserbase (Playwright) |
| Search | Brave Search API |
| Payments | Stripe (subscriptions + customer portal) |
| Email | Resend |
| Monitoring | Sentry (with `LoggingIntegration`) |
| Infrastructure | DigitalOcean (App Platform + Managed DB) |

---

## Key Engineering Decisions

**Concurrent pipeline with bounded parallelism** — `asyncio.gather` runs keyword hunts in parallel; `asyncio.Semaphore(3)` caps concurrent Browserbase sessions to stay within rate limits without serializing the pipeline.

**Swappable LLM backends** — `LLMClient` abstract base class with concrete implementations for OpenAI, Groq, Ollama, and vLLM. Switching models is a one-line env var change. Groq's `failed_generation` recovery handles Llama's native tool-call format divergence.

**RAG-powered scoring** — The scorer agent retrieves semantically similar deals from pgvector before scoring. This gives it live market context ("similar items sell for $X") rather than scoring in a vacuum.

**Defense-in-depth filtering** — Deal filters apply at the SQL level for efficiency and at the Python level for testability. The `min_discount_pct` filter has a graceful fallback: if strict filtering returns zero results, it relaxes the threshold and flags `filtered: false` in the response.

---

## Running Locally

**Prerequisites:** Docker, Python 3.12, Node 20+

```bash
# 1. Start Postgres + Redis
docker-compose up -d

# 2. Backend
pip install -e ".[dev]"
cp .env.example .env          # fill in API keys
alembic upgrade head
uvicorn dealbot.api.main:app --reload --port 8001 --env-file .env

# 3. Worker
celery -A dealbot.worker.celery_app worker --loglevel=info

# 4. Frontend
cd frontend && npm install
cp .env.local.example .env.local   # set AUTH_SECRET + API_BASE_URL
npm run dev
```

Open [http://localhost:3000](http://localhost:3000).

---

## Project Structure

```
dealbot/
├── agents/          # LLM agents (orchestrator, scorer, keyword extractor, NL watchlist)
├── api/             # FastAPI routes (auth, deals, watchlists, billing)
├── db/              # SQLAlchemy models, migrations, RAG retrieval
├── graph/           # LangGraph pipeline nodes and graph definition
├── llm/             # LLMClient abstraction + OpenAI / Groq / Ollama / vLLM backends
├── scrapers/        # Community sources (RSS feeds, student deal sites)
└── worker/          # Celery tasks (hunt, seed, digest)
frontend/
└── src/app/         # Next.js pages (landing, dashboard, catalog, watchlists)
```

---

<p align="center">Built by <a href="https://github.com/joeyb007">Joseph Barbosa</a></p>
