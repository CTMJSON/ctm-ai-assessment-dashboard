#!/usr/bin/env python3
"""
CTM AI Assessment Dashboard.

Pulls activities for the last N days from a CTM account, filters to
activities that have AI-assessment custom fields populated, and renders
an HTML dashboard with configurable customer-segment comparison and an
All/<segment>/Other filter toggle.

The AI custom field names, customer segment tags, account ID, customer
name, and auth env key are all driven from a JSON config file or CLI
flags - the script is account-agnostic.
"""

import argparse
import concurrent.futures
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path


API_BASE = "https://api.calltrackingmetrics.com/api/v1"
# Env file lookup order: $CTM_ENV_FILE, ./env.txt (project dir), ~/.ctm/env.txt.
DEFAULT_ENV_FILE_CANDIDATES = [
    os.environ.get("CTM_ENV_FILE"),
    str(Path(__file__).resolve().parent / "env.txt"),
    str(Path.home() / ".ctm" / "env.txt"),
]
DEFAULT_ENV_FILE = next((p for p in DEFAULT_ENV_FILE_CANDIDATES if p and Path(p).exists()), DEFAULT_ENV_FILE_CANDIDATES[1])
PAGE_SIZE = 50

# Field "roles" the dashboard knows how to render. Each role maps to a
# CTM custom_fields key via the config. Roles are optional - if a key is
# missing or null, the corresponding section degrades gracefully.
SUPPORTED_ROLES = ("score", "star", "categories", "highlights", "concerns", "notes")

DEFAULT_FIELD_LABELS = {
    "score": "Score",
    "star": "Star Rating",
    "categories": "Category",
    "highlights": "Highlights",
    "concerns": "Concerns",
    "notes": "Notes",
}

HOUR_LABELS = [
    "12AM", "1AM", "2AM", "3AM", "4AM", "5AM",
    "6AM", "7AM", "8AM", "9AM", "10AM", "11AM",
    "12PM", "1PM", "2PM", "3PM", "4PM", "5PM",
    "6PM", "7PM", "8PM", "9PM", "10PM", "11PM",
]

SEGMENT_PALETTE = [
    {"text": "#7fffd4", "border": "rgba(0,212,170,.4)", "bg": "rgba(0,212,170,.16)"},
    {"text": "#ffd28a", "border": "rgba(245,158,11,.4)", "bg": "rgba(245,158,11,.16)"},
    {"text": "#c4b5fd", "border": "rgba(139,92,246,.4)", "bg": "rgba(139,92,246,.16)"},
    {"text": "#8fdcff", "border": "rgba(30,144,255,.4)", "bg": "rgba(30,144,255,.16)"},
    {"text": "#f472b6", "border": "rgba(244,114,182,.4)", "bg": "rgba(244,114,182,.16)"},
    {"text": "#fb7185", "border": "rgba(251,113,133,.4)", "bg": "rgba(251,113,133,.16)"},
]

OTHER_SEGMENT = {"id": "_other", "label": "Other / Untagged", "short": "Other", "tags": []}

CTM_LOGO = """
<svg width="120" height="54" viewBox="0 0 200 54" fill="none" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="CTM">
  <polygon points="0,54 27,27 0,0" fill="#00b5e2"/>
  <polygon points="0,0 27,27 54,0" fill="#1b5fa8"/>
  <rect x="27" y="0" width="27" height="27" fill="#1b5fa8"/>
  <text x="62" y="38" fill="white" font-family="Arial" font-weight="bold" font-size="36">CTM</text>
</svg>
""".strip()


# ---------------------------------------------------------------------------
# Config and credentials
# ---------------------------------------------------------------------------


def load_config(path):
    cfg_path = Path(path)
    if not cfg_path.exists():
        return {}
    with cfg_path.open() as f:
        return json.load(f)


def load_env_file(path):
    env_path = Path(path)
    data = {}
    if not env_path.exists():
        return data
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line and (":" not in line or line.index("=") < line.index(":")):
            key, val = line.split("=", 1)
        elif ":" in line:
            key, val = line.split(":", 1)
        else:
            continue
        data[key.strip()] = val.strip().strip('"').strip("'")
    return data


def merged_env(env_file):
    data = load_env_file(env_file)
    for key, val in os.environ.items():
        data[key] = val
    return data


def resolve_auth_header(args, cfg):
    env_file = args.env_file or cfg.get("env_file") or str(DEFAULT_ENV_FILE)
    env = merged_env(env_file)

    preferred = []
    for key in (args.auth_env_key, cfg.get("auth_env_key")):
        if key:
            preferred.append(key)
    preferred.extend(["CTM_AUTH_TOKEN", "CTM_API_KEY", "CTM_AUTH", "CTM_BASIC_AUTH"])

    token = args.auth_token or cfg.get("auth_token")
    source = "--auth-token" if token else None
    if not token:
        for key in preferred:
            if env.get(key):
                token = env[key]
                source = key
                break
    if not token:
        raise SystemExit(
            "Missing CTM credentials. Set auth_env_key in config or pass --auth-token / --auth-env-key."
        )
    token = token.strip()
    header = token if token.lower().startswith("basic ") else f"Basic {token}"
    return header, source or "credential"


def resolve_date_range(args, cfg):
    start = args.start_date or cfg.get("start_date")
    end = args.end_date or cfg.get("end_date")
    if start and end:
        return start, end
    days_back = args.days_back if args.days_back is not None else int(cfg.get("days_back") or 30)
    today = datetime.now()
    return start or (today - timedelta(days=days_back)).strftime("%Y-%m-%d"), end or today.strftime("%Y-%m-%d")


def resolve_fields(cfg):
    """Return a dict mapping role -> {key, label}. Missing roles map to None."""
    raw = cfg.get("fields") or {}
    out = {}
    for role in SUPPORTED_ROLES:
        entry = raw.get(role)
        if entry is None:
            out[role] = None
            continue
        if isinstance(entry, str):
            out[role] = {"key": entry, "label": DEFAULT_FIELD_LABELS.get(role, role)}
        elif isinstance(entry, dict) and entry.get("key"):
            out[role] = {
                "key": entry["key"],
                "label": entry.get("label") or DEFAULT_FIELD_LABELS.get(role, role),
            }
        else:
            out[role] = None
    return out


def resolve_segments(cfg):
    """Return a list of segments: [{id, label, short, tags, color}, ...]."""
    raw = cfg.get("customer_segments") or []
    segments = []
    for i, seg in enumerate(raw):
        if not isinstance(seg, dict):
            continue
        tags = [str(t).lower().strip() for t in (seg.get("tags") or []) if str(t).strip()]
        if not tags:
            continue
        sid = str(seg.get("id") or "_".join(tags))
        segments.append({
            "id": sid,
            "label": seg.get("label") or sid.upper(),
            "short": seg.get("short") or sid.upper(),
            "tags": tags,
            "color": SEGMENT_PALETTE[i % len(SEGMENT_PALETTE)],
        })
    return segments


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------


