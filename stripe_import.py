#!/usr/bin/env python3
"""
FlyScout - Stripe "Network cost updates" importer
==================================================
Turns the text of Stripe's "Network cost updates" docs page (use the page's
"Copy for LLM" or "View as Markdown" button) into structured data:

  <out>/stripe-updates.json      one entry per update, change-log shaped
  <out>/stripe-rate-lines.json   one line per individual rate / fee, with
                                 old vs new values and basis-point deltas

Usage:
    python stripe_import.py stripe_page.md
    python stripe_import.py stripe_page.md --out private_data --source-url URL

IMPORTANT: Stripe's page is behind a login. Keep the output OUT of any public
repository (default folder is private_data/ - add it to .gitignore) until
Flywire IT / Stripe confirm it may be shared.

Re-running is safe: entries have stable ids, first_seen_at is preserved, and
anything that disappears from a newer copy of the page is kept and marked
status="no_longer_listed" instead of being deleted.
"""
import argparse
import hashlib
import json
import os
import re
from datetime import datetime, timezone

IN_SCOPE = ["US", "UK", "EU", "AU", "SG", "CA", "JP"]
APAC_IN_SCOPE = ["AU", "SG", "JP"]

EU_COUNTRIES = [
    "Austria", "Belgium", "Bulgaria", "Croatia", "Cyprus", "Czech Republic",
    "Denmark", "Estonia", "Finland", "France", "Germany", "Greece", "Hungary",
    "Ireland", "Italy", "Latvia", "Lithuania", "Luxembourg", "Malta",
    "Netherlands", "Poland", "Portugal", "Romania", "Slovakia", "Slovenia",
    "Spain", "Sweden",
]

MONTHS = ["jan", "feb", "mar", "apr", "may", "jun",
          "jul", "aug", "sep", "oct", "nov", "dec"]
# Tolerant on purpose: Stripe's page has typos (e.g. "Novemeber").
DATE_RE = re.compile(r"\b([A-Z][a-z]{2,9})\s+(\d{1,2}),\s+(\d{4})\b")

STOPWORDS = set("""this that with from will have been their there which also
into than then they them when where about after before other more most some
such only each both fees fee update updates applies apply applied effective
starting start users user business businesses card cards transaction
transactions""".split())


# ── small helpers ───────────────────────────────────────────────────────────
def now_iso():
    return datetime.now(timezone.utc).isoformat()


def clean_text(s):
    s = (s or "").replace("\u00a0", " ").replace("&nbsp;", " ")
    s = re.sub(r"\*\*", "", s)
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)      # [text](url) -> text
    s = re.sub(r":\s*\n\s*-\s*", ": ", s)               # "title:" + bullet
    s = re.sub(r"\s*\n\s*-\s*", "; ", s)                 # bullets -> ;
    s = re.sub(r"\s+", " ", s)
    return s.strip(" ;")


def short_hash(*parts):
    return hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:10]


def parse_one_date(m):
    word = m.group(1).lower()
    if word[:3] not in MONTHS:
        return None
    try:
        return datetime(int(m.group(3)), MONTHS.index(word[:3]) + 1, int(m.group(2))).date().isoformat()
    except ValueError:
        return None


def parse_dates(cell):
    """All dates in a cell, with the region label for bullet lines.
    'TBD' becomes a date-less entry so it is not silently dropped."""
    out = []
    for line in (cell or "").split("\n"):
        label, body = None, line
        m = re.match(r"^\s*-\s*([^:]+):\s*(.*)$", line)
        if m:
            label, body = m.group(1).strip(), m.group(2)
        found = False
        for dm in DATE_RE.finditer(body):
            iso = parse_one_date(dm)
            if iso:
                out.append({"label": label, "date": iso})
                found = True
        if not found and "TBD" in body:
            out.append({"label": label, "date": None, "note": "TBD"})
    return out


