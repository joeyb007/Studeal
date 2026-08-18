<p align="center">
  <img src="frontend/public/logo.svg" alt="Studeal" width="48" />
</p>

<h1 align="center">Studeal</h1>
<p align="center"><em>A fleet of browser-driving AI agents that hunt secondhand marketplaces so you never overpay.</em></p>

<p align="center">
  <strong><a href="https://studeal.site">studeal.site</a></strong> · live in production
</p>

<p align="center">
  <a href="#how-a-hunt-works">How a hunt works</a> ·
  <a href="#the-shared-pool--recommender">Pool & recommender</a> ·
  <a href="#product-surfaces">Product</a> ·
  <a href="#evaluation">Evaluation</a> ·
  <a href="#running-in-production">Production</a> ·
  <a href="#stack">Stack</a> ·
  <a href="#running-locally">Running locally</a>
</p>

---

## What it does

Finding a good used deal means searching five marketplaces, in the right phrasings, every day, and knowing enough about the product to judge what you see. Almost nobody does that, so most people overpay or miss the good listings entirely.

Studeal does it for you. You describe what you want in plain language to Scout, a conversational agent that builds a rich buyer profile (budget, brands, condition, what the thing is actually for). Scout deploys a persistent hunting agent that sweeps Kijiji, Facebook Marketplace, eBay, Craigslist, and a set of refurb retailers on a schedule, driving real browser sessions the way a person would: navigating, searching, scrolling, paginating. Every listing it sees lands in a shared pool; a recommender ranks the pool against your profile and emails you the matches worth your money, each with a one-line reason.

There are no per-site scrapers and no site adapters. The same agent code navigates every marketplace, which is what the evaluation section is about.

## How a hunt works

```
                        Watchlist (buyer profile)
                                  │
                       Marketplace router (LLM)
                 picks (query, marketplace) lanes
                                  │
        ┌───────────────┬─────────┴───────┬───────────────┐
   lane: kijiji    lane: facebook    lane: kijiji     lane: ebay      ... up to 6
   "aeron chair"   "aeron chair"     "ergonomic       concurrent, each with its
        │               │             office chair"   own browser session
        ▼               ▼                  │
   Explorer agent (Claude Sonnet) drives the page via CDP
   perception snapshots: AX tree + DOMSnapshot fused into a
   compact element tree the LLM can read and act on
        │
        ▼
   Every settled page snapshot is sunk to an extractor pool
   (Claude Haiku) that emits structured offers concurrently,
   while a deterministic sidecar captures listing thumbnails
   from the same DOM (no LLM touches an image URL)
        │
        ▼
   Offers are grounded against the page (fabricated URLs
   dropped), deduplicated by canonical URL, embedded
   (Titan V2), and upserted into the shared listings pool
        │
        ▼
   Ranker (LLM listwise) scores pool candidates against the
   buyer profile · results cached · matches alerted by email
```

Design choices that matter:

- **Perception, not parsing.** Pages are captured through Chrome DevTools Protocol as a fused accessibility-tree + DOM snapshot, filtered to visible and interactive elements. The explorer reads the same compact tree it acts on, so navigation generalizes across sites with zero per-site code.
- **Extraction is off the navigation loop.** The explorer never extracts listings. Snapshots stream to a concurrent extractor pool, so a slow extraction never stalls navigation, and large pages are chunked into overlapping views for recall (single-window truncation was measured missing most of a 137k-char page).
- **Parallel lanes.** Each (query, marketplace) pair is its own lane with its own browser session, fanned out under a concurrency cap. Lane state persists to the database, so the live view survives a page refresh.
- **Trust boundaries around the LLM.** Extracted offers must ground to an anchor actually present in the snapshot (an LLM completing a clipped href will invent plausible listing URLs; those are dropped, not persisted). Thumbnails are associated to listings by deterministic DOM containment plus a per-marketplace CDN whitelist, so an attribution error can only ever be a missing image, never a wrong one.

## The shared pool & recommender

