"""
FlyScout — Card Network Rule Tracker
======================================
Runs on a schedule via GitHub Actions (see .github/workflows/check-updates.yml).
No server to host, no Python to run locally — GitHub executes this for you.

Every run:
  1. Fetches each source in SOURCES, compares against last snapshot
  2. On real change: classifies, extracts rates, logs to rule-changes.json,
     sends detailed Slack alert
  3. Derives live current rates from the change log → writes data/rates.json
  4. Generates full Flywire-specific predictions (corridor, impact, actions,
     urgency, confidence, ETA) from change history → writes data/predictions.json
  5. Sends Slack alert only when a prediction status changes (not every run)
  6. GitHub Actions commits all updated data/*.json back to the repo

Maintaining this: edit SOURCES to add/remove monitored pages.
"""
import os, re, json, hashlib, logging
from datetime import datetime, timezone, timedelta

import requests
from bs4 import BeautifulSoup

# ── CONFIG ──────────────────────────────────────────────────────────────
BASE_DIR       = os.path.dirname(os.path.abspath(__file__))
DATA_DIR       = os.path.join(BASE_DIR, "data")
CHANGES_FILE   = os.path.join(DATA_DIR, "rule-changes.json")
PREDICTIONS_FILE = os.path.join(DATA_DIR, "predictions.json")
RATES_FILE     = os.path.join(DATA_DIR, "rates.json")
SNAPSHOTS_FILE = os.path.join(DATA_DIR, "snapshots.json")

SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
GEMINI_API_KEY    = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL      = "gemini-2.5-flash"
REQUEST_TIMEOUT   = 20
USER_AGENT = "FlyScoutBot/1.0 (+card-network-rule-tracker; Flywire Cards Network team)"
MARKETS = ["US", "UK", "EU", "AU", "SG", "CA", "JP"]
SOURCE_FAILURE_ALERT_THRESHOLD = 5  # alert after this many consecutive fetch failures

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("flyscout")