# ── classification ──────────────────────────────────────────────────────────
def map_markets(text):
    """-> (direct in-scope markets, regional in-scope markets, region label).
    Direct = a market is named. Regional = only a region (APAC / Global)."""
    t = re.sub(r"\(excluding[^)]*\)", "", text or "")     # "Europe (excluding UK)"
    def has(w):
        return re.search(r"\b%s\b" % re.escape(w), t) is not None

    direct = []
    if "United States" in t:
        direct.append("US")
    if "United Kingdom" in t or has("UK"):
        direct.append("UK")
    if has("Europe") or any(has(c) for c in EU_COUNTRIES):
        direct.append("EU")
    for name, code in (("Australia", "AU"), ("Singapore", "SG"),
                       ("Canada", "CA"), ("Japan", "JP")):
        if has(name):
            direct.append(code)
    regional, label = [], None
    if has("Global"):
        regional, label = list(IN_SCOPE), "GLOBAL"
    elif has("Asia Pacific") or has("APAC"):
        regional, label = list(APAC_IN_SCOPE), "APAC"
    regional = [m for m in regional if m not in direct]
    return direct, regional, label


MARKET_ALIASES = {"US": ["United States", "US"], "UK": ["United Kingdom", "UK"],
                  "EU": ["Europe"], "AU": ["Australia", "Asia Pacific", "APAC"],
                  "SG": ["Singapore", "Asia Pacific", "APAC"], "CA": ["Canada"],
                  "JP": ["Japan", "Asia Pacific", "APAC"]}


def _word(a, text):
    return re.search(r"\b%s\b" % re.escape(a), text or "") is not None


def date_for_market(dates, market):
    """Earliest date that applies to one market. Bullet labels decide:
    'Global excluding Canada' skips Canada, 'Canada and Europe' covers CA+EU,
    an unlabeled date applies to everyone. None = TBD / not stated."""
    aliases = MARKET_ALIASES.get(market, [])
    applicable, tbd = [], False
    for d in dates:
        lab = d.get("label")
        if lab is None:
            continue
        base, _, excl = lab.partition(" excluding ")
        base = re.sub(r"\(excluding[^)]*\)", "", base)
        excluded = bool(excl) and any(_word(a, excl) for a in aliases)
        applies = (not excluded) and ("Global" in base or any(_word(a, base) for a in aliases))
        if applies:
            if d["date"]:
                applicable.append(d["date"])
            else:
                tbd = True
    if applicable:
        return min(applicable)
    if tbd:
        return None
    unlabeled = [d["date"] for d in dates if d.get("label") is None and d["date"]]
    return min(unlabeled) if unlabeled else None


def detect_networks(text):
    low = (text or "").lower()
    found = []
    for key, label in (("visa", "Visa"), ("mastercard", "Mastercard"),
                       ("discover", "Discover"), ("american express", "Amex"),
                       ("interac", "Interac"), ("unionpay", "UnionPay")):
        if key in low and label not in found:
            found.append(label)
    return found


def parse_network(cell):
    """'Visa and Mastercard: Interchange' -> (['Visa','Mastercard'], 'interchange')"""
    name, _, kind = (cell or "").partition(":")
    nets = []
    low = name.lower()
    for key, label in (("visa", "Visa"), ("mastercard", "Mastercard"),
                       ("discover", "Discover"), ("american express", "Amex"),
                       ("amex", "Amex"), ("interac", "Interac"),
                       ("union pay", "UnionPay"), ("unionpay", "UnionPay")):
        if key in low and label not in nets:
            nets.append(label)
    k = kind.strip().lower()
    if k.startswith("interchange"):
        ft = "interchange"
    elif k.startswith("scheme"):
        ft = "scheme"
    elif k:
        ft = "processing"            # "Processing update", "Response codes"
    else:
        ft = "product"               # new card products have no fee type
    return nets, ft


CATEGORY_FOR = {"interchange": "interchange", "scheme": "scheme_fee",
                "processing": "processing", "product": "card_product"}


def derive_cnp(text):
    t = (text or "").lower()
    if re.search(r"card[- ]not[- ]present|\bcnp\b|e-?commerce|electronic commerce|"
                 r"digital commerce|\bonline\b|\bdcap\b", t):
        return True
    if re.search(r"card[- ]present|\bchip\b|contactless|in-person|face-to-face", t):
        return False
    return None