class CTMClient:
    def __init__(self, auth_header, timeout=60, retries=3):
        self.headers = {
            "Authorization": auth_header,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
        }
        self.timeout = timeout
        self.retries = retries

    def fetch_json(self, url):
        last_error = None
        for attempt in range(self.retries + 1):
            try:
                req = urllib.request.Request(url, headers=self.headers)
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    body = resp.read().decode("utf-8")
                    return json.loads(body) if body else {}
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")[:500]
                last_error = RuntimeError(f"HTTP {exc.code} for {url}: {body}")
                if exc.code in (429, 500, 502, 503, 504) and attempt < self.retries:
                    retry_after = exc.headers.get("Retry-After")
                    delay = int(retry_after) if retry_after and str(retry_after).isdigit() else (2 + attempt * 2)
                    time.sleep(delay)
                    continue
                raise last_error
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = RuntimeError(f"Request failed for {url}: {exc}")
                if attempt < self.retries:
                    time.sleep(1 + attempt)
                    continue
                raise last_error
        raise last_error


def build_search_url(account_id, page, start_date, end_date):
    params = [
        ("per_page", str(PAGE_SIZE)),
        ("page", str(page)),
        ("reported", "1"),
        ("start_date", start_date),
        ("end_date", end_date),
    ]
    return f"{API_BASE}/accounts/{account_id}/calls/search.json?{urllib.parse.urlencode(params)}"


def daterange(start_date, end_date):
    start = datetime.strptime(start_date, "%Y-%m-%d").date()
    end = datetime.strptime(end_date, "%Y-%m-%d").date()
    d = start
    while d <= end:
        yield d.strftime("%Y-%m-%d")
        d = d + timedelta(days=1)


def fetch_day(client, account_id, day, max_pages):
    """The CTM search endpoint caps a query at 10k rows regardless of pagination,
    so we chunk by day to guarantee full coverage."""
    page = 1
    rows = []
    pages_fetched = 0
    errors = []
    total_entries = None
    total_pages = None
    while True:
        try:
            data = client.fetch_json(build_search_url(account_id, page, day, day))
        except Exception as exc:
            errors.append({"page": page, "day": day, "error": str(exc)})
            break
        batch = data.get("calls") or []
        rows.extend(batch)
        pages_fetched += 1
        if total_entries is None:
            total_entries = int(data.get("total_entries") or 0)
            total_pages = int(data.get("total_pages") or 1)
        if max_pages and pages_fetched >= max_pages:
            break
        if page >= (total_pages or 1):
            break
        if not batch:
            break
        page += 1
    return {
        "day": day,
        "rows": rows,
        "pages_fetched": pages_fetched,
        "total_entries": total_entries or len(rows),
        "total_pages": total_pages or pages_fetched,
        "errors": errors,
    }


def fetch_all_pages(client, account_id, start_date, end_date, max_pages, workers):
    days = list(daterange(start_date, end_date))
    print(f"  Chunking by day across {len(days)} days ({start_date} -> {end_date})")
    calls = []
    total_entries_sum = 0
    total_pages_sum = 0
    pages_fetched_sum = 0
    errors = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(fetch_day, client, account_id, day, max_pages): day for day in days}
        completed = 0
        for fut in concurrent.futures.as_completed(futures):
            result = fut.result()
            completed += 1
            calls.extend(result["rows"])
            total_entries_sum += result["total_entries"]
            total_pages_sum += result["total_pages"]
            pages_fetched_sum += result["pages_fetched"]
            errors.extend(result["errors"])
            print(
                f"  [{completed:>2}/{len(days)}] {result['day']}: "
                f"{len(result['rows']):,}/{result['total_entries']:,} rows "
                f"({result['pages_fetched']}/{result['total_pages']} pages)"
                + (f" ERRORS={len(result['errors'])}" if result['errors'] else "")
            )

    return {
        "calls": calls,
        "total_pages": total_pages_sum,
        "total_entries": total_entries_sum,
        "pages_fetched": pages_fetched_sum,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def clean_text(value, default="Unspecified"):
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def to_int(value, default=0):
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def to_float(value, default=None):
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_pct(part, whole, digits=1):
    return round((part / whole) * 100, digits) if whole else 0


def avg(values, digits=1):
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), digits) if vals else 0


def parse_dt(activity):
    called_at = activity.get("called_at")
    if called_at:
        for fmt in ("%Y-%m-%d %I:%M %p %z", "%Y-%m-%d %H:%M:%S %z"):
            try:
                return datetime.strptime(called_at, fmt)
            except ValueError:
                pass
    ts = activity.get("@timestamp")
    if ts:
        try:
            return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except ValueError:
            pass
    unix_time = activity.get("unix_time")
    if unix_time:
        try:
            return datetime.fromtimestamp(float(unix_time), tz=timezone.utc)
        except (TypeError, ValueError):
            pass
    return None


def has_ai_assessment(activity, fields):
    cf = activity.get("custom_fields") or {}
    for role in SUPPORTED_ROLES:
        spec = fields.get(role)
        if not spec:
            continue
        v = cf.get(spec["key"])
        if v is None:
            continue
        if isinstance(v, str) and not v.strip():
            continue
        if isinstance(v, list) and not v:
            continue
        return True
    return False


def segment_for(activity, segments):
    tags = {str(t).lower().strip() for t in (activity.get("tag_list") or [])}
    for seg in segments:
        if any(tag in tags for tag in seg["tags"]):
            return seg["id"]
    return OTHER_SEGMENT["id"]


def activity_kind(activity):
    direction = str(activity.get("direction") or "").lower().strip()
    if direction in ("chat", "inbound", "outbound"):
        return direction
    return direction or "other"


def split_multivalue(raw):
    if raw is None:
        return []
    if isinstance(raw, list):
        items = raw
    else:
        items = re.split(r"[;|]", str(raw))
    return [item.strip() for item in items if item and str(item).strip()]


def split_concerns(raw):
    """Concerns can be ; or , delimited."""
    if raw is None:
        return []
    if isinstance(raw, list):
        items = raw
    else:
        items = re.split(r"[;,|]", str(raw))
    return [item.strip() for item in items if item and str(item).strip()]


def agent_name(activity):
    agent = activity.get("agent")
    if isinstance(agent, dict) and clean_text(agent.get("name"), ""):
        return clean_text(agent.get("name"))
    return clean_text(activity.get("agent_id"), "Unassigned")


def get_field(activity, fields, role):
    spec = fields.get(role)
    if not spec:
        return None
    cf = activity.get("custom_fields") or {}
    return cf.get(spec["key"])


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------


SCORE_BUCKET_ORDER = ["90-100", "80-89", "70-79", "60-69", "50-59", "0-49", "Unscored"]
STAR_BUCKET_ORDER = ["5", "4", "3", "2", "1", "Unrated"]


def score_bucket(score):
    if score is None:
        return "Unscored"
    s = float(score)
    if s >= 90: return "90-100"
    if s >= 80: return "80-89"
    if s >= 70: return "70-79"
    if s >= 60: return "60-69"
    if s >= 50: return "50-59"
    return "0-49"


def star_bucket(star):
    if star is None:
        return "Unrated"
    try:
        n = int(round(float(star)))
        if 1 <= n <= 5:
            return str(n)
    except (TypeError, ValueError):
        pass
    return "Unrated"


def empty_stats():
    return {
        "total": 0,
        "scores": [],
        "stars": [],
        "by_kind": Counter(),
        "by_agent": defaultdict(lambda: {"count": 0, "scores": [], "stars": []}),
        "categories": Counter(),
        "highlights": Counter(),
        "concerns": Counter(),
        "hour_counts": Counter(),
        "weekday_counts": Counter(),
        "date_counts": Counter(),
        "date_scores": defaultdict(list),
        "score_buckets": Counter(),
        "star_buckets": Counter(),
        "samples": [],
    }


