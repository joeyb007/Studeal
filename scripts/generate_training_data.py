#!/usr/bin/env python3
"""Generate RLAIF training data for the deal quality scorer via OpenAI Batch API.

Uses the Batch API to avoid rate limits — submits all 500 labeling requests in one
batch file, polls until complete (up to 24h), downloads results. ~50% cheaper than
the standard API and no RPM/TPM limits apply.

Run:
  python scripts/generate_training_data.py          # submit + wait
  python scripts/generate_training_data.py --check  # check status of running batch
  python scripts/generate_training_data.py --fetch <batch_id>  # fetch completed batch

Output: dealbot/ml/training_data.jsonl
Cost:   ~$0.02 (500 calls, short prompts, batch discount)
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

OPENAI_API_KEY = ""  # set after load_dotenv
OUTPUT_PATH = Path(__file__).resolve().parents[1] / "dealbot" / "ml" / "training_data.jsonl"
BATCH_ID_PATH = Path(__file__).resolve().parents[1] / "dealbot" / "ml" / ".batch_id"
OPENAI_API = "https://api.openai.com/v1"


# ---------------------------------------------------------------------------
# Feature vector
# ---------------------------------------------------------------------------

@dataclass
class DealFeatures:
    discount_pct: float        # 0-100
    has_strikethrough: float   # 0 or 1
    condition: float           # 1.0=new, 0.7=refurb, 0.4=used, 0.5=unknown
    source_trust: float        # 1.0=major CA retailer, 0.6=marketplace, 0.3=unknown
    price_percentile: float    # 0-1, 1.0=cheapest in pool
    validation_confidence: float  # 0-1
    student_eligible: float    # 0 or 1


# ---------------------------------------------------------------------------
# 15 calibrated anchors
# ---------------------------------------------------------------------------

ANCHORS = [
    (DealFeatures(40, 1, 1.0, 1.0, 0.90, 0.95, 1), 96,
     "40% off verified, new, major CA retailer, cheapest in catalog, student eligible"),
    (DealFeatures(35, 1, 1.0, 1.0, 0.85, 0.93, 0), 88,
     "genuine 35% off from trusted retailer, very cheap vs similar deals"),
    (DealFeatures(28, 1, 1.0, 1.0, 0.78, 0.91, 0), 81,
     "solid discount, trusted source, in the cheapest quartile"),
    (DealFeatures(22, 1, 0.7, 1.0, 0.72, 0.88, 0), 73,
     "20%+ off refurb from trusted retailer — good value"),
    (DealFeatures(18, 1, 1.0, 0.6, 0.65, 0.82, 0), 65,
     "decent discount but from marketplace — slightly less reliable"),
    (DealFeatures(0,  0, 0.7, 1.0, 0.62, 0.88, 0), 58,
     "no tracked discount but refurb from major retailer at fair pool price"),
    (DealFeatures(12, 1, 1.0, 0.6, 0.48, 0.78, 0), 50,
     "modest discount, marketplace, mid-range pool position"),
    (DealFeatures(8,  1, 0.4, 0.6, 0.50, 0.72, 0), 42,
     "small discount on used item from marketplace"),
    (DealFeatures(0,  0, 0.5, 0.6, 0.38, 0.70, 0), 35,
     "no discount, unknown condition, marketplace, above median price"),
    (DealFeatures(0,  0, 1.0, 0.6, 0.30, 0.68, 0), 28,
     "full price new item from marketplace, nothing special"),
    (DealFeatures(0,  0, 0.5, 0.3, 0.28, 0.60, 0), 20,
     "no discount, unknown source, expensive vs catalog"),
    (DealFeatures(0,  0, 0.5, 0.3, 0.18, 0.55, 0), 12,
     "full price, unknown everything, one of the most expensive in catalog"),
    (DealFeatures(0,  0, 0.5, 0.3, 0.08, 0.50, 0), 6,
     "worst price in catalog, no signals of legitimacy"),
    (DealFeatures(55, 1, 1.0, 0.3, 0.55, 0.58, 0), 48,
     "high % off but low trust source and moderate confidence — suspicious deep discount"),
    (DealFeatures(0,  0, 1.0, 1.0, 0.88, 0.92, 0), 62,
     "no tracked discount but new item cheapest in catalog from major retailer — genuinely good value"),
]


def _build_system_prompt() -> str:
    anchor_lines = ["Calibration anchors (treat these as ground truth for your scale):"]
    for f, score, why in ANCHORS:
        anchor_lines.append(f"  {json.dumps(asdict(f))} → {score}  # {why}")

    return f"""You are evaluating deal quality for a Canadian student deal-hunting app.