def derive_card_type(text):
    t = (text or "").lower()
    kinds = [k for k, rx in (("credit", r"\bcredit\b|\bcharge\b"),
                             ("debit", r"\bdebit\b"),
                             ("prepaid", r"\bprepaid\b")) if re.search(rx, t)]
    if not kinds:
        return "unspecified"
    return kinds[0] if len(kinds) == 1 else "mixed"


def derive_segment(text):
    t = (text or "").lower()
    c, k = bool(re.search(r"\bconsumer\b", t)), bool(re.search(r"\bcommercial\b|\bbusiness\b|\bcorporate\b|\bb2b\b", t))
    if c and k:
        return "mixed"
    return "consumer" if c else "commercial" if k else "unspecified"


VERTICAL_RULES = [
    ("education", r"universit|tuition|college|\bmcc 8220\b|\b8220\b|education"),
    ("healthcare", r"healthcare|hospital|medical"),
    ("travel", r"travel|car rental|vehicle rental|hotel|lodging|airline|\b7512\b"),
    ("insurance", r"insurance|\b6300\b"),
    ("fuel_fleet", r"fleet|fuel|petroleum|\bgas\b"),
    ("restaurants", r"restaurant|fast food|quick serve|\bqsr\b"),
    ("government_charity", r"government|charit"),
]
FLYWIRE_VERTICALS = {"education", "healthcare", "travel"}


def derive_verticals(text):
    t = (text or "").lower()
    return [v for v, rx in VERTICAL_RULES if re.search(rx, t)]


def derive_trigger(text):
    if re.search(r"reserve bank|\brba\b|bank negara|regulator|central bank", text or "", re.I):
        return "regulatory_order"
    return "network_policy_update"


def old_new_values(text):
    m = re.search(r"from\s+([\d.,]+%?(?:\s?[A-Z]{3})?)\s+to\s+([\d.,]+%?(?:\s?[A-Z]{3})?)", text or "")
    return (m.group(1), m.group(2)) if m else (None, None)


def tokens(text):
    out = set()
    for w in re.findall(r"[a-z0-9]+", (text or "").lower()):
        if len(w) > 3 and w not in STOPWORDS:
            out.add(w[:-1] if w.endswith("s") else w)
    return out


# ── markdown table reading ──────────────────────────────────────────────────
def split_cells(row_text):
    t = row_text.strip()
    if t.startswith("|"):
        t = t[1:]
    if t.endswith("|"):
        t = t[:-1]
    return [c.strip() for c in t.split("|")]


def collect_table(lines, i):
    """Table starting at the header on line i. Rows may span several lines
    (Stripe puts newlines inside cells) - a row starts with '|' and ends with
    a line that ends in '|'."""
    header = split_cells(lines[i])
    j, rows, current = i + 2, [], None          # i+1 is the --- separator
    while j < len(lines):
        ln = lines[j]
        if ln.startswith("|"):
            if current is not None:
                rows.append(current)
            current = ln
        elif current is not None and not current.rstrip().endswith("|"):
            current += "\n" + ln
        else:
            break
        j += 1
    if current is not None:
        rows.append(current)
    return header, [split_cells(r) for r in rows], j


# ── section parsers ─────────────────────────────────────────────────────────
def parse_recent(lines):
    """'Recently added updates': the only place the page gives a date an
    update was *announced* - that is the true published_date."""
    items, start = [], None
    for i, ln in enumerate(lines):
        if ln.strip() == "## Recently added updates":
            start = i + 1
            break
    if start is None:
        return items
    pub = None
    for ln in lines[start:]:
        if ln.startswith("## "):
            break
        m = re.match(r"^###\s+Updates for (.+)$", ln.strip())
        if m:
            dm = DATE_RE.search(m.group(1))
            pub = parse_one_date(dm) if dm else None
            continue
        m = re.match(r"^\d+\.\s+(.*)$", ln.strip())
        if m and pub:
            items.append({"published_date": pub, "text": clean_text(m.group(1))})
    return items


def parse_summary_tables(lines, report):
    start = next((i for i, ln in enumerate(lines)
                  if ln.strip() == "## Notable network cost updates"), None)
    if start is None:
        return []
    events, i, section = [], start + 1, None
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("#### "):               # detailed tables begin
            break
        if ln.startswith("### "):
            section = ln[4:].strip()
        if ln.startswith("|") and "COUNTRY OR REGION" in ln.upper():
            header, rows, i = collect_table(lines, i)
            for cells in rows:
                if len(cells) != 4:
                    report["malformed_rows"] += 1
                    continue
                events.append(build_summary_event(cells, section, report))
            continue
        i += 1
    return [e for e in events if e]