# ── SOURCE REGISTRY ─────────────────────────────────────────────────────
SOURCES = [
    # US
    # ── UNITED STATES ──────────────────────────────────────────────────
    # Visa US merchant fees hub — contains download link for the annual
    # interchange PDF and surcharge rule text. Confirmed accessible Jun 2026.
    {"id":"us_visa_ic",  "market":"US","network":"Visa",       "category":"interchange","cnp":False,
     "name":"Visa USA Interchange — Merchant Fees Hub",
     "url":"https://usa.visa.com/support/small-business/regulations-fees.html"},
    # Mastercard.com blocks GitHub Actions IPs with 403. Using Merchant Cost
    # Consulting instead — the industry source used for our seed data entries,
    # publicly accessible, and promptly updated when Mastercard publishes new
    # rate schedules. Documents both rate history and effective dates.
    {"id":"us_mc_ic",    "market":"US","network":"Mastercard", "category":"interchange","cnp":False,
     "name":"Mastercard US Interchange Rates — Merchant Cost Consulting",
     "url":"https://merchantcostconsulting.com/lower-credit-card-processing-fees/mastercard-interchange-rates/"},
    # Federal Reserve Regulation II hub — confirmed accessible, tracks the
    # debit cap and routing rules. Last updated May 2024, will change when
    # new regulatory actions occur (Eighth Circuit ruling, new rulemaking).
    {"id":"us_fed_reg2", "market":"US","network":"Regulator",  "category":"interchange","cnp":False,
     "name":"Federal Reserve — Regulation II Debit Interchange",
     "url":"https://www.federalreserve.gov/paymentsystems/regii-about.htm"},

    # ── UNITED KINGDOM ─────────────────────────────────────────────────
    # PSR card scheme and processing fees market review — confirmed working,
    # "Last updated: May 2026", actively updated with new consultation documents.
    {"id":"uk_psr_scheme","market":"UK","network":"Regulator", "category":"regulatory", "cnp":True,
     "name":"UK PSR — Card Scheme and Processing Fees Market Review",
     "url":"https://www.psr.org.uk/our-work/market-reviews/market-review-into-card-scheme-and-processing-fees/"},
    # PSR UK-EEA cross-border interchange market review — fixed URL (404 was
    # due to wrong path; correct path omits "consumer" and "fees").
    {"id":"uk_psr_xborder","market":"UK","network":"Regulator","category":"interchange","cnp":True,
     "name":"UK PSR — UK-EEA Cross-Border Interchange Market Review",
     "url":"https://www.psr.org.uk/our-work/market-reviews/market-review-into-cross-border-interchange-fees/"},

    # ── EUROPEAN UNION ─────────────────────────────────────────────────
    # EBA payment services page — confirmed working. Monitors for new
    # IFR-related opinions, technical standards, and guidelines.
    {"id":"eu_eba",      "market":"EU","network":"Regulator",  "category":"regulatory", "cnp":False,
     "name":"EBA — Payment Services & IFR Regulation",
     "url":"https://www.eba.europa.eu/regulation-and-policy/payment-services-and-electronic-money"},

    # ── AUSTRALIA ──────────────────────────────────────────────────────
    # RBA retail payments regulation review hub — broader landing page for
    # all RBA payment regulation work; fixed from the 404 /payments-system/
    # card-payments-regulation/ path which no longer exists.
    {"id":"au_rba",      "market":"AU","network":"Regulator",  "category":"regulatory", "cnp":True,
     "name":"RBA — Review of Retail Payments Regulation (Hub)",
     "url":"https://www.rba.gov.au/payments-and-infrastructure/review-of-retail-payments-regulation/"},
    # RBA 2026 conclusions page — confirmed working, richest content source
    # in the entire set. Tracks the Oct 2026 surcharge ban and interchange
    # cap changes. Will update when RBA publishes implementation details.
    {"id":"au_rba_2026", "market":"AU","network":"Regulator",  "category":"regulatory", "cnp":True,
     "name":"RBA — 2026 Review: Merchant Card Payment Costs Conclusions",
     "url":"https://www.rba.gov.au/payments-and-infrastructure/review-of-retail-payments-regulation/2026-03/"},

    # ── SINGAPORE ──────────────────────────────────────────────────────
    # MAS Parliamentary Replies — more stable than the main regulation page
    # which has been returning maintenance pages since monitoring began.
    # Contains genuine payment regulation content including interchange fee
    # questions and surcharging policy responses from MAS to Parliament.
    {"id":"sg_mas",      "market":"SG","network":"Regulator",  "category":"regulatory", "cnp":True,
     "name":"MAS — Parliamentary Replies (Payment Services)",
     "url":"https://www.mas.gov.sg/news/parliamentary-replies"},
    # MAS media releases — second SG source for broader coverage of
    # payment system regulatory announcements.
    {"id":"sg_mas_news", "market":"SG","network":"Regulator",  "category":"regulatory", "cnp":True,
     "name":"MAS — Media Releases",
     "url":"https://www.mas.gov.sg/news/media-releases"},

    # ── CANADA ─────────────────────────────────────────────────────────
    # FCAC Code of Conduct for the Payment Card Industry — active page,
    # confirmed accessible. Fixed from the 404 /programs/payment-cards.html
    # path. This page will update when the Code is revised.
    {"id":"ca_fcac",     "market":"CA","network":"Regulator",  "category":"regulatory", "cnp":True,
     "name":"FCAC — Code of Conduct for the Payment Card Industry",
     "url":"https://www.canada.ca/en/financial-consumer-agency/services/industry/laws-regulations/credit-debit-code-conduct.html"},
    # Government of Canada interchange news — tracks official Government
    # announcements on Visa/Mastercard fee agreements and reforms.
    # Using the FCAC annual reports page which lists all recent activity.
    {"id":"ca_govt_ic",  "market":"CA","network":"Both",       "category":"interchange","cnp":False,
     "name":"FCAC — Annual Report & Payment Card News",
     "url":"https://www.canada.ca/en/financial-consumer-agency/corporate/planning/annual-reports.html"},

    # ── JAPAN ──────────────────────────────────────────────────────────
    # FSA recent releases — the /policy/payserv/index.html path returned
    # 404; using the FSA's main recent.html page which lists all regulatory
    # developments including payment services updates.
    {"id":"jp_fsa",      "market":"JP","network":"Regulator",  "category":"regulatory", "cnp":False,
     "name":"Japan FSA — Recent Releases (incl. Payment Services)",
     "url":"https://www.fsa.go.jp/en/recent.html"},
    # METI cashless payments policy — tracks Japan's cashless push which
    # directly drives interchange and acceptance rule changes.
    {"id":"jp_meti",     "market":"JP","network":"Regulator",  "category":"regulatory", "cnp":False,
     "name":"METI — Cashless Payment Policy",
     "url":"https://www.meti.go.jp/english/policy/mono_info_service/cashless/index.html"},

]


# ── FLYWIRE MARKET INTELLIGENCE ─────────────────────────────────────────
# Encodes Flywire's business context per market+category so the prediction
# engine can generate specific, actionable output — not generic text.
# Urgency is computed dynamically from trigger type and recency;
# these templates provide the corridor/impact/action scaffolding.

