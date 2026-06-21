"""
FlyScout — Card Network Rule Tracker
======================================
Runs on a schedule via GitHub Actions (see .github/workflows/check-updates.yml).
No server to host, no Python to run locally — GitHub executes this for you.

What it does, every run:
  1. Fetches each source in SOURCES
  2. Compares content against the last known snapshot (data/snapshots.json)
  3. On a real change: extracts rate numbers if present, classifies what likely
     triggered it, appends a detailed entry to data/rule-changes.json, and
     sends a detailed Slack alert (nothing generic — source, market, network,
     category, before/after, trigger type, link)
  4. Recomputes the prediction engine (data/predictions.json) from the full
     change history, and sends a Slack alert ONLY when a prediction newly
     becomes "due" or "pending" (not on every run — avoids spam)
  5. Commits updated data/*.json back to the repo (handled by the workflow
     file's git commit step, not by this script)

Maintaining this: add/remove/edit entries in SOURCES below. That's the whole
maintenance surface — no other file needs to change to track a new source.
"""
import os
import re
import json
import hashlib
import logging
from datetime import datetime, timezone, timedelta

import requests
from bs4 import BeautifulSoup

# ── CONFIG ────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
CHANGES_FILE = os.path.join(DATA_DIR, "rule-changes.json")
PREDICTIONS_FILE = os.path.join(DATA_DIR, "predictions.json")
SNAPSHOTS_FILE = os.path.join(DATA_DIR, "snapshots.json")

SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
REQUEST_TIMEOUT = 20
USER_AGENT = "FlyScoutBot/1.0 (+card-network-rule-tracker; Flywire Cards Network team)"