Rate deals 0-100 where:
  81-100: exceptional — strong discount, trusted source, excellent value
  61-80:  good        — meaningful saving from reliable source
  41-60:  decent      — genuine saving, nothing standout
  21-40:  weak        — minor savings or uncertain source
  0-20:   poor        — full price, untrustworthy, or very low confidence

Feature definitions:
  discount_pct          — % off the listed/was price (0 = no tracked discount)
  has_strikethrough     — 1 = verified was/is price exists, 0 = not verified
  condition             — 1.0=new  0.7=refurb  0.4=used  0.5=unknown
  source_trust          — 1.0=major CA retailer (Amazon/BestBuy/Walmart)
                          0.6=marketplace (eBay/Poshmark)
                          0.3=unknown/small seller
  price_percentile      — 0-1, where 1.0 = cheapest vs similar deals in catalog
  validation_confidence — 0-1, system confidence this is a real legitimate deal
  student_eligible      — 1 = confirmed student discount available

Key interactions:
- High discount_pct WITH has_strikethrough=1 is strong (the discount is verified)
- High discount_pct WITH has_strikethrough=0 is weaker (could be inflated MSRP)
- Good price_percentile even without a tracked discount still means good value
- Low source_trust should dampen your score even if other signals look good
- Low validation_confidence means the system isn't sure this is a real deal

{chr(10).join(anchor_lines)}