def add_activity(stats, activity, fields):
    score = to_float(get_field(activity, fields, "score"))
    star = to_float(get_field(activity, fields, "star"))
    kind = activity_kind(activity)
    name = agent_name(activity)
    dt = parse_dt(activity)

    stats["total"] += 1
    if score is not None: stats["scores"].append(score)
    if star is not None: stats["stars"].append(star)
    stats["by_kind"][kind] += 1
    stats["score_buckets"][score_bucket(score)] += 1
    stats["star_buckets"][star_bucket(star)] += 1

    entry = stats["by_agent"][name]
    entry["count"] += 1
    if score is not None: entry["scores"].append(score)
    if star is not None: entry["stars"].append(star)

    for v in split_multivalue(get_field(activity, fields, "categories")):
        stats["categories"][v] += 1
    for v in split_multivalue(get_field(activity, fields, "highlights")):
        stats["highlights"][v] += 1
    for v in split_concerns(get_field(activity, fields, "concerns")):
        stats["concerns"][v] += 1

    if dt:
        stats["hour_counts"][dt.hour] += 1
        stats["weekday_counts"][dt.strftime("%A")] += 1
        date_key = dt.strftime("%Y-%m-%d")
        stats["date_counts"][date_key] += 1
        if score is not None:
            stats["date_scores"][date_key].append(score)

    stats["samples"].append({
        "id": activity.get("id"),
        "account_id": activity.get("account_id"),
        "dt": dt,
        "called_at": clean_text(activity.get("called_at"), ""),
        "direction": kind,
        "agent": name,
        "score": score,
        "star": star,
        "categories": clean_text(get_field(activity, fields, "categories"), ""),
        "highlights": clean_text(get_field(activity, fields, "highlights"), ""),
        "concerns": clean_text(get_field(activity, fields, "concerns"), ""),
        "notes": clean_text(get_field(activity, fields, "notes"), ""),
        "duration": to_int(activity.get("duration")),
    })