def build_summary_event(cells, section, report):
    country, date_cell, network_cell, summary = cells
    direct, regional, label = map_markets(country)
    if not direct and not regional:
        report["skipped_out_of_scope"] += 1
        return None
    nets, ft = parse_network(network_cell)
    dates = parse_dates(date_cell)
    if not any(d["date"] for d in dates):
        report["rows_without_date"] += 1
    text = clean_text(summary)
    return {"kind": "table", "section": section, "country_raw": country,
            "direct": direct, "regional": regional, "region_label": label,
            "networks": nets, "fee_type": ft, "dates": dates,
            "summary": text, "network_raw": network_cell}


def parse_recent_as_event(item):
    direct, regional, label = map_markets(item["text"])
    nets = detect_networks(item["text"])
    low = item["text"].lower()
    ft = "interchange" if "interchange" in low else "scheme" if "fee" in low else "processing"
    dates = []
    for dm in DATE_RE.finditer(item["text"]):
        iso = parse_one_date(dm)
        if iso:
            dates.append({"label": None, "date": iso})
    return {"kind": "recent", "section": "Recently added updates",
            "country_raw": "", "direct": direct, "regional": regional,
            "region_label": label, "networks": nets, "fee_type": ft,
            "dates": dates, "summary": item["text"], "network_raw": "",
            "published_date": item["published_date"]}


def merge_recent(events, recents):
    """Attach the published date to a table event it describes; otherwise the
    recent item becomes its own event. Matching = same network, same market,
    and >=50% of the item's distinctive words appear in the table summary."""
    standalone = []
    for r in recents:
        cand = parse_recent_as_event(r)
        want = tokens(r["text"])
        best = None
        for e in events:
            if e["kind"] != "table":
                continue
            if cand["networks"] and e["networks"] and not set(cand["networks"]) & set(e["networks"]):
                continue
            if cand["direct"] and e["direct"] and not set(cand["direct"]) & set(e["direct"]):
                continue
            have = tokens(e["summary"])
            score = len(want & have) / max(len(want), 1)
            if score >= 0.5 and (best is None or score > best[0]):
                best = (score, e)
        if best:
            best[1].setdefault("published_date", r["published_date"])
            best[1]["recent_text"] = r["text"]
        else:
            standalone.append(cand)
    return events + standalone


# ── detailed rate tables ────────────────────────────────────────────────────
def parse_rate(raw):
    s = re.sub(r"\(no change\)", "", raw or "", flags=re.I).strip()
    out = {"pct": None, "fixed": None, "currency": None,
           "cap_or_min": bool(re.search(r"cap|minimum|maximum", s, re.I)),
           "kind": "value"}
    if not s or s.upper() == "N/A":
        out["kind"] = "n/a"
    elif s.lower().startswith("varies"):
        out["kind"] = "varies"
    pm = re.search(r"(\d+(?:\.\d+)?)\s*%", s)
    am = re.search(r"(\d+(?:\.\d+)?)\s*(USD|EUR|AUD|CAD|BRL|GBP|JPY)\b", s)
    if pm:
        out["pct"] = float(pm.group(1))
    if am:
        out["fixed"], out["currency"] = float(am.group(1)), am.group(2)
    return out