FLYWIRE_INTEL = {
    ("AU", "surcharge"): {
        "corridor": "International students → AU universities (Visa/MC CNP)",
        "impact": (
            "Flywire cannot pass card acceptance costs to payers at AU institutions "
            "via surcharge from 1 Oct 2026 (RBA final decision). Domestic interchange "
            "caps also cut from same date. Foreign-card interchange cap follows 1 Apr 2027 "
            "(cost saving for international student home-country cards)."
        ),
        "action": (
            "1. Audit all AU merchant agreements — identify where Flywire currently passes surcharges.\n"
            "2. Model AU card acceptance costs under new lower interchange caps.\n"
            "3. Decide: absorb into spread, increase base pricing, or negotiate acquirer rate reduction.\n"
            "4. Notify AU institutional clients of any pricing changes before 1 Oct 2026."
        ),
        "urgency_map": {
            "regulatory_order":      ("critical", 100),
            "government_consultation":("high",    65),
            "network_policy_update": ("medium",   50),
        },
    },
    ("AU", "interchange"): {
        "corridor": "International students → AU universities (Visa/MC CNP)",
        "impact": (
            "RBA domestic interchange caps directly affect Flywire's AU card acceptance costs. "
            "Debit CNP reduced May 2025 (0.28%→0.22%). Foreign-card cap from 1 Apr 2027 reduces "
            "costs for international students paying AU institutions with home-country Visa/MC."
        ),
        "action": (
            "1. Update AU pricing models with new interchange caps when confirmed.\n"
            "2. Monitor RBA publications for final cap levels on foreign cards.\n"
            "3. Flag for annual AU pricing review."
        ),
        "urgency_map": {
            "regulatory_order":      ("high",   90),
            "government_consultation":("medium", 50),
            "network_policy_update": ("medium",  60),
        },
    },
    ("UK", "interchange"): {
        "corridor": "EU-issued cards → UK university merchants (CNP)",
        "impact": (
            "Post-Brexit: EU-issued cards at UK merchants cost up to 1.50% credit / 1.20% debit CNP "
            "(was 0.30%/0.20% under EU IFR). Surcharging banned in UK (Consumer Rights Act 2018) "
            "so Flywire/institution absorbs the full cross-border cost. PSR market review may re-cap "
            "toward 0.30% — a 120bps saving on EU student flows to UK universities."
        ),
        "action": (
            "1. Quantify total EU-issued card CNP volume at UK merchants over last 12 months.\n"
            "2. Model the margin impact of a 0.30% re-cap scenario vs current 1.50%.\n"
            "3. Track PSR card-acquiring market review — submit response if consultation opens.\n"
            "4. Avoid locking UK institutional pricing contracts beyond Q2 2026 without a PSR review clause."
        ),
        "urgency_map": {
            "regulatory_order":      ("critical", 95),
            "government_consultation":("high",    55),
            "network_policy_update": ("high",     70),
        },
    },
    ("US", "interchange"): {
        "corridor": "US domestic payments — education, healthcare, travel (Visa/MC CNP)",
        "impact": (
            "US has the highest CNP credit interchange globally (2.30–2.40% Visa Signature/Infinite). "
            "Reg II debit cap vacated by court (Aug 2025, stayed pending 8th Circuit appeal) — "
            "if upheld, US debit costs could rise 2–4x from current 21¢+5bps. "
            "Antitrust settlement caps qualifying consumer credit at 125bps from Mar 2025."
        ),
        "action": (
            "1. Identify what percentage of US volume is domestic debit — model 2x/4x cost scenarios.\n"
            "2. Verify whether Flywire's US institutional clients qualify for the 125bps antitrust cap.\n"
            "3. Monitor Eighth Circuit case docket — ruling timing determines urgency of debit cost action.\n"
            "4. Ensure US acquirer contracts allow interchange pass-through adjustment."
        ),
        "urgency_map": {
            "regulatory_order":      ("high",   80),
            "government_consultation":("medium", 40),
            "network_policy_update": ("medium",  60),
        },
    },
    ("US", "surcharge"): {
        "corridor": "US cross-border CNP — education and healthcare payments",
        "impact": (
            "Visa/Mastercard antitrust settlement changed US surcharge rules: 3% cap if applied "
            "uniformly across all card brands, 1% cap if applied selectively to Visa/MC only. "
            "Surcharging on debit/prepaid cards remains prohibited. Cart-level disclosure required."
        ),
        "action": (
            "1. Review Flywire US surcharge model — confirm which cap applies based on current implementation.\n"
            "2. Verify cart-level disclosure compliance (required since Feb 2025).\n"
            "3. Confirm debit/prepaid cards are excluded from any surcharge."
        ),
        "urgency_map": {
            "regulatory_order":      ("high",   85),
            "government_consultation":("medium", 40),
            "network_policy_update": ("medium",  55),
        },
    },
    ("EU", "interchange"): {
        "corridor": "EU domestic and cross-border CNP — education and healthcare",
        "impact": (
            "EU IFR caps consumer credit CNP at 0.30% (cheapest market globally for Flywire). "
            "Non-EEA cards (e.g. US/AU student paying EU university) cost 1.50% credit CNP — "
            "capped until Nov 2029 by voluntary Visa/MC commitment (confirmed Jul 2024)."
        ),
        "action": (
            "1. Low urgency — EU caps stable and extended to 2029.\n"
            "2. Ensure EU acquirer contracts correctly apply IFR rates to consumer cards.\n"
            "3. Flag commercial/corporate card volumes — these are NOT covered by IFR caps.\n"
            "4. Monitor EU Parliament for proposed IFR debit cap reduction (0.20%→0.15%)."
        ),
        "urgency_map": {
            "regulatory_order":      ("medium", 70),
            "government_consultation":("low",   30),
            "network_policy_update": ("medium", 50),
        },
    },
    ("CA", "surcharge"): {
        "corridor": "CA domestic CNP — education payments (credit cards only; Quebec exempt)",
        "impact": (
            "Credit card surcharging legal since Oct 2022 at 2.4% cap. Quebec institutions "
            "cannot surcharge (Consumer Protection Act). Debit/prepaid excluded."
        ),
        "action": (
            "1. Verify Quebec institutions are excluded from Flywire's CA surcharge model.\n"
            "2. Confirm debit/prepaid cards are not surcharged in Canada.\n"
            "3. Ensure 30-day acquirer advance notice requirement is met before any surcharge changes."
        ),
        "urgency_map": {
            "regulatory_order":      ("medium", 70),
            "government_consultation":("low",   30),
            "network_policy_update": ("medium", 55),
        },
    },
    ("CA", "interchange"): {
        "corridor": "CA domestic CNP — education payments",
        "impact": (
            "SMB interchange relief targets ~0.95% average effective credit interchange for "
            "eligible merchants. Visa SMB credit 1.35%, debit 0.50% (from Mar 2025). "
            "Flywire's CA institutional clients may qualify for SMB rates."
        ),
        "action": (
            "1. Confirm which CA institutions qualify for Visa/MC SMB interchange rates.\n"
            "2. Update CA pricing models to reflect Mar 2025 rate reduction (1.40%→1.35% credit).\n"
            "3. Flag for annual CA pricing review."
        ),
        "urgency_map": {
            "regulatory_order":      ("medium", 65),
            "government_consultation":("low",   30),
            "network_policy_update": ("medium", 55),
        },
    },
    ("SG", "interchange"): {
        "corridor": "SG domestic and cross-border CNP — education and healthcare",
        "impact": (
            "No government interchange caps in Singapore — rates set by Visa/Mastercard alone. "
            "MAS parliamentary scrutiny increasing (Mar 2026). "
            "Surcharging allowed at cost of acceptance; blanket brand surcharges banned Sep 2024."
        ),
        "action": (
            "1. Monitor MAS consultation publications — no imminent rule change confirmed.\n"
            "2. Ensure Flywire's SG surcharge model is cost-of-acceptance only (no blanket brand surcharges).\n"
            "3. Pre-transaction disclosure required for any surcharge."
        ),
        "urgency_map": {
            "regulatory_order":      ("high",  75),
            "government_consultation":("low",  20),
            "network_policy_update": ("medium", 50),
        },
    },
    ("JP", "interchange"): {
        "corridor": "JP domestic CNP — education payments",
        "impact": (
            "Japan debit restructured Nov 2024: contactless NFC 0.90%, new QR tier 0.75%. "
            "JFTC/METI interchange disclosure requirement in force since Sep 2022. "
            "Cashless ratio reached 42.8% in 2024 (exceeded 40% target)."
        ),
        "action": (
            "1. Update JP pricing models to reflect Nov 2024 debit restructuring.\n"
            "2. Monitor METI cashless policy for any further rate mandates.\n"
            "3. Low urgency — no further Visa/MC JP rule changes confirmed since Jan 2024."
        ),
        "urgency_map": {
            "regulatory_order":      ("medium", 60),
            "government_consultation":("low",   25),
            "network_policy_update": ("medium", 50),
        },
    },
}

