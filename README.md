# FlyScout — Card Network Rule Tracker

Tracks Visa & Mastercard interchange and CNP surcharge rule changes across
US, UK, EU, Australia, Singapore, Canada, and Japan — automatically.

No server to run. No Python to install locally. GitHub does the work.

---

## How it works

```
index.html              ← what you see (dashboard, change log, predictions, chat)
data/rule-changes.json  ← the tracked data — written automatically, never by hand
data/predictions.json   ← computed predictions — also automatic
check_rules.py          ← the detection script
.github/workflows/      ← tells GitHub when to run check_rules.py
```

1. **GitHub Actions runs `check_rules.py` on a schedule** (3x/day, no server needed —
   GitHub runs it for free on their infrastructure).
2. The script checks each source in `SOURCES` (inside `check_rules.py`), compares
   content to the last known version, and on a real change: classifies it
   (market, network, interchange/surcharge, CNP, likely trigger), extracts rate
   numbers if present, and appends a detailed entry to `data/rule-changes.json`.
3. It also recomputes `data/predictions.json` — a rule-based (not AI) engine that
   flags markets where a change looks "due" based on historical cadence, or
   "pending" because of an open regulatory consultation.
4. Every detected change and every new prediction sends a detailed Slack alert
   (if you've configured a webhook — see below).
5. The workflow commits the updated `data/*.json` files back to the repo.
   GitHub Pages auto-redeploys, so the live site reflects new data within minutes.
6. `index.html` just reads those JSON files — no backend call, no CORS proxy.

---

## Deploy (3 steps)

1. Push this folder to a new GitHub repository.
2. Repo → **Settings → Pages → Source: main branch** → save.
3. Visit `https://your-username.github.io/your-repo` — the dashboard works
   immediately, using the bootstrap seed data in `data/rule-changes.json`.

## Enable automated detection + Slack alerts

1. Repo → **Settings → Secrets and variables → Actions → New repository secret**
   - Name: `SLACK_WEBHOOK_URL`
   - Value: your Slack Incoming Webhook URL (create one at
     [api.slack.com/messaging/webhooks](https://api.slack.com/messaging/webhooks))
2. Repo → **Actions tab** → enable workflows if prompted.
3. That's it. The schedule in `.github/workflows/check-updates.yml` runs at
   02:00, 10:00, and 18:00 UTC daily. You can also trigger a run manually:
   Actions tab → "FlyScout Rule Check" → "Run workflow".

## Enable the AI chat assistant (optional)

The chat works without this — it searches tracked data with keyword matching
and never invents numbers. Adding a key makes it more conversational and lets
it search the live web for questions not yet covered by tracked data.

1. Get a free key at [aistudio.google.com](https://aistudio.google.com)
2. Open the deployed site → **Settings** → paste the key
3. Stored only in your browser (localStorage) — never sent anywhere except
   directly to Google's API.

---

## Maintaining this

**To add or remove a monitored source:** edit the `SOURCES` list in
`check_rules.py`. Each entry is a small dict — market, network, category,
name, URL. That's the entire maintenance surface; no other file needs to
change.

**To verify the seed data:** `data/rule-changes.json` ships with 3 example
entries (marked `"auto_detected": false`) to demonstrate the schema before
the first automated scan runs. Verify them against current sources, or just
let them get superseded naturally once real detection starts — the dashboard
clearly labels them as "seed example" in the Change Log until then.

**Severity / trigger classification** is keyword-based and intentionally
simple and auditable — not an AI guess. If a source consistently misclassifies,
adjust the keyword lists (`CATEGORY_KEYWORDS`, `TRIGGER_KEYWORDS`,
`CNP_KEYWORDS`) near the top of `check_rules.py`.

---

## Design principles this was built around

- **No hallucinated numbers.** Rate changes are only recorded when the script
  finds an actual percentage pattern in the page text and it differs from the
  last known value. The AI chat is instructed never to state a number it can't
  attribute to a source.
- **Nothing is hand-maintained going forward.** Once the first scan runs, all
  new entries come from automated detection — not manual data entry.
- **Predictions are explainable.** Every prediction shows exactly which
  historical entries it's reasoning from — never a bare percentage with no
  evidence.
- **Slack alerts are detailed, not generic.** Every alert includes market,
  network, category, before/after values where extractable, likely trigger,
  and a direct source link.
- **The tool never looks broken.** If a source fails to fetch, that one check
  is skipped and logged — it doesn't stop the rest of the run or crash the page.