def parse_detail(lines, report, seen_at):
    start = next((i for i, ln in enumerate(lines)
                  if re.match(r"^####\s+(APAC|EMEA|LATAM|US\s*&\s*CA)\s*$", ln.strip())), None)
    if start is None:
        return []
    group = network = fee_type = eff = None
    out, i = [], start
    while i < len(lines):
        ln = lines[i].strip()
        m = re.match(r"^####\s+(.*)$", ln)
        if m:
            v = m.group(1).strip()
            if re.match(r"^(APAC|EMEA|LATAM|US\s*&\s*CA)$", v):
                group = v
            else:
                network = parse_network(v)[0]
        m = re.match(r"^###\s+(Interchange|Scheme)\s*$", ln)
        if m:
            fee_type = m.group(1).lower()
        m = re.match(r"^Effective:\s+(\d{4}-\d{2}-\d{2})", ln)
        if m:
            eff = m.group(1)
        if ln.startswith("| Country"):
            header, rows, i = collect_table(lines, i)
            hdr = [h.lower() for h in header]
            for cells in rows:
                if len(cells) != len(hdr):
                    report["malformed_rows"] += 1
                    continue
                row = dict(zip(hdr, cells))
                line = build_rate_line(row, group, network, fee_type, eff, seen_at, report)
                if line:
                    out.append(line)
            continue
        i += 1
    return out


def build_rate_line(row, group, network, fee_type, eff, seen_at, report):
    direct, regional, label = map_markets(row.get("country", ""))
    if not direct and not regional:
        report["rate_lines_out_of_scope"] += 1
        return None
    name = clean_text(row.get("interchange program") or row.get("reporting plan name") or "")
    new_raw = row.get("new interchange rate") or row.get("new rate") or ""
    old_raw = row.get("current interchange rate") or row.get("existing rate") or ""
    new, old = parse_rate(new_raw), parse_rate(old_raw)
    unchanged = bool(re.search(r"\(no change\)", new_raw, re.I))
    delta = None
    if unchanged:
        delta = 0.0
    elif (new["pct"] is not None and old["pct"] is not None
          and new["fixed"] is None and old["fixed"] is None):
        delta = round((new["pct"] - old["pct"]) * 100, 2)
    update_type = row.get("update type", "")
    ctx = " ".join([name, row.get("card category", ""), row.get("details", "")])
    return {
        "id": "rl_" + short_hash(group, network, fee_type, eff, row.get("country"), name,
                                  row.get("card category"), row.get("reporting plan name"), new_raw),
        "group": group, "network": (network or [None])[0] if isinstance(network, list) else network,
        "fee_type": fee_type, "category": CATEGORY_FOR.get(fee_type, fee_type),
        "country_raw": row.get("country", ""),
        "markets": direct, "markets_affected": direct + regional,
        "name": name, "card_category": row.get("card category", ""),
        "plan_code": row.get("reporting plan name", "") if "interchange program" in row else "",
        "update_type": update_type, "removed": "removed" in update_type.lower(),
        "new_raw": new_raw, "old_raw": old_raw,
        "new_pct": new["pct"], "old_pct": old["pct"],
        "new_fixed": new["fixed"], "old_fixed": old["fixed"],
        "currency": new["currency"] or old["currency"],
        "has_cap_or_min": new["cap_or_min"] or old["cap_or_min"],
        "delta_bps": delta, "unchanged": unchanged,
        "effective_date": eff,
        "cnp": derive_cnp(ctx), "card_type": derive_card_type(ctx),
        "segment": derive_segment(ctx), "verticals": derive_verticals(ctx),
        "qualifications": clean_text(row.get("qualifications", "")),
        "details": clean_text(row.get("details", "")),
        "source": "stripe_network_cost_updates", "first_seen_at": seen_at,
    }