Every hunt feeds one cumulative listings pool, and recommendation is content-based retrieval over it: watchlist intent embeddings against listing embeddings (pgvector cosine), re-ranked by a listwise LLM pass with the buyer profile in context.

- **Sufficiency gate.** Before browsing, a hunt checks whether the pool already holds enough fresh, novel, similar listings for this watchlist. If yes, it serves from the pool ("cached" hunt, near-zero cost); if not, it hunts live. The gate fails toward hunting, and Pro users always hunt live.
- **Ranking cache.** The LLM ranker runs event-driven (hunt completion, profile edits) with a stale-while-revalidate backstop, so the read path is pure SQL at ~50ms instead of a 3-8s LLM call.
- **Lifecycle.** Listings unseen for 7 days leave the read surfaces (probably sold); after 90 days they purge. Re-sighting a listing refreshes it, including its thumbnail.

## Product surfaces

- **Mission Control**: the live theater. Each running hunt renders as a grid of lane tiles (queued, connecting, live, done) with real-time viewport frames streamed from the agent's browser, collapsing into a completed-run summary with the next-sweep countdown.
- **Daily Drops**: the catalog. Browse everything fresh in the pool with multi-select store and condition filters and a price cap, or search it in natural language (query embedding against the pool, ranked by fit).
- **My Agents**: Scout's chat, plus each agent's dossier: the profile it hunts with (editable in place, re-ranks in the background) and its current ranked matches.
- **Email alerts** (Resend): new matches per sweep, each with the ranker's one-line reason.

## Evaluation

The repo carries an eval harness (`tests/evals/`, results in `docs/evals/results.md`) that runs scripted hunt campaigns against live marketplaces with forced targets, scoring precision per run and accounting tokens and dollars per hunt.

Bedrock-era campaign (2026-08-02, nav: claude-sonnet-4-5, extract: claude-haiku-4-5):

- **Reliability: 5/5 runs**, zero error-stops, 219-380 unique offers per run at 176-259s and $2.2-3.1 per run. Roughly 10x the unique-offer recall of the July GPT-4o-era campaigns at ~2x the cost.
- **Holdout (never-tuned marketplaces, single-shot): eBay Aeron 576 unique offers at 97.7% precision**; eBay headphones 330 at 88.8%.
- **Headline: held-out mean precision 93.3% vs 72.1% on tuned sites.** The zero-adapter agent generalizes at full precision; the lower tuned-site figure is a marketplace property (FB and Kijiji pad results with related inventory, deliberately retained as pool stock and gated later by the ranker).

## Running in production

Studeal runs on AWS, defined end to end in Terraform (`infra/`): a two-AZ VPC, ECS Fargate (ARM64) running the API, the Celery worker, and exactly one beat scheduler, RDS Postgres 16 with pgvector, ElastiCache Redis, an ALB terminating TLS on an ACM certificate, and Route53 for DNS. Secrets are injected from SSM Parameter Store and Secrets Manager at container start, so no credential lives in the image or the repo.

**The browser fleet is split by what each marketplace actually demands**, decided by probing rather than assumption. Most sites serve a full results page to a bare cloud IP; a few don't, and they fail in two distinguishable ways:

| What the site requires | Backend | Sites |
|---|---|---|
| Nothing special | AgentCore browser (AWS-billed) | Kijiji · eBay · Craigslist · Newegg · OpenBox · REFURB.io · Canada Computers |
| A residential exit IP | AgentCore + prepaid residential proxy | Facebook Marketplace |
| A stealth browser fingerprint | *parked · no cheap source* | Best Buy · Visions |

That split matters because the two failure modes look identical from the outside but have different fixes. Facebook walls datacenter IPs regardless of fingerprint; Best Buy walls automation fingerprints regardless of IP. Probing each site separately turned one expensive third-party dependency into a mostly AWS-billed fleet.

Media requests (images, video, fonts) are aborted at the browser on every backend. Thumbnails survive because capture reads the `src` attribute out of the DOM rather than the downloaded pixels, which is also why no image bytes cross the metered proxy.

**Spend is metered, not estimated.** Every LLM call records its cost against a Redis day-ledger tagged with the pipeline stage that made it, surfaced at `/health/spend`:

| Stage | Share of LLM spend |
|---|---|
| Extraction (Haiku) | ~51% |
| Navigation (Haiku + Sonnet escalation) | ~41% |
| Ranking (Haiku) | ~5% |

A hunt costs roughly **$1.30** across ~6 concurrent lanes. Layered caps bound the damage in every direction: a daily LLM budget that stops background hunting at 100% and interactive requests at 150%, per-user daily hunt caps, monthly and daily caps on proxied sessions, and `FLEET_PAUSED` as a break-glass. Every guard fails **open** on a Redis error, because a metering blip must never take the product down, and logs loudly when it does.

## Stack

| Layer | Technology |
|---|---|
| Frontend | Next.js 15, TypeScript, CSS Modules, Auth.js |
| Backend | FastAPI, Python 3.12, Pydantic v2, SQLAlchemy 2 (async) |
| LLMs | AWS Bedrock: Claude Sonnet 4.5 (navigation, Scout), Claude Haiku 4.5 (extraction) · swappable `LLMClient` backends (Bedrock / OpenAI / Ollama) |
| Embeddings | Amazon Titan Multimodal G1 (1024-dim, listing photo + text fused into one vector), pgvector |
| Browser automation | Playwright over CDP · Bedrock AgentCore browser (prod) / local Chromium (dev) · residential proxy on the lanes that need one |
| Queue & events | Celery + Redis · Redis pub/sub streaming live hunt events to the UI |
| Infrastructure | Terraform · ECS Fargate (ARM64), RDS Postgres, ElastiCache, ALB + ACM, Route53 |
| Database | PostgreSQL 16 + pgvector |
| Email | Resend |
| Payments | Stripe |

## Running locally

Prerequisites: Docker, Python 3.12, Node 20+.

```bash
# 1. Postgres + Redis
docker-compose up -d

# 2. Backend API
pip install -e ".[dev]"
cp .env.example .env            # fill in credentials
alembic upgrade head
uvicorn dealbot.api.main:app --reload --port 8001 --env-file .env

# 3. Worker (hunts run here)
celery -A dealbot.worker.celery_app worker --loglevel=info

# 4. Frontend
cd frontend && npm install
# frontend/.env.local: set API_BASE_URL and AUTH_SECRET
npm run dev
```

Open [http://localhost:3000](http://localhost:3000). Tests: `python -m pytest`.

## Project structure

```
dealbot/
├── agents/          # explorer (navigation), extractor pool, perception (CDP),
│                    # marketplace router, image capture, Scout (nl_watchlist)
├── api/             # FastAPI routes: auth, watchlists, hunts, listings feed,
│                    # alerts, billing, live event stream
├── db/              # SQLAlchemy models + Alembic migrations
├── events/          # typed hunt event schema + Redis publisher
├── llm/             # LLMClient abstraction: Bedrock / OpenAI / Ollama backends
├── notifications/   # email (Resend)
├── persistence/     # canonical-URL upsert into the shared pool
├── costs.py         # per-stage spend metering + budget guards
├── recsys/          # sufficiency gate, ranking cache
├── scrapers/        # browser sessions (Browserbase / local), DOM settlement
└── worker/          # Celery tasks: hunts, ranking recompute, alerts
frontend/
└── src/app/         # Daily Drops, My Agents, Mission Control
tests/               # unit + integration; tests/evals/ is the live-hunt harness
infra/               # Terraform: VPC, ECS, RDS, ElastiCache, ALB, DNS, budgets
```

## Status

Live at **[studeal.site](https://studeal.site)**. The fleet hunts on a daily cadence against the marketplaces above, and the pool, recommender, and product surfaces are all serving from production.

Known gaps, kept here rather than in a drawer: two retailers are parked behind a browser-fingerprint wall with no cheap workaround; one marketplace has no thumbnail capture because its listing URLs carry no distinguishing pattern and its images lazy-load behind a placeholder (an honest missing image beats a wrong one); and picks trail a finished sweep by a minute or two while listings embed, which the UI says out loud instead of rendering an empty shelf.