# Default intel for market+category combinations not explicitly mapped
DEFAULT_INTEL = {
    "corridor": "Cross-border CNP payments across Flywire's market portfolio",
    "impact":   "Regulatory activity detected — review for impact on Flywire's card acceptance costs and rules.",
    "action":   "1. Review the tracked change for pricing/compliance implications.\n2. Escalate to Cards Network team if material rate or rule change confirmed.",
    "urgency_map": {
        "regulatory_order":       ("high",   75),
        "government_consultation": ("medium", 40),
        "network_policy_update":   ("medium", 55),
        "internal_capture":        ("low",    20),
    },
}


# ── KEYWORD CLASSIFICATION ───────────────────────────────────────────────
CATEGORY_KEYWORDS = {
    "interchange": ["interchange", "reimbursement fee", "merchant discount rate", "ifr cap"],
    "surcharge":   ["surcharge", "surcharging", "cost of acceptance", "checkout fee"],
}
CNP_KEYWORDS   = ["card-not-present", "card not present", "cnp", "e-commerce", "ecommerce", "online"]
TRIGGER_KEYWORDS = {
    "regulatory_order":       ["regulation", "mandate", "shall not exceed", "statutory", "compliance deadline"],
    "government_consultation":["consultation", "review", "feedback", "submission", "discussion paper", "proposed"],
    "network_policy_update":  ["effective", "bulletin", "rule change", "scheme update", "network announces"],
    "scheme_review":          ["periodic review", "scheduled review"],
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
    return any(k in text.lower() for k in CNP_KEYWORDS)


def classify_trigger(text):
    tl = text.lower()
    scores = {t: sum(tl.count(k) for k in kws) for t, kws in TRIGGER_KEYWORDS.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "internal_capture"


def extract_rates(text):
    return RATE_PATTERN.findall(text or "")


def diff_rates(old_text, new_text):
    old_rates = extract_rates(old_text)
    new_rates = extract_rates(new_text)
    if old_rates and new_rates and old_rates != new_rates:
        return (", ".join(r + "%" for r in old_rates[:4]),
                ", ".join(r + "%" for r in new_rates[:4]))
    return None, None


# ── FETCH & EXTRACT ──────────────────────────────────────────────────────
def fetch(url):
    r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r


def extract_text(resp):
    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]):
        tag.decompose()
    return re.sub(r"\s+", " ", soup.get_text(separator=" ", strip=True)).strip()


def content_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


# ── STORAGE HELPERS ──────────────────────────────────────────────────────
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