MARKETS = ["US", "UK", "EU", "AU", "SG", "CA", "JP"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("flyscout")


# ── SOURCE REGISTRY ──────────────────────────────────────────────────
# This is the entire maintenance surface. Add a dict to add a source.
# `network`: "Visa" | "Mastercard" | "Both" | "Regulator"
# `category`: "interchange" | "surcharge" | "regulatory" (regulatory = general
#             regulator page that may cover either category — classified per-change)
SOURCES = [
    # ── UNITED STATES ──
    {"id": "us_visa_ic", "market": "US", "network": "Visa", "category": "interchange",
     "name": "Visa USA Interchange Reimbursement Fees", "cnp": False,
     "url": "https://usa.visa.com/support/consumer/visa-rules.html"},
    {"id": "us_mc_ic", "market": "US", "network": "Mastercard", "category": "interchange",
     "name": "Mastercard US Interchange Programs & Rates", "cnp": False,
     "url": "https://www.mastercard.us/en-us/business/overview/merchant-acquiring/interchange.html"},

    # ── UNITED KINGDOM ──
    {"id": "uk_psr", "market": "UK", "network": "Regulator", "category": "regulatory",
     "name": "UK Payment Systems Regulator — Card Acquiring", "cnp": True,
     "url": "https://www.psr.org.uk/our-work/card-acquiring/"},
    {"id": "uk_mc_ic", "market": "UK", "network": "Mastercard", "category": "interchange",
     "name": "Mastercard UK Interchange Rates", "cnp": False,
     "url": "https://www.mastercard.co.uk/en-gb/business/overview/merchant-acquiring/interchange-rates.html"},

    # ── EUROPEAN UNION ──
    {"id": "eu_eba", "market": "EU", "network": "Regulator", "category": "regulatory",
     "name": "EBA — Payment Services & Interchange", "cnp": False,
     "url": "https://www.eba.europa.eu/regulation-and-policy/payment-services-and-electronic-money"},
    {"id": "eu_visa_ic", "market": "EU", "network": "Visa", "category": "interchange",
     "name": "Visa Europe Interchange Fees", "cnp": False,
     "url": "https://www.visaeurope.com/making-payments/interchange/"},

    # ── AUSTRALIA ──
    {"id": "au_rba", "market": "AU", "network": "Regulator", "category": "regulatory",
     "name": "RBA — Card Payments Regulation & Surcharging", "cnp": True,
     "url": "https://www.rba.gov.au/payments-and-infrastructure/payments-system/card-payments-regulation/"},
    {"id": "au_mc_ic", "market": "AU", "network": "Mastercard", "category": "interchange",
     "name": "Mastercard Australia Interchange Schedule", "cnp": False,
     "url": "https://www.mastercard.com.au/en-au/business/overview/merchant-acquiring.html"},

    # ── SINGAPORE ──
    {"id": "sg_mas", "market": "SG", "network": "Regulator", "category": "regulatory",
     "name": "MAS — Payments Regulation", "cnp": True,
     "url": "https://www.mas.gov.sg/regulation/payments"},

    # ── CANADA ──
    {"id": "ca_fcac", "market": "CA", "network": "Regulator", "category": "regulatory",
     "name": "FCAC — Payment Card Code of Conduct", "cnp": True,
     "url": "https://www.canada.ca/en/financial-consumer-agency/programs/payment-cards.html"},
    {"id": "ca_visa_ic", "market": "CA", "network": "Visa", "category": "interchange",
     "name": "Visa Canada Interchange Rates", "cnp": False,
     "url": "https://www.visa.ca/en_ca/about-visa/interchange/"},

    # ── JAPAN ──
    {"id": "jp_fsa", "market": "JP", "network": "Regulator", "category": "regulatory",
     "name": "Japan FSA — Payment Services Policy", "cnp": False,
     "url": "https://www.fsa.go.jp/en/policy/payserv/index.html"},
]


# ── KEYWORD CLASSIFICATION ───────────────────────────────────────────
CATEGORY_KEYWORDS = {
    "interchange": ["interchange", "reimbursement fee", "merchant discount rate", "ifr cap"],
    "surcharge": ["surcharge", "surcharging", "cost of acceptance", "checkout fee"],
}
CNP_KEYWORDS = ["card-not-present", "card not present", "cnp", "e-commerce", "ecommerce", "online transaction"]

TRIGGER_KEYWORDS = {
    "regulatory_order": ["regulation", "mandate", "shall not exceed", "statutory", "compliance deadline"],
    "government_consultation": ["consultation", "review", "feedback", "submission", "discussion paper", "proposed"],
    "network_policy_update": ["effective", "bulletin", "rule change", "scheme update", "network announces"],
    "scheme_review": ["periodic review", "scheduled review"],
}

RATE_PATTERN = re.compile(r"\b(\d{1,2}\.\d{1,3})\s*%")


def classify_category(text, default_category):
    if default_category in ("interchange", "surcharge"):
        return default_category
    tl = text.lower()
    scores = {cat: sum(tl.count(k) for k in kws) for cat, kws in CATEGORY_KEYWORDS.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "interchange"


def classify_cnp(text, source_cnp_flag):
    if source_cnp_flag:
        return True
    tl = text.lower()
    return any(k in tl for k in CNP_KEYWORDS)


def classify_trigger(text):
    tl = text.lower()
    scores = {t: sum(tl.count(k) for k in kws) for t, kws in TRIGGER_KEYWORDS.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "internal_capture"


def extract_rates(text):
    """Return a list of percentage values found, e.g. ['0.20', '0.30']."""
    return RATE_PATTERN.findall(text or "")


def diff_rates(old_text, new_text):
    """If old and new both contain rate numbers and they differ, return (old, new) strings."""
    old_rates = extract_rates(old_text)
    new_rates = extract_rates(new_text)
    if old_rates and new_rates and old_rates != new_rates:
        return (", ".join(r + "%" for r in old_rates[:4]),
                ", ".join(r + "%" for r in new_rates[:4]))
    return None, None


# ── FETCH & EXTRACT ──────────────────────────────────────────────────
def fetch(url):
    headers = {"User-Agent": USER_AGENT}
    r = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r


def extract_text(resp):
    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]):
        tag.decompose()
    text = soup.get_text(separator=" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def content_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


# ── STORAGE HELPERS ──────────────────────────────────────────────────
def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ── SLACK ALERTS ──────────────────────────────────────────────────────
def slack_post(payload):
    if not SLACK_WEBHOOK_URL:
        log.info("No SLACK_WEBHOOK_URL set — skipping Slack post (would have sent: %s)",
                  payload.get("text", "")[:80])
        return
    try:
        r = requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=10)
        if r.status_code >= 300:
            log.warning("Slack webhook returned %s: %s", r.status_code, r.text[:200])
    except requests.exceptions.RequestException as e:
        log.warning("Slack post failed: %s", e)


def send_change_alert(change):
    """Detailed 'something changed' alert — every field, nothing generic."""
    old_val = change["old_value"] or "—"
    new_val = change["new_value"] or "—"
    cnp_tag = " · CNP-focused" if change["cnp"] else ""
    trigger_label = change["trigger"].replace("_", " ").title()

    blocks = [
        {"type": "header", "text": {"type": "plain_text",
            "text": f"📋 FlyScout: {change['market']} {change['network']} {change['category']} change detected"}},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*Market:*\n{change['market']}"},
            {"type": "mrkdwn", "text": f"*Network:*\n{change['network']}"},
            {"type": "mrkdwn", "text": f"*Category:*\n{change['category'].title()}{cnp_tag}"},
            {"type": "mrkdwn", "text": f"*Likely trigger:*\n{trigger_label}"},
        ]},
        {"type": "section", "text": {"type": "mrkdwn",
            "text": f"*What changed:*\n{change['summary']}"}},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*Before:*\n{old_val}"},
            {"type": "mrkdwn", "text": f"*After:*\n{new_val}"},
        ]},
    ]
    if change.get("new_snippet"):
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
            "text": f"*Source text:*\n>{change['new_snippet'][:300]}"}})
    blocks.append({"type": "actions", "elements": [
        {"type": "button", "text": {"type": "plain_text", "text": "🔗 Open Source"},
         "url": change["source_url"], "action_id": "open_source"},
    ]})
    blocks.append({"type": "context", "elements": [
        {"type": "mrkdwn", "text": f"Detected {change['detected_at']} · Source: {change['source_name']} · FlyScout"}]})

    slack_post({"text": f"📋 FlyScout: {change['market']} {change['network']} change detected", "blocks": blocks})


