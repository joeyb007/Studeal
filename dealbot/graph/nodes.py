from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import date, datetime, timezone

import httpx
from bs4 import BeautifulSoup
from sqlalchemy.exc import IntegrityError

from dealbot.agents.extractor import ExtractorAgent
from dealbot.agents.query_gen import QueryGenAgent
from dealbot.agents.scorer import ScorerAgent
from dealbot.db.database import get_async_session
from dealbot.db.models import Deal
from dealbot.db.rag import keyword_covered_today, retrieve_similar_deals
from dealbot.graph.state import PipelineState
from dealbot.llm.base import LLMClient
from dealbot.llm.embeddings import embed_text
from dealbot.search.brave import BraveSearchClient
from dealbot.search.client import FetchedPage, SearchResult
from dealbot.worker.matching import run_matching

_BRAVE_N = int(os.environ.get("BRAVE_SEARCH_N", "10"))
_FETCH_TIMEOUT = 10  # seconds per page
_MAX_TEXT_CHARS = 4000

# Replace with your Amazon Associates tag at deploy time
AMAZON_AFFILIATE_TAG = "dealbot-20"


def _affiliate_url(url: str, asin: str | None) -> str:
    """Rewrite Amazon product URLs to include the affiliate tag."""
    if asin:
        return f"https://www.amazon.com/dp/{asin}?tag={AMAZON_AFFILIATE_TAG}"
    return url


def _similar_deals_context(similar: list[Deal]) -> str | None:
    """Format retrieved deals into a context string for the scorer's system prompt."""
    if not similar:
        return None
    lines = ["Similar deals scored previously (use for market context):"]
    for d in similar:
        lines.append(
            f"- {d.title}: score={d.score}, tier={d.alert_tier}, "
            f"category={d.category}, sale_price=${d.sale_price:.2f}"
        )
    return "\n".join(lines)


logger = logging.getLogger(__name__)


# --- Helpers ----------------------------------------------------------------