# ── SLACK ALERTS ─────────────────────────────────────────────────────────
def slack_post(payload):
    if not SLACK_WEBHOOK_URL:
        log.info("No SLACK_WEBHOOK_URL — skipping: %s", payload.get("text", "")[:80])
        return
    try:
        r = requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=10)
        if r.status_code >= 300:
            log.warning("Slack %s: %s", r.status_code, r.text[:200])
    except requests.exceptions.RequestException as e:
        log.warning("Slack post failed: %s", e)


def send_change_alert(change):
    cnp_tag   = " · CNP-focused" if change["cnp"] else ""
    old_val   = change["old_value"] or "—"
    new_val   = change["new_value"] or "—"
    trigger_l = change["trigger"].replace("_", " ").title()
    blocks = [
        {"type": "header", "text": {"type": "plain_text",
            "text": f"📋 FlyScout: {change['market']} {change['network']} {change['category']} change detected"}},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*Market:*\n{change['market']}"},
            {"type": "mrkdwn", "text": f"*Network:*\n{change['network']}"},
            {"type": "mrkdwn", "text": f"*Category:*\n{change['category'].title()}{cnp_tag}"},
            {"type": "mrkdwn", "text": f"*Trigger:*\n{trigger_l}"},
        ]},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*What changed:*\n{change['summary']}"}},
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
         "url": change["source_url"]}]})
    blocks.append({"type": "context", "elements": [
        {"type": "mrkdwn", "text": f"Detected {change['detected_at']} · {change['source_name']} · FlyScout"}]})
    slack_post({"text": f"📋 FlyScout: {change['market']} {change['network']} change detected", "blocks": blocks})


def send_prediction_alert(pred):
    urgency_emoji = {"critical": "🔴", "high": "🟡", "medium": "🔵", "low": "⚪"}.get(pred.get("urgency","low"), "⚪")
    blocks = [
        {"type": "header", "text": {"type": "plain_text",
            "text": f"{urgency_emoji} FlyScout Prediction: {pred['market']} {pred['network']} {pred['category']}"}},
        {"type": "section", "text": {"type": "mrkdwn",
            "text": f"*{pred['title']}*\n\n*Corridor:* {pred.get('corridor','—')}\n\n*Flywire impact:* {pred.get('impact','—')[:300]}"}},
        {"type": "section", "text": {"type": "mrkdwn",
            "text": f"*ETA:* {pred.get('eta','Unknown')} · *Confidence:* {pred.get('confidence',0)}% · *Status:* {pred.get('status','—')}"}},
        {"type": "section", "text": {"type": "mrkdwn",
            "text": f"*Recommended actions:*\n{pred.get('action','—')[:400]}"}},
        {"type": "context", "elements": [
            {"type": "mrkdwn", "text": f"Evidence: {pred.get('evidence','—')} · Computed {pred.get('computed_at','—')[:10]} · FlyScout prediction engine"}]},
    ]
    slack_post({"text": f"{urgency_emoji} FlyScout Prediction: {pred['title']}", "blocks": blocks})


# ── CHANGE DETECTION ─────────────────────────────────────────────────────
# Patterns that indicate a transient/maintenance page rather than real content.
# If the fetched page matches any of these, skip hash comparison entirely —
# do not update the snapshot, do not fire an alert.
MAINTENANCE_PATTERNS = [
    r"sorry,?\s+this\s+service\s+is\s+currently\s+unavailable",
    r"this\s+page\s+is\s+(temporarily\s+)?unavailable",
    r"maintenance\s+mode",
    r"we.ll\s+be\s+back\s+soon",
    r"service\s+is\s+down\s+for\s+maintenance",
    r"temporarily\s+offline",
    r"site\s+is\s+undergoing\s+maintenance",
    r"please\s+try\s+again\s+later",
    r"503\s+service\s+unavailable",
]
MAINTENANCE_RE = re.compile("|".join(MAINTENANCE_PATTERNS), re.IGNORECASE)

# Minimum useful content length. Pages shorter than this are almost certainly
# error pages, redirects to login walls, or CDN blocks — not real content.
MIN_CONTENT_LENGTH = 500

def is_maintenance_page(text):
    """Return True if the page looks like a maintenance/error page."""
    if len(text) < MIN_CONTENT_LENGTH:
        return True
    return bool(MAINTENANCE_RE.search(text[:2000]))