def send_prediction_alert(pred):
    """Predictive 'this looks likely to change soon' alert — only on state change."""
    status_label = {"due": "🔮 Looks due for a change", "pending": "⏳ Open consultation, no rule change yet"}.get(
        pred["status"], pred["status"])

    blocks = [
        {"type": "header", "text": {"type": "plain_text",
            "text": f"{status_label}: {pred['market']} {pred['network']} {pred['category']}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Reasoning:*\n{pred['reasoning']}"}},
        {"type": "context", "elements": [
            {"type": "mrkdwn", "text": f"Based on {len(pred['evidence_ids'])} historical entr{'y' if len(pred['evidence_ids'])==1 else 'ies'} · Computed {pred['computed_at']} · FlyScout prediction engine (rule-based, not AI-generated)"}]},
    ]
    slack_post({"text": f"{status_label}: {pred['market']} {pred['network']} {pred['category']}", "blocks": blocks})


# ── CHANGE DETECTION ─────────────────────────────────────────────────
def check_source(source, snapshots, changes_store):
    sid = source["id"]
    try:
        resp = fetch(source["url"])
        text = extract_text(resp)
    except requests.exceptions.RequestException as e:
        log.warning("[%s] fetch failed: %s", sid, e)
        return None

    h = content_hash(text)
    prev = snapshots.get(sid)
    snapshots[sid] = {"hash": h, "text": text[:8000], "checked_at": now_iso()}

    if prev is None:
        log.info("[%s] baseline established", sid)
        return None

    if prev["hash"] == h:
        log.info("[%s] no change", sid)
        return None

    # ── Real change detected ──
    category = classify_category(text, source["category"] if source["category"] != "regulatory" else None)
    cnp = classify_cnp(text, source["cnp"])
    trigger = classify_trigger(text)
    old_val, new_val = diff_rates(prev.get("text", ""), text)

    # Find a snippet around the first rate number, or just the start of content, for context
    snippet_match = RATE_PATTERN.search(text)
    if snippet_match:
        start = max(0, snippet_match.start() - 120)
        new_snippet = text[start:start + 280]
    else:
        new_snippet = text[:280]

    change = {
        "id": f"chg_{sid}_{int(datetime.now().timestamp())}",
        "market": source["market"],
        "network": source["network"],
        "category": category,
        "cnp": cnp,
        "title": f"{source['name']} updated",
        "summary": f"Content change detected on {source['name']}. "
                   f"{'Rate values changed.' if old_val else 'Review source for details — no specific rate pattern was automatically extracted.'}",
        "old_value": old_val,
        "new_value": new_val,
        "trigger": trigger,
        "effective_date": None,
        "detected_at": now_iso(),
        "source_name": source["name"],
        "source_url": source["url"],
        "old_snippet": (prev.get("text", "") or "")[:280],
        "new_snippet": new_snippet,
        "reviewed": False,
        "auto_detected": True,
    }
    changes_store.append(change)
    log.info("[%s] CHANGE DETECTED — %s/%s/%s, trigger=%s", sid, change["market"], change["network"], category, trigger)
    send_change_alert(change)
    return change