def finalize(stats):
    stats["avg_score"] = avg(stats["scores"], 1) if stats["scores"] else None
    stats["avg_star"] = avg(stats["stars"], 2) if stats["stars"] else None
    stats["scored_count"] = len(stats["scores"])
    stats["rated_count"] = len(stats["stars"])

    agent_rows = []
    for name, entry in stats["by_agent"].items():
        agent_rows.append({
            "name": name,
            "count": entry["count"],
            "avg_score": avg(entry["scores"], 1) if entry["scores"] else None,
            "avg_star": avg(entry["stars"], 2) if entry["stars"] else None,
            "scored": len(entry["scores"]),
        })
    stats["agent_rows"] = agent_rows

    stats["samples"].sort(key=lambda x: x["dt"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    stats["low_scores"] = sorted(
        [s for s in stats["samples"] if s["score"] is not None],
        key=lambda x: x["score"],
    )[:15]
    stats["high_scores"] = sorted(
        [s for s in stats["samples"] if s["score"] is not None],
        key=lambda x: x["score"],
        reverse=True,
    )[:15]


def build_slices(activities, segments, fields):
    slices = {"all": empty_stats()}
    seg_ids = [s["id"] for s in segments] + [OTHER_SEGMENT["id"]]
    for sid in seg_ids:
        slices[sid] = empty_stats()

    for a in activities:
        add_activity(slices["all"], a, fields)
        sid = segment_for(a, segments)
        add_activity(slices[sid], a, fields)

    for s in slices.values():
        finalize(s)
    return slices


# ---------------------------------------------------------------------------
# JSON-safe stats for embedding
# ---------------------------------------------------------------------------


def stats_to_payload(stats, date_keys):
    score_bucket_values = [stats["score_buckets"].get(b, 0) for b in SCORE_BUCKET_ORDER]
    star_bucket_values = [stats["star_buckets"].get(b, 0) for b in STAR_BUCKET_ORDER]
    hour_values = [stats["hour_counts"].get(h, 0) for h in range(24)]

    daily_volume = [stats["date_counts"].get(d, 0) for d in date_keys]
    daily_avg_score = []
    for d in date_keys:
        scores = stats["date_scores"].get(d) or []
        daily_avg_score.append(round(sum(scores) / len(scores), 1) if scores else None)

    kind_labels = [k for k, _ in stats["by_kind"].most_common()]
    kind_values = [stats["by_kind"][k] for k in kind_labels]

    categories = stats["categories"].most_common(15)
    highlights = stats["highlights"].most_common(15)
    concerns = stats["concerns"].most_common(15)

    agent_rows = sorted(stats["agent_rows"], key=lambda r: (-(r["count"]), r["name"]))[:30]

    return {
        "total": stats["total"],
        "scored_count": stats["scored_count"],
        "rated_count": stats["rated_count"],
        "avg_score": stats["avg_score"],
        "avg_star": stats["avg_star"],
        "score_buckets": {"labels": SCORE_BUCKET_ORDER, "values": score_bucket_values},
        "star_buckets": {"labels": STAR_BUCKET_ORDER, "values": star_bucket_values},
        "hour": {"labels": HOUR_LABELS, "values": hour_values},
        "daily": {"labels": date_keys, "volume": daily_volume, "avg_score": daily_avg_score},
        "by_kind": {"labels": kind_labels, "values": kind_values},
        "categories": {"labels": [w[0] for w in categories], "values": [w[1] for w in categories]},
        "highlights": {"labels": [w[0] for w in highlights], "values": [w[1] for w in highlights]},
        "concerns": {"labels": [w[0] for w in concerns], "values": [w[1] for w in concerns]},
        "agents": agent_rows,
        "low_scores": [serialize_sample(s) for s in stats["low_scores"]],
        "high_scores": [serialize_sample(s) for s in stats["high_scores"]],
    }


def serialize_sample(sample):
    dt = sample.get("dt")
    return {
        "id": sample.get("id"),
        "account_id": sample.get("account_id"),
        "when": sample.get("called_at") or (dt.strftime("%Y-%m-%d %I:%M %p") if dt else ""),
        "direction": sample.get("direction"),
        "agent": sample.get("agent"),
        "score": sample.get("score"),
        "star": sample.get("star"),
        "categories": sample.get("categories"),
        "highlights": sample.get("highlights"),
        "concerns": sample.get("concerns"),
        "notes": sample.get("notes"),
        "duration": sample.get("duration"),
    }


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------


def esc(value):
    return html.escape(str(value if value is not None else ""))


def js(value):
    return json.dumps(value, ensure_ascii=True, default=str)


def render_kpi(label, value, sub="", cls=""):
    return f"""
    <div class="kpi-card {esc(cls)}">
      <div class="kpi-label">{esc(label)}</div>
      <div class="kpi-value">{esc(value)}</div>
      <div class="kpi-sub">{esc(sub)}</div>
    </div>
    """


def _top_label(counter, fallback="--", max_len=32):
    if not counter:
        return fallback
    label, _ = counter.most_common(1)[0]
    return (label[:max_len - 2] + "...") if len(label) > max_len else label


def _top_share(counter, total, fallback=""):
    if not counter or not total:
        return fallback
    _, count = counter.most_common(1)[0]
    return f"{count:,} ({safe_pct(count, total)}%)"


def render_summary_kpis(overall, segments, slices, fetch_meta, fields):
    total = overall["total"]
    fetched_total = fetch_meta.get("total_entries", total) or total
    coverage_pct = safe_pct(total, fetched_total)

    segment_summary = " / ".join(
        f"{slices[s['id']]['total']:,} {s['short']}" for s in segments[:3]
    ) or "n/a"

    cards = [
        render_kpi("AI Assessments", f"{total:,}", f"{coverage_pct}% of {fetched_total:,} activities pulled"),
    ]

    if fields.get("score"):
        cards.append(render_kpi(
            f"Avg {fields['score']['label']}",
            f"{overall['avg_score']:.1f}" if overall["avg_score"] is not None else "--",
            f"{overall['scored_count']:,} scored",
            "kpi-green",
        ))

    if fields.get("star"):
        cards.append(render_kpi(
            f"Avg {fields['star']['label']}",
            f"{overall['avg_star']:.2f}" if overall["avg_star"] is not None else "--",
            f"{overall['rated_count']:,} rated",
            "kpi-blue",
        ))

    if segments:
        cards.append(render_kpi(
            "Segment Mix",
            segment_summary,
            ", ".join(f"{safe_pct(slices[s['id']]['total'], total)}% {s['short']}" for s in segments[:3]),
            "kpi-warn",
        ))

    inbound = overall["by_kind"].get("inbound", 0)
    outbound = overall["by_kind"].get("outbound", 0)
    chat = overall["by_kind"].get("chat", 0)
    cards.append(render_kpi("Calls", f"{inbound + outbound:,}", f"{inbound:,} in / {outbound:,} out"))
    cards.append(render_kpi("Chats", f"{chat:,}", "Direction=chat", "kpi-blue"))

    if fields.get("categories"):
        cards.append(render_kpi(
            f"Top {fields['categories']['label']}",
            _top_label(overall["categories"]),
            _top_share(overall["categories"], total),
        ))
    if fields.get("concerns"):
        cards.append(render_kpi(
            f"Top {fields['concerns']['label']}",
            _top_label(overall["concerns"]),
            _top_share(overall["concerns"], total),
            "kpi-warn",
        ))

    return "\n".join(cards)


def render_segment_compare(segments, slices, fields):
    """Build a comparison table with one column per defined segment."""
    if not segments:
        return ""

    def num(v, fmt="{:,}"):
        return "--" if v is None else fmt.format(v)

    rows = [("Assessments", lambda s: num(s["total"]))]
    if fields.get("score"):
        rows.append((f"Avg {fields['score']['label']}", lambda s: num(s["avg_score"], "{:.1f}") if s["avg_score"] is not None else "--"))
    if fields.get("star"):
        rows.append((f"Avg {fields['star']['label']}", lambda s: num(s["avg_star"], "{:.2f}") if s["avg_star"] is not None else "--"))
    if fields.get("score"):
        rows.append(("Scored Activities", lambda s: num(s["scored_count"])))
    rows.extend([
        ("Calls (inbound)", lambda s: num(s["by_kind"].get("inbound", 0))),
        ("Calls (outbound)", lambda s: num(s["by_kind"].get("outbound", 0))),
        ("Chats", lambda s: num(s["by_kind"].get("chat", 0))),
    ])
    if fields.get("categories"):
        rows.append((f"Top {fields['categories']['label']}", lambda s: _top_label(s["categories"], "--")))
    if fields.get("highlights"):
        rows.append((f"Top {fields['highlights']['label']}", lambda s: _top_label(s["highlights"], "--")))
    if fields.get("concerns"):
        rows.append((f"Top {fields['concerns']['label']}", lambda s: _top_label(s["concerns"], "--")))

    header_cells = "".join(
        f'<th>{esc(s["label"])} ({esc(s["short"])})</th>' for s in segments
    )
    tbody_rows = []
    for label, getter in rows:
        cells = "".join(
            f'<td class="num seg-col seg-col-{esc(seg["id"])}">{esc(getter(slices[seg["id"]]))}</td>'
            for seg in segments
        )
        tbody_rows.append(f"<tr><td>{esc(label)}</td>{cells}</tr>")

    return f"""
    <section class="dashboard-section">
      <div class="section-title">Segment Comparison</div>
      <table class="data-table segment-table">
        <thead><tr><th>Metric</th>{header_cells}</tr></thead>
        <tbody>
          {"".join(tbody_rows)}
        </tbody>
      </table>
    </section>
    """


def render_toggle(segments):
    buttons = ['<button data-slice="all" class="active">All</button>']
    for seg in segments:
        buttons.append(f'<button data-slice="{esc(seg["id"])}">{esc(seg["short"])}</button>')
    buttons.append(f'<button data-slice="{OTHER_SEGMENT["id"]}">{OTHER_SEGMENT["short"]}</button>')
    return "\n".join(buttons)


def render_fetch_notes(meta, start_date, end_date, ai_count):
    parts = [
        f"<li>Pulled <strong>{meta.get('total_entries', 0):,}</strong> total activities for {esc(start_date)} - {esc(end_date)}.</li>",
        f"<li><strong>{meta.get('pages_fetched', 0):,}</strong> pages fetched across <strong>{len(list(daterange(start_date, end_date)))}</strong> day-chunks.</li>",
        f"<li><strong>{ai_count:,}</strong> activities had AI assessment custom fields populated.</li>",
    ]
    for err in meta.get("errors") or []:
        parts.append(f"<li class=\"error-item\">Day {esc(err.get('day'))} page {esc(err.get('page'))}: {esc(err.get('error'))}</li>")
    return "\n".join(parts)


def render_segment_styles(segments):
    """Generate per-segment badge colors using each segment's palette entry."""
    css = []
    for seg in segments:
        c = seg["color"]
        css.append(
            f".badge-seg-{seg['id']}{{background:{c['bg']};color:{c['text']};border-color:{c['border']}}}"
        )
    return "\n".join(css)


def render_html(customer_name, account_id, start_date, end_date, segments, slices, fetch_meta, fields, ai_count, output_file):
    generated_at = datetime.now().strftime("%B %d, %Y %I:%M %p")
    all_dates = sorted({d for s in slices.values() for d in s["date_counts"].keys()})

    payload = {
        "slices": {sid: stats_to_payload(s, all_dates) for sid, s in slices.items()},
        "segments": [{"id": s["id"], "label": s["label"], "short": s["short"]} for s in segments]
                    + [{"id": OTHER_SEGMENT["id"], "label": OTHER_SEGMENT["label"], "short": OTHER_SEGMENT["short"]}],
        "fields": {role: spec for role, spec in fields.items()},
    }

    field_labels_js = {role: (spec["label"] if spec else None) for role, spec in fields.items()}

    overall = slices["all"]
    summary_kpis = render_summary_kpis(overall, segments, slices, fetch_meta, fields)
    segment_section = render_segment_compare(segments, slices, fields)
    toggle_buttons = render_toggle(segments)
    fetch_notes = render_fetch_notes(fetch_meta, start_date, end_date, ai_count)
    segment_styles = render_segment_styles(segments)

    has_score = fields.get("score") is not None
    has_star = fields.get("star") is not None
    has_categories = fields.get("categories") is not None
    has_highlights = fields.get("highlights") is not None
    has_concerns = fields.get("concerns") is not None

    score_section = ""
    if has_score or has_star:
        cols = []
        if has_score:
            cols.append('<div class="chart-box"><h3>' + esc(fields["score"]["label"]) + ' Buckets</h3><canvas id="scoreChart"></canvas></div>')
        if has_star:
            cols.append('<div class="chart-box"><h3>' + esc(fields["star"]["label"]) + ' Buckets</h3><canvas id="starChart"></canvas></div>')
        score_section = f"""
        <section class="dashboard-section">
          <div class="section-title">Score &amp; Rating Distribution</div>
          <div class="chart-grid">{"".join(cols)}</div>
        </section>
        """

    categorical_section = ""
    panels = []
    section_labels = []
    if has_categories:
        section_labels.append(fields["categories"]["label"])
        panels.append(f'<div class="panel"><h3>Top {esc(fields["categories"]["label"])}</h3><table class="data-table"><thead><tr><th>Value</th><th class="num">Count</th><th>Share</th></tr></thead><tbody id="categoryRows"></tbody></table></div>')
    if has_highlights:
        section_labels.append(fields["highlights"]["label"])
        panels.append(f'<div class="panel"><h3>Top {esc(fields["highlights"]["label"])}</h3><table class="data-table"><thead><tr><th>Value</th><th class="num">Count</th><th>Share</th></tr></thead><tbody id="highlightRows"></tbody></table></div>')
    if has_concerns:
        section_labels.append(fields["concerns"]["label"])
        panels.append(f'<div class="panel"><h3>Top {esc(fields["concerns"]["label"])}</h3><table class="data-table"><thead><tr><th>Value</th><th class="num">Count</th><th>Share</th></tr></thead><tbody id="concernRows"></tbody></table></div>')
    if panels:
        section_title = " · ".join(section_labels) if section_labels else "Categorical Outputs"
        categorical_section = f"""
        <section class="dashboard-section">
          <div class="section-title">{esc(section_title)}</div>
          <div class="table-grid">{"".join(panels)}</div>
        </section>
        """

    replacements = {
        "{{customer_name}}": esc(customer_name),
        "{{account_id}}": esc(account_id),
        "{{date_range}}": f"{esc(start_date)} - {esc(end_date)}",
        "{{generated_at}}": esc(generated_at),
        "{{ctm_logo}}": CTM_LOGO,
        "{{summary_kpis}}": summary_kpis,
        "{{segment_section}}": segment_section,
        "{{toggle_buttons}}": toggle_buttons,
        "{{fetch_notes}}": fetch_notes,
        "{{segment_styles}}": segment_styles,
        "{{score_section}}": score_section,
        "{{categorical_section}}": categorical_section,
        "{{payload}}": js(payload),
        "{{field_labels}}": js(field_labels_js),
        "{{has_score}}": "true" if has_score else "false",
        "{{has_star}}": "true" if has_star else "false",
        "{{has_categories}}": "true" if has_categories else "false",
        "{{has_highlights}}": "true" if has_highlights else "false",
        "{{has_concerns}}": "true" if has_concerns else "false",
        "{{highlights_label}}": esc(fields["highlights"]["label"]) if has_highlights else "Highlights",
        "{{concerns_label}}": esc(fields["concerns"]["label"]) if has_concerns else "Concerns",
        "{{output_file}}": esc(output_file),
    }
    doc = DASHBOARD_TEMPLATE
    for key, val in replacements.items():
        doc = doc.replace(key, str(val))
    return doc


DASHBOARD_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{{customer_name}} - AI Assessment Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
:root {
  --bg-darkest:#071220; --bg-dark:#0d1b2a; --bg-card:#132033; --bg-card2:#162640;
  --panel:#0f1b2d; --border:#1e3a5f; --accent:#1e90ff; --accent2:#00d4aa;
  --warn:#f59e0b; --danger:#ef4444; --text:#e2eaf4; --text-muted:#7a9cc0;
  --ctm-blue:#1b5fa8; --ctm-cyan:#00b5e2;
}
*{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth}
body{background:var(--bg-darkest);color:var(--text);font-family:'Segoe UI',system-ui,sans-serif;font-size:14px;line-height:1.5}
a{color:#8fdcff;text-decoration:none}
a:hover{text-decoration:underline}
.main{max-width:1400px;margin:0 auto;padding:32px 36px}
.page-header{display:flex;align-items:flex-start;justify-content:space-between;margin-bottom:18px;gap:20px;flex-wrap:wrap}
.page-title{font-size:28px;font-weight:700;color:#fff;line-height:1.2}
.page-sub{color:var(--text-muted);font-size:13px;margin-top:6px}
.report-date{background:var(--bg-card);border:1px solid var(--border);border-radius:8px;padding:10px 16px;font-size:12px;color:var(--text-muted);text-align:right;white-space:nowrap}
.report-date strong{color:var(--text);display:block;font-size:13px}
.brand-row{display:flex;align-items:center;gap:18px;margin-bottom:18px}
.summary-grid{display:grid;grid-template-columns:repeat(4,minmax(160px,1fr));gap:16px;margin-bottom:24px}
.kpi-card{background:var(--bg-card);border:1px solid var(--border);border-radius:10px;padding:18px;position:relative;overflow:hidden;min-height:118px}
.kpi-card::before{content:'';position:absolute;top:0;left:0;right:0;height:3px;background:linear-gradient(90deg,var(--ctm-blue),var(--ctm-cyan))}
.kpi-card .kpi-label{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--text-muted);margin-bottom:8px}
.kpi-card .kpi-value{font-size:26px;font-weight:700;color:#fff;line-height:1.05;word-break:break-word}
.kpi-card .kpi-sub{font-size:12px;color:var(--text-muted);margin-top:7px}
.kpi-green .kpi-value{color:var(--accent2)}
.kpi-blue .kpi-value{color:#8fdcff}
.kpi-warn .kpi-value{color:var(--warn)}
.dashboard-section{background:var(--bg-card);border:1px solid var(--border);border-radius:10px;padding:22px;margin-bottom:24px}
.section-title{font-size:15px;font-weight:600;color:#fff;margin-bottom:16px;display:flex;align-items:center;gap:10px}
.section-title::before{content:'';width:4px;height:18px;background:linear-gradient(var(--ctm-blue),var(--ctm-cyan));border-radius:2px}
.toggle-bar{display:flex;align-items:center;gap:12px;flex-wrap:wrap;background:var(--bg-card2);border:1px solid var(--border);border-radius:10px;padding:12px 18px;margin-bottom:18px;position:sticky;top:0;z-index:10}
.toggle-bar .label{color:var(--text-muted);font-size:12px;text-transform:uppercase;letter-spacing:.08em}
.toggle-bar .toggle{display:inline-flex;border:1px solid var(--border);border-radius:8px;overflow:hidden;flex-wrap:wrap}
.toggle-bar .toggle button{background:transparent;color:var(--text-muted);border:none;padding:7px 16px;font-size:13px;cursor:pointer;font-weight:500;transition:all .15s}
.toggle-bar .toggle button.active{background:linear-gradient(90deg,var(--ctm-blue),var(--ctm-cyan));color:#fff}
.toggle-bar .toggle button:not(.active):hover{background:rgba(30,144,255,.08);color:var(--text)}
.toggle-bar .live-kpis{display:flex;gap:18px;margin-left:auto}
.toggle-bar .live-kpis div{font-size:12px;color:var(--text-muted)}
.toggle-bar .live-kpis div strong{color:var(--text);display:block;font-size:16px;margin-top:2px}
.chart-grid{display:grid;grid-template-columns:1fr 1fr;gap:18px}
.chart-grid.three{grid-template-columns:1fr 1fr 1fr}
.chart-grid.full{grid-template-columns:1fr}
.chart-box{background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:16px;height:330px;display:flex;flex-direction:column;min-width:0}
.chart-box.tall{height:380px}
.chart-box h3,.panel h3{font-size:12px;text-transform:uppercase;letter-spacing:.07em;color:var(--text-muted);margin-bottom:12px;font-weight:600}
.chart-box canvas{flex:1;min-height:0}
.table-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:18px}
.table-grid.two{grid-template-columns:1fr 1fr}
.panel{background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:16px;min-width:0}
.data-table{width:100%;border-collapse:collapse;font-size:12px}
.data-table th{text-align:left;padding:7px 8px;color:var(--text-muted);border-bottom:1px solid var(--border);font-weight:500;font-size:11px}
.data-table td{padding:8px;border-bottom:1px solid rgba(30,58,95,.45);color:var(--text);vertical-align:top}
.data-table tr:last-child td{border-bottom:none}
.data-table.compact td:first-child{color:var(--text-muted)}
.data-table.wide{min-width:850px}
.num{text-align:right;font-variant-numeric:tabular-nums}
.empty-row{color:var(--text-muted);font-style:italic;text-align:center}
.progress-bar-wrap{display:flex;align-items:center;gap:8px;min-width:140px}
.progress-bar-fill{height:6px;border-radius:3px;background:linear-gradient(90deg,var(--ctm-blue),var(--ctm-cyan));min-width:2px;max-width:95px}
.progress-bar-wrap span{font-size:11px;color:var(--text-muted);font-variant-numeric:tabular-nums}
.badge{display:inline-block;background:rgba(30,144,255,.15);color:#8fdcff;border:1px solid rgba(30,144,255,.35);border-radius:4px;padding:2px 8px;font-size:11px;margin:0 5px 5px 0}
.badge-chat{background:rgba(139,92,246,.18);color:#c4b5fd;border-color:rgba(139,92,246,.4)}
.badge-inbound{background:rgba(30,144,255,.18);color:#8fdcff;border-color:rgba(30,144,255,.4)}
.badge-outbound{background:rgba(0,212,170,.18);color:#7fffd4;border-color:rgba(0,212,170,.4)}
.badge-other,.badge-unknown,.badge-seg-_other{background:rgba(255,255,255,.05);color:var(--text-muted);border-color:rgba(255,255,255,.08)}
{{segment_styles}}
.segment-table th{text-transform:uppercase;letter-spacing:.06em;font-size:11px}
.segment-table td:first-child{color:var(--text-muted)}
.segment-table .seg-col{font-weight:600;color:#fff}
.notes-cell{max-width:420px;white-space:normal;color:var(--text-muted);font-size:11px;line-height:1.45}
.assessment-table td{vertical-align:top}
.assessment-table tr:hover td{background:rgba(30,144,255,.05)}
.score-pill{display:inline-block;border-radius:12px;padding:2px 10px;font-weight:600;font-size:12px;background:rgba(30,144,255,.15);color:#8fdcff}
.score-pill.high{background:rgba(0,212,170,.18);color:#7fffd4}
.score-pill.mid{background:rgba(245,158,11,.18);color:#ffd28a}
.score-pill.low{background:rgba(239,68,68,.18);color:#ff9b9b}
.stars{color:#fbbf24;letter-spacing:1px}
.notes-list li{margin-bottom:6px;list-style:none;color:var(--text-muted);font-size:12px}
.notes-list li.error-item{color:#ff9b9b}
.footer{text-align:center;padding:32px 0 48px;color:var(--text-muted);font-size:12px;border-top:1px solid var(--border);margin-top:20px}
@media(max-width:1100px){.summary-grid{grid-template-columns:repeat(2,1fr)}.chart-grid,.chart-grid.three,.table-grid,.table-grid.two{grid-template-columns:1fr}}
@media(max-width:720px){.main{padding:20px 16px}.page-header{flex-direction:column}.summary-grid{grid-template-columns:1fr}.kpi-card .kpi-value{font-size:24px}.toggle-bar{flex-direction:column;align-items:flex-start}.toggle-bar .live-kpis{margin-left:0}}
</style>
</head>
<body>
<main class="main">
  <div class="brand-row">{{ctm_logo}}<div><div class="page-title">AI Assessment Dashboard</div><div class="page-sub">{{customer_name}} (Account #{{account_id}}) - {{date_range}}</div></div></div>

  <div class="page-header" style="margin-bottom:24px">
    <div></div>
    <div class="report-date"><strong>Report Generated</strong>{{generated_at}}</div>
  </div>

  <div class="summary-grid">
    {{summary_kpis}}
  </div>

  <div class="toggle-bar">
    <span class="label">Filter</span>
    <div class="toggle" id="custToggle">
      {{toggle_buttons}}
    </div>
    <div class="live-kpis">
      <div>Assessments<strong id="liveTotal">--</strong></div>
      <div>Avg Score<strong id="liveScore">--</strong></div>
      <div>Avg Star<strong id="liveStar">--</strong></div>
    </div>
  </div>

  {{segment_section}}

  {{score_section}}

  <section class="dashboard-section">
    <div class="section-title">Time &amp; Volume</div>
    <div class="chart-grid">
      <div class="chart-box tall"><h3>Daily Volume</h3><canvas id="dailyVolumeChart"></canvas></div>
      <div class="chart-box tall"><h3>Daily Avg Score</h3><canvas id="dailyScoreChart"></canvas></div>
    </div>
    <div class="chart-grid" style="margin-top:18px">
      <div class="chart-box"><h3>By Hour of Day</h3><canvas id="hourChart"></canvas></div>
      <div class="chart-box"><h3>Direction Mix</h3><canvas id="kindChart"></canvas></div>
    </div>
  </section>

  {{categorical_section}}

  <section class="dashboard-section">
    <div class="section-title">Agent Leaderboard</div>
    <div class="panel">
      <table class="data-table">
        <thead><tr><th>Agent</th><th class="num">Assessments</th><th class="num">Avg Score</th><th class="num">Avg Star</th><th class="num">Scored</th></tr></thead>
        <tbody id="agentRows"></tbody>
      </table>
    </div>
  </section>

  <section class="dashboard-section">
    <div class="section-title">Lowest-Scoring Assessments</div>
    <div class="panel" style="overflow-x:auto">
      <table class="data-table assessment-table wide">
        <thead><tr><th>When</th><th>Dir</th><th>Agent</th><th class="num">Score</th><th>Star</th><th>{{concerns_label}}</th><th>Notes</th></tr></thead>
        <tbody id="lowRows"></tbody>
      </table>
    </div>
  </section>

  <section class="dashboard-section">
    <div class="section-title">Highest-Scoring Assessments</div>
    <div class="panel" style="overflow-x:auto">
      <table class="data-table assessment-table wide">
        <thead><tr><th>When</th><th>Dir</th><th>Agent</th><th class="num">Score</th><th>Star</th><th>{{highlights_label}}</th><th>Notes</th></tr></thead>
        <tbody id="highRows"></tbody>
      </table>
    </div>
  </section>

  <section class="dashboard-section">
    <div class="section-title">Fetch Notes</div>
    <ul class="notes-list">{{fetch_notes}}</ul>
  </section>

  <div class="footer">Generated by CTM AI Assessment Dashboard - {{output_file}}</div>
</main>
<script>
const PAYLOAD = {{payload}};
const FIELD_LABELS = {{field_labels}};
const ACCOUNT_ID = '{{account_id}}';
const HAS_SCORE = {{has_score}};
const HAS_STAR = {{has_star}};
const HAS_CATEGORIES = {{has_categories}};
const HAS_HIGHLIGHTS = {{has_highlights}};
const HAS_CONCERNS = {{has_concerns}};

const palette = ['#00b5e2','#1e90ff','#00d4aa','#f59e0b','#8b5cf6','#ef4444','#10b981','#f472b6','#7a9cc0','#60a5fa','#34d399','#fbbf24','#ff8a80'];
Chart.defaults.color = '#c5d0de';
Chart.defaults.borderColor = 'rgba(255,255,255,0.06)';

function fmtNum(n) { return (n == null) ? '--' : Number(n).toLocaleString(); }
function fmtScore(n) { return (n == null) ? '--' : Number(n).toFixed(1); }
function fmtStar(n) { return (n == null) ? '--' : Number(n).toFixed(2); }
function esc(v) {
  if (v == null) return '';
  return String(v).replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
}
function scorePillClass(s) {
  if (s == null) return '';
  if (s >= 85) return 'high';
  if (s >= 70) return 'mid';
  return 'low';
}
function starStr(s) {
  if (s == null) return '--';
  const n = Math.round(Number(s));
  if (!(n >= 1 && n <= 5)) return '--';
  return '★'.repeat(n) + '☆'.repeat(5 - n);
}
function badgeFor(kind, cls) {
  return '<span class="badge badge-' + esc(cls || 'other') + '">' + esc(kind || '') + '</span>';
}
function rowsHtml(items, total) {
  if (!items || !items.length) return '<tr><td colspan="3" class="empty-row">No data</td></tr>';
  return items.map(function(pair) {
    var label = pair[0], count = pair[1];
    var share = total ? (count / total * 100).toFixed(1) : 0;
    return '<tr><td>' + esc(label) + '</td><td class="num">' + fmtNum(count) +
           '</td><td><div class="progress-bar-wrap"><div class="progress-bar-fill" style="width:' + share +
           '%"></div><span>' + share + '%</span></div></td></tr>';
  }).join('');
}

const charts = {};

function makeChart(id, config) {
  const el = document.getElementById(id);
  if (!el) return null;
  charts[id] = new Chart(el, config);
  return charts[id];
}

function init() {
  if (HAS_SCORE) {
    makeChart('scoreChart', {
      type: 'bar',
      data: {labels: PAYLOAD.slices.all.score_buckets.labels, datasets: [{label:'Assessments', data: PAYLOAD.slices.all.score_buckets.values, backgroundColor: 'rgba(0,181,226,.78)', borderRadius: 4}]},
      options: {responsive:true, maintainAspectRatio:false, plugins:{legend:{display:false}}, scales:{y:{beginAtZero:true}}}
    });
  }
  if (HAS_STAR) {
    makeChart('starChart', {
      type: 'bar',
      data: {labels: PAYLOAD.slices.all.star_buckets.labels, datasets: [{label:'Assessments', data: PAYLOAD.slices.all.star_buckets.values, backgroundColor: 'rgba(251,191,36,.78)', borderRadius: 4}]},
      options: {responsive:true, maintainAspectRatio:false, plugins:{legend:{display:false}}, scales:{y:{beginAtZero:true}}}
    });
  }
  makeChart('dailyVolumeChart', {
    type: 'line',
    data: {labels: PAYLOAD.slices.all.daily.labels, datasets: [{label:'Assessments', data: PAYLOAD.slices.all.daily.volume, borderColor:'#00b5e2', backgroundColor:'rgba(0,181,226,.16)', fill:true, tension:.28, pointRadius:2}]},
    options: {responsive:true, maintainAspectRatio:false, plugins:{legend:{display:false}}, scales:{y:{beginAtZero:true}}}
  });
  if (HAS_SCORE) {
    makeChart('dailyScoreChart', {
      type: 'line',
      data: {labels: PAYLOAD.slices.all.daily.labels, datasets: [{label:'Avg Score', data: PAYLOAD.slices.all.daily.avg_score, borderColor:'#00d4aa', backgroundColor:'rgba(0,212,170,.14)', fill:true, tension:.28, pointRadius:2, spanGaps:true}]},
      options: {responsive:true, maintainAspectRatio:false, plugins:{legend:{display:false}}, scales:{y:{suggestedMin:60, suggestedMax:100}}}
    });
  }
  makeChart('hourChart', {
    type: 'bar',
    data: {labels: PAYLOAD.slices.all.hour.labels, datasets: [{label:'Assessments', data: PAYLOAD.slices.all.hour.values, backgroundColor: 'rgba(30,144,255,.7)', borderRadius:3}]},
    options: {responsive:true, maintainAspectRatio:false, plugins:{legend:{display:false}}, scales:{x:{ticks:{maxRotation:0, font:{size:10}}}, y:{beginAtZero:true}}}
  });
  makeChart('kindChart', {
    type: 'doughnut',
    data: {labels: PAYLOAD.slices.all.by_kind.labels, datasets: [{data: PAYLOAD.slices.all.by_kind.values, backgroundColor: palette, borderColor:'#132033', borderWidth:2}]},
    options: {responsive:true, maintainAspectRatio:false, plugins:{legend:{position:'right', labels:{boxWidth:10, font:{size:11}}}}}
  });

  setSlice('all');
  document.querySelectorAll('#custToggle button').forEach(function(btn) {
    btn.addEventListener('click', function() {
      document.querySelectorAll('#custToggle button').forEach(function(b) { b.classList.remove('active'); });
      btn.classList.add('active');
      setSlice(btn.dataset.slice);
    });
  });
}

function setSlice(name) {
  const s = (PAYLOAD.slices && PAYLOAD.slices[name]) || PAYLOAD.slices.all;
  document.getElementById('liveTotal').textContent = fmtNum(s.total);
  document.getElementById('liveScore').textContent = fmtScore(s.avg_score);
  document.getElementById('liveStar').textContent = fmtStar(s.avg_star);

  if (HAS_SCORE) updateChart('scoreChart', s.score_buckets.labels, [{data: s.score_buckets.values}]);
  if (HAS_STAR)  updateChart('starChart',  s.star_buckets.labels,  [{data: s.star_buckets.values}]);
  updateChart('dailyVolumeChart', s.daily.labels, [{data: s.daily.volume}]);
  if (HAS_SCORE) updateChart('dailyScoreChart', s.daily.labels, [{data: s.daily.avg_score}]);
  updateChart('hourChart', s.hour.labels, [{data: s.hour.values}]);
  updateChart('kindChart', s.by_kind.labels, [{data: s.by_kind.values}]);

  if (HAS_CATEGORIES) document.getElementById('categoryRows').innerHTML = rowsHtml(s.categories.labels.map(function(l,i){return [l,s.categories.values[i]];}), s.total);
  if (HAS_HIGHLIGHTS) document.getElementById('highlightRows').innerHTML = rowsHtml(s.highlights.labels.map(function(l,i){return [l,s.highlights.values[i]];}), s.total);
  if (HAS_CONCERNS) document.getElementById('concernRows').innerHTML = rowsHtml(s.concerns.labels.map(function(l,i){return [l,s.concerns.values[i]];}), s.total);

  document.getElementById('agentRows').innerHTML = (s.agents || []).map(function(a) {
    return '<tr><td>' + esc(a.name) + '</td><td class="num">' + fmtNum(a.count) +
           '</td><td class="num">' + fmtScore(a.avg_score) +
           '</td><td class="num">' + fmtStar(a.avg_star) +
           '</td><td class="num">' + fmtNum(a.scored) + '</td></tr>';
  }).join('') || '<tr><td colspan="5" class="empty-row">No agents</td></tr>';

  document.getElementById('lowRows').innerHTML = renderSamples(s.low_scores, 'concerns');
  document.getElementById('highRows').innerHTML = renderSamples(s.high_scores, 'highlights');
}

function renderSamples(samples, detailKey) {
  if (!samples || !samples.length) return '<tr><td colspan="7" class="empty-row">No assessments</td></tr>';
  return samples.map(function(s) {
    var link = s.id ? '<a href="https://app.calltrackingmetrics.com/accounts/' + ACCOUNT_ID + '/calls/' + s.id + '" target="_blank" rel="noopener">' + esc(s.when) + '</a>' : esc(s.when);
    var dirBadge = badgeFor(s.direction || 'other', s.direction || 'unknown');
    var pillCls = scorePillClass(s.score);
    var scoreCell = (s.score != null) ? '<span class="score-pill ' + pillCls + '">' + Number(s.score).toFixed(0) + '</span>' : '--';
    var detail = esc(s[detailKey] || '');
    var notes = esc(s.notes || '');
    return '<tr><td>' + link + '</td><td>' + dirBadge + '</td><td>' + esc(s.agent) +
           '</td><td class="num">' + scoreCell + '</td><td><span class="stars">' + starStr(s.star) + '</span></td>' +
           '<td class="notes-cell">' + detail + '</td><td class="notes-cell">' + notes + '</td></tr>';
  }).join('');
}

function updateChart(id, labels, datasetUpdates) {
  const chart = charts[id];
  if (!chart) return;
  chart.data.labels = labels;
  datasetUpdates.forEach(function(upd, i) {
    if (!chart.data.datasets[i]) return;
    Object.assign(chart.data.datasets[i], upd);
  });
  chart.update();
}

init();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Generate a CTM AI Assessment dashboard for any account")
    parser.add_argument("--config", default="config.json", help="Optional JSON config path")
    parser.add_argument("--account-id", type=int, help="CTM account ID")
    parser.add_argument("--auth-token", help="CTM Basic auth token, with or without 'Basic ' prefix")
    parser.add_argument("--auth-env-key", help="Specific env.txt key to read for CTM auth")
    parser.add_argument("--env-file", default=str(DEFAULT_ENV_FILE), help="Path to env.txt")
    parser.add_argument("--output", help="Output HTML file")
    parser.add_argument("--workers", type=int, help="Concurrent day fetches")
    parser.add_argument("--max-pages", type=int, help="Cap pages per day (useful for testing)")
    parser.add_argument("--days-back", type=int, help="How many days of history to pull (default 30)")
    parser.add_argument("--start-date", help="Override start date, YYYY-MM-DD")
    parser.add_argument("--end-date", help="Override end date, YYYY-MM-DD")
    parser.add_argument("--customer-name", help="Display name used in the dashboard title")
    args = parser.parse_args()

    cfg = load_config(args.config)
    if not cfg and args.config == "config.json":
        sys.exit(
            "No config.json found. Copy config.example.json to config.json and edit it, "
            "or pass --account-id / --auth-env-key on the command line."
        )

    auth_header, source = resolve_auth_header(args, cfg)
    account_id = args.account_id or cfg.get("account_id")
    if not account_id:
        sys.exit("Missing account_id. Set it in config.json or pass --account-id.")
    workers = args.workers if args.workers is not None else int(cfg.get("workers") or 6)
    customer_name = args.customer_name or cfg.get("customer_name") or f"Account {account_id}"
    output_file = Path(args.output or cfg.get("output_file") or f"outputs/account_{account_id}_ai_assessment_report.html")

    start_date, end_date = resolve_date_range(args, cfg)
    fields = resolve_fields(cfg)
    segments = resolve_segments(cfg)

    if not any(fields.values()):
        sys.exit(
            "No AI fields configured. Add at least one of 'score', 'star', 'categories', "
            "'highlights', 'concerns', 'notes' under \"fields\" in config.json."
        )

    print(f"\n=== CTM AI Assessment Dashboard: {customer_name} ===")
    print(f"Account: {account_id}")
    print(f"Date range: {start_date} -> {end_date}")
    print(f"Credential source: {source}")
    print(f"Workers: {workers}")
    print(f"Page size: {PAGE_SIZE}")
    print(f"Output: {output_file}")
    print(f"Fields configured:")
    for role in SUPPORTED_ROLES:
        spec = fields.get(role)
        if spec:
            print(f"  {role}: {spec['key']} ({spec['label']})")
    if segments:
        print(f"Segments: {', '.join(s['short'] + '=' + ','.join(s['tags']) for s in segments)}")
    else:
        print("Segments: (none configured - only All/Other in toggle)")
    print()

    client = CTMClient(auth_header)
    fetch_meta = fetch_all_pages(client, account_id, start_date, end_date, args.max_pages, workers)
    activities_all = fetch_meta["calls"]
    print(f"\nTotal rows fetched: {len(activities_all):,}")

    ai_activities = [a for a in activities_all if has_ai_assessment(a, fields)]
    pct = safe_pct(len(ai_activities), len(activities_all))
    print(f"AI-assessed activities: {len(ai_activities):,} ({pct}% of fetched)")

    slices = build_slices(ai_activities, segments, fields)

    print("\nBy segment:")
    for sid, s in slices.items():
        if sid == "all":
            continue
        label = next((seg["short"] for seg in segments if seg["id"] == sid), sid)
        print(f"  {label}: {s['total']:,} (avg_score={s['avg_score']}, avg_star={s['avg_star']})")

    print("\nBy direction (overall):")
    for kind, count in slices["all"]["by_kind"].most_common():
        print(f"  {kind}: {count:,}")

    print("\nRendering HTML...")
    output_file.parent.mkdir(parents=True, exist_ok=True)
    doc = render_html(
        customer_name, account_id, start_date, end_date,
        segments, slices, fetch_meta, fields, len(ai_activities), output_file,
    )
    output_file.write_text(doc, encoding="utf-8")
    print(f"\nReport written to: {output_file}")


if __name__ == "__main__":
    main()