def generate_impact_analysis(change, intel):
    """
    Calls Gemini to generate a structured impact analysis for a detected change.
    Stored permanently in the change entry — generated once, read many times.
    Returns None if GEMINI_API_KEY is not set or the call fails.
    """
    if not GEMINI_API_KEY:
        log.info("No GEMINI_API_KEY — skipping impact analysis generation")
        return None

    prompt = f"""You are a card network specialist analysing a rule change for Flywire's Cards Network team.
Flywire processes high-value cross-border card-not-present payments for education, healthcare, and travel.

Detected change:
- Market: {change['market']}
- Network: {change['network']}
- Category: {change['category']} {'(CNP)' if change.get('cnp') else ''}
- Title: {change['title']}
- Summary: {change['summary']}
- Before: {change.get('old_value') or 'not specified'}
- After: {change.get('new_value') or 'not specified'}
- Trigger: {change.get('trigger', 'unknown')}
- Source snippet: {change.get('new_snippet') or 'not available'}
- Flywire corridor: {intel.get('corridor', 'Cross-border CNP payments')}

Respond ONLY with a JSON object (no markdown, no preamble) containing:
{{
  "what_changed": "one precise sentence — what specifically changed and by how much",
  "why_it_matters": "one sentence — regulatory or market significance",
  "acquirer_impact": "one sentence — direct impact on acquiring banks",
  "cross_border_impact": "one sentence — impact on cross-border CNP payment companies like Flywire",
  "recommended_actions": ["action 1", "action 2", "action 3"]
}}

Be specific. Use numbers where available. Do not invent figures not in the data above."""

    try:
        resp = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}",
            json={
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": 600, "temperature": 0.1},
            },
            timeout=30,
        )
        if not resp.ok:
            log.warning("Gemini impact analysis failed: %s %s", resp.status_code, resp.text[:200])
            return None
        data = resp.json()
        text = (data.get("candidates", [{}])[0]
                .get("content", {}).get("parts", [{}])[0].get("text", ""))
        text = text.strip().strip("`").strip()
        if text.startswith("json"):
            text = text[4:].strip()
        result = json.loads(text)
        result["generated_at"] = now_iso()
        result["model"] = GEMINI_MODEL
        log.info("Impact analysis generated for %s", change["id"])
        return result
    except Exception as e:
        log.warning("Impact analysis generation failed: %s", e)
        return None


def check_source(source, snapshots, changes_store):
    sid = source["id"]
    snap = snapshots.get(sid, {})

    try:
        resp = fetch(source["url"])
        text = extract_text(resp)
    except requests.exceptions.RequestException as e:
        log.warning("[%s] fetch failed: %s", sid, e)
        # Item 15 — track consecutive failures for source health monitoring
        snap["consecutive_failures"] = snap.get("consecutive_failures", 0) + 1
        snap["last_failure"] = now_iso()
        snap["last_failure_reason"] = str(e)[:200]
        snapshots[sid] = snap
        if snap["consecutive_failures"] == SOURCE_FAILURE_ALERT_THRESHOLD:
            log.warning("[%s] SOURCE HEALTH ALERT — %d consecutive failures",
                        sid, snap["consecutive_failures"])
        return None

    # Skip maintenance/error pages — do not update snapshot, do not alert.
    if is_maintenance_page(text):
        log.warning("[%s] maintenance/error page detected (%d chars) — skipping, snapshot unchanged",
                    sid, len(text))
        snap["consecutive_failures"] = snap.get("consecutive_failures", 0) + 1
        snap["last_failure"] = now_iso()
        snap["last_failure_reason"] = f"maintenance page ({len(text)} chars)"
        snapshots[sid] = snap
        return None

    # Successful fetch — reset failure counter
    snap["consecutive_failures"] = 0
    h = content_hash(text)
    prev_hash = snap.get("hash")
    prev_text = snap.get("text", "")
    snap.update({"hash": h, "text": text[:8000], "checked_at": now_iso()})
    snapshots[sid] = snap

    if prev_hash is None:
        log.info("[%s] baseline established (%d chars)", sid, len(text))
        return None
    if prev_hash == h:
        log.info("[%s] no change", sid)
        return None

    # Rate-pattern guard: suppress if only layout/nav changed, not actual rates
    old_rates = set(extract_rates(prev_text))
    new_rates = set(extract_rates(text))
    if old_rates and new_rates and old_rates == new_rates:
        log.info("[%s] hash changed but rate patterns identical %s — layout noise, skipping", sid, old_rates)
        return None

    category = classify_category(text, source["category"] if source["category"] != "regulatory" else None)
    cnp      = classify_cnp(text, source["cnp"])
    trigger  = classify_trigger(text)
    old_val, new_val = diff_rates(prev_text, text)
    snippet_match = RATE_PATTERN.search(text)
    new_snippet = text[max(0, snippet_match.start()-120):snippet_match.start()+160] if snippet_match else text[:280]

    change = {
        "id": f"chg_{sid}_{int(datetime.now().timestamp())}",
        "market": source["market"], "network": source["network"],
        "category": category, "cnp": cnp,
        "title": f"{source['name']} updated",
        "summary": (f"Content change detected on {source['name']}. "
                    f"{'Rate values changed.' if old_val else 'Review source — no specific rate pattern extracted.'}"),
        "old_value": old_val, "new_value": new_val,
        "trigger": trigger, "effective_date": None, "detected_at": now_iso(),
        "source_name": source["name"], "source_url": source["url"],
        "old_snippet": prev_text[:280],
        "new_snippet": new_snippet, "reviewed": False, "auto_detected": True,
        "impact_analysis": None,  # populated below if Gemini key is available
    }

    # Item 12 — generate impact analysis via Gemini and store permanently
    intel = FLYWIRE_INTEL.get((source["market"], category), DEFAULT_INTEL)
    impact = generate_impact_analysis(change, intel)
    if impact:
        change["impact_analysis"] = impact

    # Deduplication: suppress Slack alert if same market+category within 24h
    cutoff_24h = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    duplicate = any(
        c.get("market") == change["market"]
        and c.get("category") == change["category"]
        and c.get("detected_at", "") >= cutoff_24h
        and c.get("id") != change["id"]
        for c in changes_store
    )

    changes_store.append(change)
    log.info("[%s] CHANGE DETECTED — %s/%s/%s trigger=%s%s",
             sid, change["market"], change["network"], category, trigger,
             " (duplicate suppressed)" if duplicate else "")

    if not duplicate:
        send_change_alert(change)
    else:
        log.info("[%s] Slack alert suppressed — same market+category detected within 24h", sid)

    return change