# ── PREDICTION ENGINE (rule-based, not AI) ───────────────────────────
def compute_predictions(all_changes, prev_predictions):
    """
    For each market+network+category combination with 2+ historical changes,
    compute the average gap between changes and flag as 'due' if overdue.
    For combinations with an open government_consultation and no subsequent
    rate change, flag as 'pending'.
    """
    prev_by_key = {f"{p['market']}|{p['network']}|{p['category']}": p for p in prev_predictions}
    predictions = []

    # Group changes by market+network+category
    groups = {}
    for c in all_changes:
        key = f"{c['market']}|{c['network']}|{c['category']}"
        groups.setdefault(key, []).append(c)

    for key, entries in groups.items():
        market, network, category = key.split("|")
        entries_sorted = sorted(entries, key=lambda c: c["detected_at"])
        status, reasoning = None, None

        # Rule A: due-for-change based on historical cadence
        if len(entries_sorted) >= 2:
            dates = []
            for e in entries_sorted:
                d = e.get("effective_date") or e["detected_at"]
                try:
                    dt = datetime.fromisoformat(d.replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    dates.append(dt)
                except ValueError:
                    continue
            if len(dates) >= 2:
                gaps = [(dates[i+1] - dates[i]).days for i in range(len(dates)-1)]
                avg_gap_days = sum(gaps) / len(gaps)
                days_since_last = (datetime.now(timezone.utc) - dates[-1]).days
                if avg_gap_days > 0 and days_since_last >= avg_gap_days:
                    status = "due"
                    reasoning = (f"{market} {network} {category} has changed {len(entries_sorted)} time(s) on record, "
                                 f"averaging one change every {int(avg_gap_days)} days. "
                                 f"It has been {days_since_last} days since the last change — overdue by historical pattern.")

        # Rule B: open consultation with no resolved change yet
        consultations = [e for e in entries_sorted if e["trigger"] == "government_consultation"]
        non_consultations = [e for e in entries_sorted if e["trigger"] != "government_consultation"]
        if consultations and (not non_consultations or non_consultations[-1]["detected_at"] < consultations[-1]["detected_at"]):
            status = "pending"
            reasoning = (f"An open government consultation was logged for {market} {network} {category} "
                         f"({consultations[-1]['source_name']}, {consultations[-1]['detected_at'][:10]}) "
                         f"with no resulting rule change captured since. Historically, consultations like this "
                         f"often precede a rule change.")

        if status:
            pred = {
                "id": f"pred_{market}_{network}_{category}".lower().replace(" ", "_"),
                "market": market, "network": network, "category": category,
                "status": status, "reasoning": reasoning,
                "evidence_ids": [e["id"] for e in entries_sorted[-3:]],
                "computed_at": now_iso(),
            }
            predictions.append(pred)

            # Alert only on state change (new "due"/"pending" that wasn't there before)
            prev_status = prev_by_key.get(key, {}).get("status")
            if prev_status != status:
                send_prediction_alert(pred)

    return predictions


# ── MAIN ──────────────────────────────────────────────────────────────
def main():
    snapshots = load_json(SNAPSHOTS_FILE, {})
    changes_data = load_json(CHANGES_FILE, {"_meta": {}, "changes": []})
    predictions_data = load_json(PREDICTIONS_FILE, {"_meta": {}, "predictions": []})

    changes_store = changes_data["changes"]
    new_changes = []

    for source in SOURCES:
        result = check_source(source, snapshots, changes_store)
        if result:
            new_changes.append(result)

    changes_data["_meta"]["last_updated"] = now_iso()
    changes_data["_meta"]["schema_version"] = changes_data["_meta"].get("schema_version", 1)
    changes_data["_meta"]["markets"] = MARKETS

    predictions = compute_predictions(changes_store, predictions_data.get("predictions", []))
    predictions_data = {
        "_meta": {"computed_at": now_iso(), "method": "deterministic rule-based — not AI-generated"},
        "predictions": predictions,
    }

    save_json(SNAPSHOTS_FILE, snapshots)
    save_json(CHANGES_FILE, changes_data)
    save_json(PREDICTIONS_FILE, predictions_data)

    log.info("Run complete: %d source(s) checked, %d new change(s), %d active prediction(s)",
              len(SOURCES), len(new_changes), len(predictions))


if __name__ == "__main__":
    main()
