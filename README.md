# CTM AI Assessment Dashboard

A one-page HTML dashboard that turns the AskAI outputs CTM writes back to your activities into something you can hand a customer in a QBR.

You point it at any CTM account, tell it which AskAI Custom Fields to read and how to interpret the customer-type tags, and it generates a CTM-branded report covering the last 30 days of assessments — numeric scores, ratings, top categorical outputs, and highest/lowest-scoring calls — with a built-in segment toggle (e.g. existing vs. new customer).

The dashboard is **not opinionated about what AskAI is being used for**. The role names below are dashboard slots that map to whatever your customer's AskAI Actions are writing — call scoring is one common shape, but the same slots work for sentiment analysis, lead qualification, sales discovery, compliance review, or anything else.

You don't need to touch any code to run it for a new account; everything lives in a single `config.json`.

## Background

This report assumes the target CTM account is using **AskAI** to assess its calls.

AskAI is the CTM feature (Marketing Pro, Sales Engage, Enterprise, Growth, and Connect plans) that lets you ask a natural-language question about a call's transcript and writes the concise AI-generated answer into a Custom Field of your choosing. AskAI runs through a workflow Trigger — typically *"When transcription is ready"* — and each AskAI Action on that trigger answers one question and populates one Custom Field. So an account doing a full agent assessment usually has several AskAI Actions side-by-side: one for the overall score, one for the wrap-up code, one for strengths, one for coaching opportunities, one for the score rationale, and so on. AskAI requires call Transcriptions to be enabled on the relevant Call Settings.

This dashboard simply reads those Custom Fields back off the activities and summarises them.

- AskAI documentation: <https://calltrackingmetrics.zendesk.com/hc/en-us/articles/13068462406413-AskAI>
- CTM API reference (Postman): <https://postman.calltrackingmetrics.com/>

Under the hood, the script fetches activities from the standard CTM API endpoint `/api/v1/accounts/<account_id>/calls/search.json`, using the account's API Access Key + Secret Token via Basic auth — exactly as documented in the Postman collection above.

## What the dashboard shows

- **Headline KPIs** — total assessments, average score, average rating, segment mix, calls vs. chats, top category, top concern.
- **Segment Comparison** — side-by-side metrics for each customer segment you've defined (e.g. EC vs. NC).
- **Score & rating distribution** — bucketed histograms of the score and star fields, when configured.
- **Time & volume** — daily volume, daily average score, hour-of-day, and direction (inbound / outbound / chat) mix.
- **Top categorical outputs** — top 15 values for each configured categorical field. Section labels come straight from your `fields` config, so for one account you might see "Wrap-Up Code · Agent Strengths · Coaching Opportunities" and for another "Topics Discussed · Buying Signals · Objections".
- **Agent Leaderboard** — top 30 agents ranked by assessment volume, with their average score and rating.
- **Lowest- and highest-scoring assessments** — direct links back to each call in CTM, with the configured `concerns` / `highlights` field shown inline next to the score for quick context.
- **All / `<segment>` / Other toggle** — every chart and table updates live when you switch the lens.

If a field role isn't configured, the related KPI cards, charts, sections, and columns are omitted automatically — the dashboard adapts to what's in `config.json`.

## Running it (no coding required)

You'll need three things from the account you want to report on:

1. The CTM **Account ID** (visible in the URL when you're logged into CTM).
2. The account's **API Access Key + Secret Token** (Settings → Account Settings → API Integration).
3. The **Custom Field names** the account's AskAI Actions write to (visible under AI Tools → AI Insights → AskAI, or by opening any recently assessed call and looking at its Custom Fields section).

### One-time setup

```bash
cd ~/scripts/ctm-ai-assessment-dashboard
cp config.example.json config.json
```

Create an `env.txt` next to the script and add a line for the account in this format:

```text
<short_name>:<base64_of_access_key:secret_token>
```

For example:

```text
acme:YWJjMTIzZGVmNDU2OmVmZ2hpams=
```

The `<short_name>` is whatever you want to call this account (it just has to match the `auth_env_key` in `config.json`). The value after the colon is the same string CTM shows you under API Integration; if CTM hands it to you as `key:secret`, base64-encode it first (`echo -n "key:secret" | base64`).

`env.txt` is gitignored, so credentials stay local. The script looks for it in this order:

1. The path in the `CTM_ENV_FILE` environment variable, if set
2. `env.txt` next to the script
3. `~/.ctm/env.txt`
4. Or pass `--env-file /some/other/path` on the CLI

### Fill out `config.json`

The `fields` block maps the dashboard's **six built-in roles** to the actual AskAI Custom Field names on the account. **The field names below are illustrative** — every account names its AskAI fields whatever it wants. Open any recently AI-assessed call in CTM to see the real names for the account you're reporting on.

```jsonc
{
  "customer_name": "Acme Corp",
  "auth_env_key":  "acme",
  "account_id":    12345,
  "days_back":     30,
  "workers":       6,
  "output_file":   "outputs/acme_ai_assessment_report.html",

  "fields": {
    // Each entry: "role": {"key": "<actual CTM custom_field name>", "label": "<how it should be titled in the dashboard>"}
    "score":      {"key": "call_score",         "label": "Call Score"},
    "star":       {"key": "agent_star_rating",  "label": "Star Rating"},
    "categories": {"key": "wrap_up_code",       "label": "Wrap-Up Code"},
    "highlights": {"key": "agent_strengths",    "label": "Agent Strengths"},
    "concerns":   {"key": "coaching_opps",      "label": "Coaching Opportunities"},
    "notes":      {"key": "ai_summary",         "label": "AI Summary"}
  },

  "customer_segments": [
    {"id": "existing", "label": "Existing Customer", "short": "EC", "tags": ["ec"]},
    {"id": "new",      "label": "New Customer",      "short": "NC", "tags": ["nc"]}
  ]
}
```

### Generate the report

```bash
python3 ai_assessment_report.py
open outputs/acme_ai_assessment_report.html
```

That's the whole flow. The script will pull the activities, print progress per day, and open the HTML in your browser.

## Understanding the config

### `fields`

The dashboard has six **roles** — generic slots that describe a *shape* of data and *where it appears*. You map each role to whatever Custom Field the account's AskAI Actions are writing to. The role names below are intentionally neutral; the human-readable label you set in `config.json` is what appears in the dashboard.

| Role         | Shape of the field                                            | Where it appears in the dashboard                                        |
|--------------|---------------------------------------------------------------|--------------------------------------------------------------------------|
| `score`      | A number (typically 0–100, but any range works)               | Headline KPI, distribution buckets, daily average line, agent leaderboard |
| `star`       | A small numeric rating (1–5)                                  | Headline KPI, distribution buckets, agent leaderboard                    |
| `categories` | One or more short text values (semicolon-separated supported) | A "Top values" table; KPI card for the most common value                 |
| `highlights` | One or more short text values (semicolon-separated supported) | A "Top values" table; shown next to highest-scoring calls                |
| `concerns`   | One or more short text values (semicolon or comma-separated)  | A "Top values" table; shown next to lowest-scoring calls; KPI card       |
| `notes`      | Free-text                                                     | Shown verbatim under each highlighted call                               |

**The same slot fits very different AskAI use cases.** A few examples:

| Use case                          | `score`              | `star`            | `categories`         | `highlights`        | `concerns`          | `notes`              |
|-----------------------------------|----------------------|-------------------|----------------------|---------------------|---------------------|----------------------|
| Agent QA / call scoring (Outlook) | Total Call Score     | Star Rating       | Wrap-Up Code         | Agent Strengths     | Coaching Opps       | Score Notes          |
| Sales discovery                   | Discovery Score      | —                 | Topics Discussed     | Buying Signals      | Objections          | Call Summary         |
| Sentiment & CX                    | Sentiment Score      | CSAT (1–5)        | Themes               | Positive Moments    | Negative Moments    | Summary              |
| Lead qualification                | Lead Quality Score   | —                 | Lead Stage           | Qualifying Answers  | Disqualifiers       | Notes for Sales      |
| Compliance review                 | Compliance Score     | Risk Level (1–5)  | Disclosures Made     | Good Practices      | Compliance Flags    | Reviewer Notes       |

**Every role is optional.** Leave any role out of `fields` and the dashboard simply doesn't render that section. So an account that's only using AskAI for sentiment scoring might only configure `score` and `notes`, and the dashboard will adapt.

**Where the field names come from.** Each AskAI Action in CTM writes its answer to one Custom Field that you (or the account owner) named when setting up the Action. There is no standard naming convention — `total_call_score`, `score`, `qa_overall`, `sentiment_value` could all be valid `score` keys depending on how the account was configured. The fastest way to find them is to open any recently assessed activity in CTM and scroll to the Custom Fields section.

### `customer_segments`

It's common for an account to run different AskAI prompts depending on the type of customer on the call — existing vs. new, VIP vs. standard, etc. — using a Workflow rule that branches on a tag. Because the prompt is different, the scoring rubric is different too, so it's important to compare like with like.

Tell the dashboard which tags mean which segment and you'll get a segment toggle and a side-by-side comparison table.

```jsonc
"customer_segments": [
  {"id": "existing", "label": "Existing Customer", "short": "EC", "tags": ["ec"]},
  {"id": "new",      "label": "New Customer",      "short": "NC", "tags": ["nc"]}
]
```

A few real-world variations:

```jsonc
"customer_segments": [
  {"id": "vip",     "label": "VIP",             "short": "VIP",   "tags": ["vip", "platinum"]},
  {"id": "trial",   "label": "Free Trial",      "short": "Trial", "tags": ["trial"]},
  {"id": "churned", "label": "Churned",         "short": "X",     "tags": ["churned", "cancelled"]}
]
```

Any number of segments is fine. If an activity carries tags from multiple segments, the first match in your list wins. Activities matching no segment go into the **Other** bucket in the toggle so nothing is hidden.

## Common things you'll want to do

```bash
# Different time window
python3 ai_assessment_report.py --days-back 14
python3 ai_assessment_report.py --start-date 2026-04-01 --end-date 2026-04-30

# Quick check before committing to a full pull (caps pages per day)
python3 ai_assessment_report.py --max-pages 5

# Use a different account without touching config.json
python3 ai_assessment_report.py --account-id 12345 --auth-env-key acme --customer-name "Acme Corp"

# Write the output somewhere specific
python3 ai_assessment_report.py --output ~/Desktop/acme_qbr.html
```

## Where output lives

The HTML file goes wherever `output_file` (or `--output`) points — by default under `outputs/`. Both `outputs/` and `config.json` are gitignored, so credentials and customer-specific reports stay local.

You can share the resulting HTML file as-is — it's a single self-contained page with Chart.js loaded from a CDN. It doesn't call CTM when opened.

## Troubleshooting

- **"Missing CTM credentials"** — the `auth_env_key` in your config doesn't match a line in your `env.txt`. Check the spelling, and confirm `env.txt` is in one of the locations the script looks in (see "One-time setup" above).
- **"Missing account_id"** — set `account_id` in `config.json` or pass `--account-id`.
- **"No AI fields configured"** — `fields` is empty. Add at least one role.
- **Dashboard shows 0 assessments but the account uses AskAI** — the `key` values under `fields` don't match what AskAI is actually writing. Open any recently assessed call in CTM, scroll to the Custom Fields section, and copy the exact field names into `config.json`. (If even those calls show no AskAI output, confirm transcriptions are enabled and that the AskAI trigger is turned ON for the relevant tracking numbers.)
- **Some days show fewer rows than expected with `ERRORS=N` in the log** — transient CTM API errors (5xx) on individual pages; usually it's a few hundred missed rows out of tens of thousands. Rerun if it matters.

## Reference

- AskAI: <https://calltrackingmetrics.zendesk.com/hc/en-us/articles/13068462406413-AskAI>
- CTM API (Postman): <https://postman.calltrackingmetrics.com/>