# ── LIVE RATES ENGINE ─────────────────────────────────────────────────────
def compute_live_rates(all_changes):
    """
    Derives the latest known rate per market/network/category from the change
    log. The most recent entry with an extractable new_value percentage wins.
    Stored in data/rates.json — read by the frontend to enrich the KB.
    """
    latest = {}  # key: "market|network|category" → best change entry
    for c in all_changes:
        if not c.get("new_value"):
            continue
        if not RATE_PATTERN.search(c["new_value"]):
            continue
        key = f"{c['market']}|{c['network']}|{c['category']}"
        existing = latest.get(key)
        if not existing or c["detected_at"] > existing["detected_at"]:
            latest[key] = c

    rates = {}
    for key, c in latest.items():
        market, network, category = key.split("|")
        rates.setdefault(market, {}).setdefault(network, {})[category] = {
            "value":        c["new_value"],
            "previous":     c.get("old_value"),
            "detected_at":  c["detected_at"],
            "effective_date": c.get("effective_date"),
            "source_name":  c["source_name"],
            "source_url":   c["source_url"],
            "cnp":          c.get("cnp", False),
            "change_id":    c["id"],
        }

    return {
        "_meta": {
            "computed_at":  now_iso(),
            "description":  "Latest detected rates per market/network/category, "
                            "extracted automatically from rule-changes.json. "
                            "Only entries with extractable percentage values are included.",
            "entry_count":  len(latest),
        },
        "rates": rates,
    }


# ── FLYWIRE PREDICTION ENGINE ────────────────────────────────────────────
def compute_predictions(all_changes, prev_predictions):
    """
    Generates full Flywire-specific prediction objects from change history.
    Each prediction includes: corridor, business impact, recommended actions,
    urgency, confidence, ETA, and evidence — all derived from the change log
    plus the FLYWIRE_INTEL templates. Written to data/predictions.json.
    """
    prev_by_id = {p["id"]: p for p in prev_predictions}
    predictions = []

    # Group changes by market+category (network kept for context but not split)
    groups = {}
    for c in all_changes:
        key = f"{c['market']}|{c['category']}"
        groups.setdefault(key, []).append(c)

    for key, entries in groups.items():
        market, category = key.split("|")
        entries_sorted = sorted(entries, key=lambda c: c["detected_at"])
        latest = entries_sorted[-1]

        intel = FLYWIRE_INTEL.get((market, category), DEFAULT_INTEL)
        urgency_map = intel["urgency_map"]

        # Determine status and derive urgency/confidence from trigger type
        trigger      = latest.get("trigger", "internal_capture")
        urgency, confidence = urgency_map.get(trigger, ("low", 20))

        # Detect open consultation (consultation exists but no subsequent regulatory_order)
        has_consultation = any(e["trigger"] == "government_consultation" for e in entries_sorted)
        has_resolution   = any(e["trigger"] == "regulatory_order" for e in entries_sorted)
        consult_after_resolution = False
        if has_consultation and has_resolution:
            last_consult = max(e["detected_at"] for e in entries_sorted if e["trigger"] == "government_consultation")
            last_order   = max(e["detected_at"] for e in entries_sorted if e["trigger"] == "regulatory_order")
            consult_after_resolution = last_consult > last_order

        if trigger == "regulatory_order":
            status = "confirmed"
        elif trigger == "government_consultation" or (has_consultation and not has_resolution) or consult_after_resolution:
            status = "open_consultation"
        else:
            status = "monitoring"

        # ETA: use effective_date of most recent regulatory_order, else "TBC"
        orders = [e for e in entries_sorted if e["trigger"] == "regulatory_order" and e.get("effective_date")]
        if orders:
            eff = max(orders, key=lambda e: e["effective_date"])["effective_date"]
            try:
                dt_raw = datetime.fromisoformat(eff.replace("Z", "+00:00"))
                eta_date = dt_raw if dt_raw.tzinfo else dt_raw.replace(tzinfo=timezone.utc)
                days_until = (eta_date - datetime.now(timezone.utc)).days
                if days_until > 0:
                    eta = f"{eff[:10]} — {days_until} days away"
                elif days_until > -60:
                    eta = f"{eff[:10]} — recently effective"
                else:
                    eta = f"{eff[:10]} — in force"
            except ValueError:
                eta = eff[:10]
        elif status == "open_consultation":
            eta = "TBC — awaiting regulatory decision"
        else:
            eta = "TBC"

        # Build evidence string from most recent 2 entries
        evidence_entries = entries_sorted[-2:]
        evidence = " → ".join(
            f"{e['source_name']} ({e['detected_at'][:10]})" for e in evidence_entries
        )
        evidence_url = latest.get("source_url", "")

        # Network: use the most common network across entries
        networks = [e.get("network","Both") for e in entries_sorted]
        network = max(set(networks), key=networks.count)

        # Title: derive from most recent change
        if latest.get("old_value") and latest.get("new_value"):
            title = f"{market} {category}: {latest['old_value']} → {latest['new_value']}"
        else:
            title = latest.get("title", f"{market} {network} {category} regulatory activity")

        # Item 10 — staleness flag: if status is open_consultation and the
        # most recent evidence is more than 6 months old, flag it as
        # potentially stale so a reviewer knows to verify the source is
        # still the latest available information.
        is_stale = False
        if status == "open_consultation":
            try:
                last_dt_raw = datetime.fromisoformat(latest["detected_at"].replace("Z", "+00:00"))
                last_dt = last_dt_raw if last_dt_raw.tzinfo else last_dt_raw.replace(tzinfo=timezone.utc)
                is_stale = (datetime.now(timezone.utc) - last_dt).days > 180
            except (ValueError, KeyError):
                pass

        pred_id = f"fw_{market.lower()}_{category.lower()}_{trigger[:4]}"
        pred = {
            "id":           pred_id,
            "market":       market,
            "network":      network,
            "category":     category,
            "status":       status,
            "urgency":      urgency,
            "confidence":   confidence,
            "title":        title,
            "eta":          eta,
            "corridor":     intel["corridor"],
            "impact":       intel["impact"],
            "action":       intel["action"],
            "evidence":     evidence,
            "evidence_url": evidence_url,
            "evidence_ids": [e["id"] for e in evidence_entries],
            "computed_at":  now_iso(),
            "is_stale":     is_stale,
        }
        predictions.append(pred)

        # Slack alert only when urgency state changes (avoid spam)
        prev = prev_by_id.get(pred_id, {})
        if prev.get("urgency") != urgency or prev.get("status") != status:
            send_prediction_alert(pred)
            log.info("Prediction state change: %s → urgency=%s status=%s", pred_id, urgency, status)

    # Sort: critical → high → medium → low
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    predictions.sort(key=lambda p: order.get(p["urgency"], 9))
    return predictions