async def _fetch_page(result: SearchResult) -> FetchedPage | None:
    """Fetch a URL and return stripped plain text, or None on failure."""
    try:
        async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(result.url, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            html = resp.text

        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
            tag.decompose()

        text = soup.get_text(separator=" ", strip=True)[:_MAX_TEXT_CHARS]

        # Extract absolute hrefs for ReAct navigation
        base_url = str(resp.url)
        links: list[str] = []
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if href.startswith("http"):
                links.append(href)
            elif href.startswith("/"):
                from urllib.parse import urlparse
                parsed = urlparse(base_url)
                links.append(f"{parsed.scheme}://{parsed.netloc}{href}")
        links = list(dict.fromkeys(links))[:20]  # dedup, cap at 20

        return FetchedPage(url=result.url, text=text, links=links)
    except Exception:
        logger.debug("fetch_page: failed to fetch %s", result.url)
        return None


# --- Nodes ------------------------------------------------------------------

async def keyword_dedup_node(state: PipelineState) -> PipelineState:
    """Skip the hunt if a semantically similar keyword was already searched today."""
    keyword = state.get("keyword", "")
    embedding = await embed_text(keyword)
    if not embedding:
        return {**state, "keyword_covered": False}

    async with get_async_session() as session:
        covered = await keyword_covered_today(embedding, session)

    if covered:
        logger.info("keyword_dedup_node: '%s' already covered today, skipping", keyword)
    return {**state, "keyword_covered": covered}


async def query_gen_node(state: PipelineState, llm: LLMClient) -> PipelineState:
    """Generate search query variants for a watchlist keyword."""
    keyword = state["keyword"]
    logger.info("query_gen_node: keyword=%r", keyword)
    agent = QueryGenAgent(llm=llm)
    queries = await agent.generate(keyword)
    logger.info("query_gen_node: generated %d queries", len(queries))
    return {**state, "queries": queries}


async def hunt_node(state: PipelineState) -> PipelineState:
    """Search Brave for each query variant, deduplicate results by URL."""
    queries = state.get("queries", [])
    client = BraveSearchClient()
    seen_urls: set[str] = set()
    all_results: list[SearchResult] = []

    for query in queries:
        results = await client.search(query, n=_BRAVE_N)
        for r in results:
            if r.url not in seen_urls:
                seen_urls.add(r.url)
                all_results.append(r)

    logger.info("hunt_node: %d unique results across %d queries", len(all_results), len(queries))
    return {**state, "search_results": all_results}


async def fetch_node(state: PipelineState) -> PipelineState:
    """Fetch and strip HTML for each search result URL concurrently."""
    search_results = state.get("search_results", [])
    pages_or_none = await asyncio.gather(
        *[_fetch_page(r) for r in search_results],
        return_exceptions=False,
    )
    fetched = [p for p in pages_or_none if p is not None]
    logger.info("fetch_node: fetched %d/%d pages", len(fetched), len(search_results))
    return {**state, "fetched_pages": fetched}


async def extract_node(state: PipelineState, llm: LLMClient) -> PipelineState:
    """Run ExtractorAgent over each fetched page concurrently, drop failures."""
    pages = state.get("fetched_pages", [])
    agent = ExtractorAgent(llm=llm)
    results = await asyncio.gather(
        *[agent.extract(p) for p in pages],
        return_exceptions=False,
    )
    candidates = [r for r in results if r is not None]
    logger.info("extract_node: extracted %d candidates from %d pages", len(candidates), len(pages))
    return {**state, "candidates": candidates}


async def ingest_node(state: PipelineState) -> PipelineState:
    """
    Validates the incoming DealRaw and passes it through.
    In a later phase this will pull from a Redis stream instead.
    """
    logger.info("ingest_node: deal=%s source=%s", state["deal"].title, state["deal"].source)
    return state


async def score_node(state: PipelineState, llm: LLMClient) -> PipelineState:
    """Embeds the deal, retrieves similar deals via RAG, then runs ScorerAgent."""
    deal = state["deal"]
    logger.info("score_node: scoring '%s'", deal.title)

    try:
        # 1. Generate embedding for this deal
        deal_text = f"{deal.title} {deal.description or ''}".strip()
        embedding = await embed_text(deal_text)

        # 2. RAG: retrieve similar historical deals
        similar: list[Deal] = []
        if embedding:
            async with get_async_session() as session:
                similar = await retrieve_similar_deals(embedding, session)
            logger.debug("score_node: retrieved %d similar deals", len(similar))

        # 3. Score with context
        scorer = ScorerAgent(llm=llm)
        score_result = await scorer.score(
            deal,
            similar_context=_similar_deals_context(similar),
        )
        logger.info(
            "score_node: score=%d tier=%s confidence=%s",
            score_result.score,
            score_result.alert_tier,
            score_result.confidence,
        )
        return {**state, "score_result": score_result, "embedding": embedding}
    except Exception as exc:
        logger.exception("score_node: failed to score deal '%s'", deal.title)
        return {**state, "error": str(exc)}


async def persist_node(state: PipelineState) -> PipelineState:
    """Writes DealScore + embedding to Postgres via SQLAlchemy. Skipped silently if error is set."""
    if "error" in state:
        logger.warning("persist_node: skipping due to upstream error: %s", state["error"])
        return state

    score_result = state.get("score_result")
    if score_result is None:
        logger.warning("persist_node: no score_result in state, skipping")
        return state

    deal = score_result.deal
    embedding = state.get("embedding") or None

    values = dict(
        title=deal.title,
        source=deal.source,
        url=_affiliate_url(deal.url, deal.asin),
        listed_price=deal.listed_price,
        sale_price=deal.sale_price,
        asin=deal.asin,
        score=score_result.score,
        alert_tier=score_result.alert_tier.value,
        category=score_result.category.value,
        tags=json.dumps(score_result.tags),
        confidence=score_result.confidence,
        real_discount_pct=score_result.real_discount_pct,
        student_eligible=deal.student_eligible,
        condition=score_result.condition.value,
        embedding=embedding,
        hunt_date=date.today(),
        scraped_at=datetime.now(timezone.utc),
    )

    async with get_async_session() as session:
        try:
            row = Deal(**values)
            session.add(row)
            await session.flush()  # get row.id without closing the session
            await run_matching(row, session)
            await session.commit()
            logger.info("persist_node: saved deal '%s' with score %d", deal.title, score_result.score)
        except IntegrityError:
            await session.rollback()
            logger.info("persist_node: duplicate skipped '%s'", deal.url)
    return state