# ── turn parsed events into FlyScout entries ────────────────────────────────
def build_entries(events, source_url, seen_at):
    entries = []
    for e in events:
        summary = e["summary"]
        nets = e["networks"]
        network = nets[0] if len(nets) == 1 else "Both" if {"Visa", "Mastercard"} <= set(nets) else "Multiple"
        ft = e["fee_type"]
        all_dates = [d["date"] for d in e["dates"] if d["date"]]
        event_date = min(all_dates) if all_dates else None       # id only
        published = e.get("published_date")
        old_v, new_v = old_new_values(summary)
        markets = e["direct"] or [e["region_label"]]
        affected = e["direct"] + e["regional"]
        by_market = {m: date_for_market(e["dates"], m) for m in affected}
        verticals = derive_verticals(summary)
        ctx = summary
        scope = ("market" if e["direct"] and not e["regional"] else
                 "market+regional" if e["direct"] else "regional")
        event_id = "se_" + short_hash(e["country_raw"], "|".join(nets), ft,
                                      e["network_raw"], event_date, summary[:140])
        title = re.split(r"(?<=[.:])\s", summary, maxsplit=1)[0]
        title = (title[:112].rstrip(" ,;:") + "...") if len(title) > 115 else title
        for mkt in markets:
            if mkt in by_market:
                effective = by_market[mkt]
            else:                                   # GLOBAL / APAC entry
                vals = [v for v in by_market.values() if v]
                effective = min(vals) if vals else None
            display, kind = (published, "published") if published else (effective, "effective")
            entries.append({
                "id": f"{event_id}_{mkt}", "event_id": event_id,
                "market": mkt, "markets_affected": affected, "scope_type": scope,
                "network": network, "networks": nets,
                "category": CATEGORY_FOR.get(ft, ft), "fee_type": ft,
                "cnp": derive_cnp(ctx), "card_type": derive_card_type(ctx),
                "segment": derive_segment(ctx), "verticals": verticals,
                "flywire_relevant": bool(set(verticals) & FLYWIRE_VERTICALS),
                "title": title,
                "summary": summary if len(summary) <= 700 else summary[:700].rstrip() + "...",
                "old_value": old_v, "new_value": new_v,
                "values_from_text": bool(old_v or new_v),
                "trigger": derive_trigger(summary),
                "effective_date": effective, "effective_dates": e["dates"],
                "effective_by_market": by_market,
                "published_date": published,
                "display_date": display, "display_date_kind": kind,
                "first_seen_at": seen_at, "last_seen_at": seen_at, "status": "active",
                "source_name": "Stripe - Network cost updates (login required)",
                "source_url": source_url, "source_type": "stripe_network_cost_updates",
                "section": e["section"], "reviewed": False, "auto_detected": True,
            })
    return entries


def upsert(path, new_items, seen_at, key="id"):
    """Keep first_seen_at; mark items missing from the new copy instead of
    deleting them; return merged list."""
    old = {}
    if os.path.exists(path):
        try:
            old = {x[key]: x for x in json.load(open(path, encoding="utf-8")).get("items", [])}
        except (json.JSONDecodeError, OSError):
            old = {}
    merged, seen = [], set()
    for it in new_items:
        seen.add(it[key])
        if it[key] in old:
            it["first_seen_at"] = old[it[key]].get("first_seen_at", seen_at)
        merged.append(it)
    for k, it in old.items():
        if k not in seen:
            it["status"] = "no_longer_listed"
            merged.append(it)
    return merged


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", help="text/markdown copied from the Stripe page")
    ap.add_argument("--out", default="private_data")
    ap.add_argument("--source-url", default="")
    args = ap.parse_args()

    seen_at = now_iso()
    text = open(args.input, encoding="utf-8").read().replace("\u00a0", " ").replace("&nbsp;", " ")
    lines = text.split("\n")
    report = {"malformed_rows": 0, "skipped_out_of_scope": 0, "rows_without_date": 0,
              "rate_lines_out_of_scope": 0}

    recents = parse_recent(lines)
    events = merge_recent(parse_summary_tables(lines, report), recents)
    entries = build_entries(events, args.source_url, seen_at)
    rate_lines = parse_detail(lines, report, seen_at)

    os.makedirs(args.out, exist_ok=True)
    p1, p2 = os.path.join(args.out, "stripe-updates.json"), os.path.join(args.out, "stripe-rate-lines.json")
    merged_entries = upsert(p1, entries, seen_at)
    merged_lines = upsert(p2, rate_lines, seen_at)
    meta = {"imported_at": seen_at, "report": report,
            "note": "Derived from a login-gated Stripe page. Do not publish."}
    json.dump({"_meta": dict(meta, count=len(merged_entries)), "items": merged_entries},
              open(p1, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
    json.dump({"_meta": dict(meta, count=len(merged_lines)), "items": merged_lines},
              open(p2, "w", encoding="utf-8"), indent=2, ensure_ascii=False)

    print(f"Updates:    {len(entries)} entries from {len(events)} page items "
          f"({len(recents)} 'recently added')")
    print(f"Rate lines: {len(rate_lines)}")
    print(f"Report:     {report}")
    print(f"Wrote:      {p1}\n            {p2}")


if __name__ == "__main__":
    main()