# ── MAIN ─────────────────────────────────────────────────────────────────
def main():
    snapshots        = load_json(SNAPSHOTS_FILE,   {})
    changes_data     = load_json(CHANGES_FILE,     {"_meta": {}, "changes": []})
    predictions_data = load_json(PREDICTIONS_FILE, {"_meta": {}, "predictions": []})

    changes_store = changes_data["changes"]
    new_changes   = []

    for source in SOURCES:
        result = check_source(source, snapshots, changes_store)
        if result:
            new_changes.append(result)

    changes_data["_meta"]["last_updated"]    = now_iso()
    changes_data["_meta"]["schema_version"]  = changes_data["_meta"].get("schema_version", 1)
    changes_data["_meta"]["markets"]         = MARKETS

    # Compute live rates from full change history
    rates_data = compute_live_rates(changes_store)

    # Compute Flywire-specific predictions
    predictions = compute_predictions(changes_store, predictions_data.get("predictions", []))
    predictions_data = {
        "_meta": {
            "computed_at": now_iso(),
            "method":      "Flywire-specific rule-based engine — corridor/impact/action from FLYWIRE_INTEL templates, urgency/confidence/ETA derived from change log trigger types and effective dates",
            "count":       len(predictions),
        },
        "predictions": predictions,
    }

    save_json(SNAPSHOTS_FILE,   snapshots)
    save_json(CHANGES_FILE,     changes_data)
    save_json(RATES_FILE,       rates_data)
    save_json(PREDICTIONS_FILE, predictions_data)

    # Item 15 — source health summary. Surface any source that has failed
    # SOURCE_FAILURE_ALERT_THRESHOLD or more times in a row, so a silent
    # GitHub Actions success doesn't hide a source that's actually broken.
    unhealthy = [
        (sid, snap.get("consecutive_failures", 0), snap.get("last_failure_reason", "unknown"))
        for sid, snap in snapshots.items()
        if snap.get("consecutive_failures", 0) >= SOURCE_FAILURE_ALERT_THRESHOLD
    ]
    if unhealthy:
        lines = "\n".join(f"  • {sid}: {fails} consecutive failures ({reason})" for sid, fails, reason in unhealthy)
        log.warning("SOURCE HEALTH WARNING — %d source(s) unhealthy:\n%s", len(unhealthy), lines)
        slack_post({
            "text": f"⚠️ FlyScout source health warning — {len(unhealthy)} source(s) failing repeatedly",
            "blocks": [
                {"type": "header", "text": {"type": "plain_text", "text": f"⚠️ {len(unhealthy)} source(s) need attention"}},
                {"type": "section", "text": {"type": "mrkdwn",
                    "text": "\n".join(f"• *{sid}*: {fails} consecutive failures — {reason}" for sid, fails, reason in unhealthy)}},
                {"type": "context", "elements": [{"type": "mrkdwn",
                    "text": "These sources may have a dead URL, a network block, or an extended outage. Check check_rules.py SOURCES list."}]}
            ]
        })

    log.info("Run complete — %d source(s) checked, %d new change(s), %d rate(s) tracked, %d prediction(s), %d unhealthy source(s)",
             len(SOURCES), len(new_changes), rates_data["_meta"]["entry_count"], len(predictions), len(unhealthy))


if __name__ == "__main__":
    main()