Return only JSON: {{"score": N, "reasoning": "one sentence"}}"""


SYSTEM_PROMPT = _build_system_prompt()


# ---------------------------------------------------------------------------
# Feature vector generation
# ---------------------------------------------------------------------------

def _clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def generate_examples(n: int = 500) -> list[DealFeatures]:
    examples: list[DealFeatures] = []

    # Stratified sweep — guarantees coverage of key feature combinations
    for discount in [0, 5, 10, 20, 30, 40, 55]:
        for condition in [0.4, 0.5, 0.7, 1.0]:
            for source_trust in [0.3, 0.6, 1.0]:
                has_s = 1.0 if discount > 0 else 0.0
                pp = _clip(discount / 80 + random.uniform(0.1, 0.3), 0.1, 0.95)
                vc = _clip(0.5 + source_trust * 0.3 + random.uniform(-0.1, 0.1), 0.4, 0.98)
                examples.append(DealFeatures(
                    discount_pct=float(discount),
                    has_strikethrough=has_s,
                    condition=condition,
                    source_trust=source_trust,
                    price_percentile=round(pp, 2),
                    validation_confidence=round(vc, 2),
                    student_eligible=random.choice([0.0, 0.0, 0.0, 1.0]),
                ))

    # Edge cases — teach nuanced tradeoffs
    edge_cases = [
        DealFeatures(70, 1, 1.0, 0.3, 0.55, 0.52, 0),
        DealFeatures(80, 1, 1.0, 0.3, 0.60, 0.48, 0),
        DealFeatures(0, 0, 1.0, 1.0, 0.92, 0.94, 0),
        DealFeatures(0, 0, 1.0, 1.0, 0.88, 0.91, 1),
        DealFeatures(30, 1, 0.7, 1.0, 0.80, 0.90, 0),
        DealFeatures(25, 1, 0.7, 1.0, 0.75, 0.88, 1),
        DealFeatures(0,  0, 0.7, 1.0, 0.70, 0.87, 0),
        DealFeatures(40, 1, 0.4, 1.0, 0.65, 0.82, 0),
        DealFeatures(0,  0, 0.4, 1.0, 0.60, 0.80, 0),
        DealFeatures(35, 1, 1.0, 1.0, 0.80, 0.45, 0),
        DealFeatures(25, 1, 1.0, 1.0, 0.75, 0.40, 0),
        DealFeatures(20, 1, 1.0, 1.0, 0.70, 0.90, 1),
        DealFeatures(0,  0, 1.0, 0.6, 0.50, 0.75, 1),
        DealFeatures(0,  0, 0.5, 0.3, 0.30, 0.60, 1),
        DealFeatures(25, 1, 1.0, 0.6, 0.72, 0.80, 0),
        DealFeatures(40, 1, 0.7, 0.6, 0.68, 0.78, 0),
        DealFeatures(0, 0, 0.5, 0.3, 0.95, 0.60, 0),
        DealFeatures(0, 0, 0.5, 0.6, 0.90, 0.72, 0),
        DealFeatures(15, 1, 1.0, 1.0, 0.50, 0.85, 0),
        DealFeatures(10, 0, 1.0, 1.0, 0.55, 0.88, 0),
    ]
    examples.extend(edge_cases)

    # Random fill
    while len(examples) < n:
        discount = random.choice([0, 0, 5, 10, 15, 20, 25, 30, 35, 40, 50])
        cond = random.choice([0.4, 0.5, 0.5, 0.7, 0.7, 1.0, 1.0, 1.0])
        trust = random.choice([0.3, 0.6, 0.6, 1.0, 1.0, 1.0])
        has_s = 1.0 if (discount > 0 and random.random() > 0.2) else 0.0
        pp = _clip(random.betavariate(2, 2), 0.05, 0.98)
        vc = _clip(trust * 0.5 + random.uniform(0.2, 0.5), 0.3, 0.99)
        se = 1.0 if random.random() < 0.15 else 0.0
        examples.append(DealFeatures(
            discount_pct=float(discount) + random.uniform(-2, 2) if discount > 0 else 0.0,
            has_strikethrough=has_s,
            condition=cond,
            source_trust=trust,
            price_percentile=round(pp, 2),
            validation_confidence=round(vc, 2),
            student_eligible=se,
        ))

    random.shuffle(examples)
    return examples[:n]


# ---------------------------------------------------------------------------
# Batch API helpers
# ---------------------------------------------------------------------------

def build_batch_requests(examples: list[DealFeatures]) -> list[dict]:
    """Format each example as a batch API request line."""
    requests = []
    for i, f in enumerate(examples):
        requests.append({
            "custom_id": f"deal-{i:04d}",
            "method": "POST",
            "url": "/v1/chat/completions",
            "body": {
                "model": "gpt-4o",
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": f"Rate this deal:\n{json.dumps(asdict(f), indent=2)}"},
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0.2,
                "max_tokens": 100,
            },
        })
    return requests


def submit_batch(requests: list[dict]) -> str:
    """Upload JSONL file and submit batch. Returns batch_id."""
    # Write requests to temp JSONL
    tmp = Path("/tmp/deal_scorer_batch.jsonl")
    with open(tmp, "w") as f:
        for r in requests:
            f.write(json.dumps(r) + "\n")

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }

    # Upload file
    print("Uploading batch file...")
    with open(tmp, "rb") as f:
        resp = httpx.post(
            f"{OPENAI_API}/files",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            files={"file": ("batch.jsonl", f, "application/json")},
            data={"purpose": "batch"},
            timeout=60,
        )
    resp.raise_for_status()
    file_id = resp.json()["id"]
    print(f"File uploaded: {file_id}")

    # Submit batch
    resp = httpx.post(
        f"{OPENAI_API}/batches",
        headers=headers,
        json={
            "input_file_id": file_id,
            "endpoint": "/v1/chat/completions",
            "completion_window": "24h",
        },
        timeout=30,
    )
    resp.raise_for_status()
    batch = resp.json()
    batch_id = batch["id"]
    print(f"Batch submitted: {batch_id}")
    print(f"Status: {batch['status']}")
    return batch_id


def check_batch(batch_id: str) -> dict:
    """Return current batch status."""
    resp = httpx.get(
        f"{OPENAI_API}/batches/{batch_id}",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_results(batch: dict, examples: list[DealFeatures]) -> None:
    """Download completed batch results, parse, save to training_data.jsonl."""
    output_file_id = batch.get("output_file_id")
    if not output_file_id:
        print(f"ERROR: no output_file_id on batch. Status: {batch['status']}")
        print(f"Errors: {batch.get('errors')}")
        return

    resp = httpx.get(
        f"{OPENAI_API}/files/{output_file_id}/content",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
        timeout=60,
    )
    resp.raise_for_status()

    # Map custom_id → features
    id_to_features = {f"deal-{i:04d}": f for i, f in enumerate(examples)}

    records = []
    errors = 0
    for line in resp.text.strip().splitlines():
        result = json.loads(line)
        custom_id = result["custom_id"]
        features = id_to_features.get(custom_id)
        if not features:
            errors += 1
            continue
        try:
            content = result["response"]["body"]["choices"][0]["message"]["content"]
            data = json.loads(content)
            score = int(data.get("score", -1))
            reasoning = data.get("reasoning", "")
            if 0 <= score <= 100:
                records.append({
                    "features": asdict(features),
                    "score": score,
                    "reasoning": reasoning,
                })
            else:
                errors += 1
        except Exception:
            errors += 1

    print(f"\nParsed {len(records)} records ({errors} errors)")

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    print(f"Saved → {OUTPUT_PATH}")

    # Score distribution
    scores = [r["score"] for r in records]
    print("\nScore distribution:")
    for lo, hi in [(0, 20), (21, 40), (41, 60), (61, 80), (81, 100)]:
        count = sum(1 for s in scores if lo <= s <= hi)
        bar = "█" * (count // 5)
        print(f"  {lo:3d}-{hi:3d}: {bar} ({count})")

    consistency_check(records)


def consistency_check(records: list[dict]) -> None:
    keys = list(asdict(DealFeatures(0, 0, 0, 0, 0, 0, 0)).keys())

    def dist(a: dict, b: dict) -> float:
        return math.sqrt(sum((a[k] - b[k]) ** 2 for k in keys))

    flags = []
    for i in range(len(records)):
        for j in range(i + 1, len(records)):
            fa, fb = records[i]["features"], records[j]["features"]
            if dist(fa, fb) < 0.15 and abs(records[i]["score"] - records[j]["score"]) > 25:
                flags.append((i, j, records[i]["score"], records[j]["score"]))

    if flags:
        print(f"\n⚠  {len(flags)} inconsistent pairs:")
        for i, j, sa, sb in flags[:5]:
            print(f"  #{i} score={sa}  vs  #{j} score={sb}")
    else:
        print("\n✓  Consistency check passed")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def poll_until_done(batch_id: str, examples: list[DealFeatures]) -> None:
    """Poll batch status and fetch results when complete."""
    print(f"\nPolling batch {batch_id}...")
    while True:
        batch = check_batch(batch_id)
        status = batch["status"]
        counts = batch.get("request_counts", {})
        print(f"  Status: {status} | completed: {counts.get('completed', 0)}/{counts.get('total', '?')}")

        if status == "completed":
            fetch_results(batch, examples)
            BATCH_ID_PATH.unlink(missing_ok=True)
            return
        elif status in ("failed", "expired", "cancelled"):
            print(f"Batch {status}. Check OpenAI dashboard.")
            return

        time.sleep(30)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="Check status of running batch")
    parser.add_argument("--fetch", metavar="BATCH_ID", help="Fetch completed batch by ID")
    args = parser.parse_args()

    if args.check:
        if not BATCH_ID_PATH.exists():
            print("No batch ID saved. Run without flags to submit a new batch.")
            return
        batch_id = BATCH_ID_PATH.read_text().strip()
        batch = check_batch(batch_id)
        print(json.dumps(batch, indent=2))
        return

    if args.fetch:
        print(f"Fetching batch {args.fetch}...")
        examples = generate_examples(500)
        batch = check_batch(args.fetch)
        fetch_results(batch, examples)
        return

    # Default: generate + submit + poll
    print("Generating 500 feature vectors...")
    examples = generate_examples(500)
    print(f"Generated {len(examples)} examples")

    requests = build_batch_requests(examples)
    batch_id = submit_batch(requests)

    # Save batch ID so we can check later
    BATCH_ID_PATH.parent.mkdir(parents=True, exist_ok=True)
    BATCH_ID_PATH.write_text(batch_id)
    print(f"\nBatch ID saved to {BATCH_ID_PATH}")
    print("Polling for completion (checks every 30s, up to 24h)...")
    poll_until_done(batch_id, examples)


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
    if not OPENAI_API_KEY:
        print("ERROR: OPENAI_API_KEY not set")
        sys.exit(1)
    main()
