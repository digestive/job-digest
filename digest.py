"""
Daily job digest — main entry point.

Usage:
  python digest.py

Required environment variables:
  RAPIDAPI_KEY         Your JSearch API key from RapidAPI
  GMAIL_ADDRESS        Gmail address used as the sender
  GMAIL_APP_PASSWORD   16-character Gmail app password

Optional environment variable:
  RECIPIENT_EMAIL      Delivery address (defaults to GMAIL_ADDRESS)

For local development, create a .env file in this directory with the above
variables. The .env file is listed in .gitignore and will never be committed.
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml
from dotenv import load_dotenv

from emailer import build_html, send_email
from fetcher import fetch_jobs
from filter import apply_all_filters

STATE_FILE = Path(__file__).parent / "seen_jobs.json"
CONFIG_FILE = Path(__file__).parent / "config.yaml"


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def load_seen_ids() -> set[str]:
    """Load the set of already-sent job IDs from disk."""
    if STATE_FILE.exists():
        with open(STATE_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_seen_ids(seen: set[str]) -> None:
    """Persist the updated set of seen job IDs to disk."""
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f, indent=2)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    load_dotenv()

    # Validate required env vars before doing any work.
    missing = [v for v in ("RAPIDAPI_KEY", "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD")
               if not os.environ.get(v)]
    if missing:
        print(f"ERROR: Missing required environment variable(s): {', '.join(missing)}",
              file=sys.stderr)
        sys.exit(1)

    api_key = os.environ["RAPIDAPI_KEY"]

    with open(CONFIG_FILE, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    seen_ids = load_seen_ids()
    api_cfg = config.get("api", {})
    email_cfg = config.get("email", {})
    date_posted = api_cfg.get("date_posted", "today")
    num_pages = api_cfg.get("num_pages", 1)

    # ------------------------------------------------------------------
    # Fetch
    # ------------------------------------------------------------------
    all_raw: list[dict] = []
    for search in config.get("searches", []):
        label = f"{search['query']!r} (remote={search.get('remote_only', False)})"
        print(f"Fetching {label} …")
        try:
            jobs = fetch_jobs(search, api_key, date_posted=date_posted,
                              num_pages=num_pages)
            print(f"  {len(jobs)} result(s)")
            all_raw.extend(jobs)
        except Exception as exc:
            print(f"  FAILED: {exc}", file=sys.stderr)

    # Deduplicate across searches (same job can surface in multiple queries).
    seen_in_batch: set[str] = set()
    unique_raw: list[dict] = []
    for job in all_raw:
        if job["id"] not in seen_in_batch:
            seen_in_batch.add(job["id"])
            unique_raw.append(job)

    print(f"\n{len(unique_raw)} unique job(s) fetched across all searches.")

    # ------------------------------------------------------------------
    # Filter
    # ------------------------------------------------------------------
    # Set DEBUG=1 in your environment (or .env) to print a pass/fail reason
    # for every job — useful for diagnosing why results look wrong.
    verbose = os.environ.get("DEBUG", "").lower() in ("1", "true", "yes")
    if verbose:
        print("\n--- DEBUG: filter trace ---")

    filtered: list[dict] = []
    for job in unique_raw:
        result = apply_all_filters(job, config, verbose=verbose)
        if result is not None:
            filtered.append(result)

    if verbose:
        print("--- end filter trace ---\n")

    print(f"{len(filtered)} job(s) passed all filters.")

    # ------------------------------------------------------------------
    # Deduplicate against previously sent jobs
    # ------------------------------------------------------------------
    new_jobs = [j for j in filtered if j["id"] not in seen_ids]
    print(f"{len(new_jobs)} new job(s) not previously sent.")

    if not new_jobs and not email_cfg.get("send_if_empty", False):
        print("No new jobs and send_if_empty=false — skipping email.")
        return

    # ------------------------------------------------------------------
    # Build and send email
    # ------------------------------------------------------------------
    subject_prefix = email_cfg.get("subject_prefix", "Job Digest")
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    subject = f"{subject_prefix} — {today_str} ({len(new_jobs)} new)"

    html = build_html(new_jobs, subject_prefix)
    print(f"Sending: {subject!r}")
    send_email(html, subject)
    print("Email sent.")

    # ------------------------------------------------------------------
    # Persist updated seen IDs
    # ------------------------------------------------------------------
    seen_ids.update(j["id"] for j in new_jobs)
    save_seen_ids(seen_ids)
    print(f"State saved. Total seen jobs: {len(seen_ids)}")


if __name__ == "__main__":
    main()
