"""
JSearch API client.

Fetches job postings from the JSearch API (RapidAPI) and returns them as
normalized dicts with consistent field names used throughout the pipeline.

JSearch aggregates from LinkedIn, Indeed, Glassdoor, and ZipRecruiter.
API docs: https://rapidapi.com/letscrape-6bRBa3QguO5/api/jsearch
"""
import requests
from typing import Any

JSEARCH_BASE = "https://jsearch.p.rapidapi.com/search"
JSEARCH_HOST = "jsearch.p.rapidapi.com"


def _normalize(raw: dict) -> dict:
    """Convert a raw JSearch job record into a flat normalized dict."""
    city = raw.get("job_city") or ""
    state = raw.get("job_state") or ""
    country = raw.get("job_country") or ""
    is_remote = raw.get("job_is_remote", False)

    if is_remote and not city:
        location = "Remote"
    else:
        parts = [p for p in [city, state] if p]
        location = ", ".join(parts) if parts else country

    min_sal = raw.get("job_min_salary")
    max_sal = raw.get("job_max_salary")
    currency = raw.get("job_salary_currency") or "USD"
    period = raw.get("job_salary_period") or ""

    if min_sal and max_sal:
        salary = f"{currency} {int(min_sal):,}–{int(max_sal):,} / {period}"
    elif min_sal:
        salary = f"{currency} {int(min_sal):,}+ / {period}"
    elif max_sal:
        salary = f"Up to {currency} {int(max_sal):,} / {period}"
    else:
        salary = None

    return {
        "id": raw.get("job_id", ""),
        "title": raw.get("job_title", ""),
        "company": raw.get("employer_name", ""),
        "location": location,
        "is_remote": is_remote,
        "description": raw.get("job_description", ""),
        "url": raw.get("job_apply_link") or raw.get("job_google_link", ""),
        "posted_at": raw.get("job_posted_at_datetime_utc", ""),
        "salary": salary,
    }


def fetch_jobs(
    search_config: dict,
    api_key: str,
    date_posted: str = "today",
    num_pages: int = 1,
) -> list[dict]:
    """
    Fetch jobs for one search entry from config.yaml.

    Args:
        search_config: A single entry from config['searches'].
        api_key:       RapidAPI key for JSearch.
        date_posted:   Recency filter — 'today', '3days', 'week', or 'month'.
        num_pages:     Pages of results to fetch (1 page ≈ 10 results).

    Returns:
        List of normalized job dicts.

    Raises:
        requests.HTTPError: On non-2xx response.
        ValueError: If the API returns a non-OK status.
    """
    headers: dict[str, str] = {
        "X-RapidAPI-Key": api_key,
        "X-RapidAPI-Host": JSEARCH_HOST,
    }

    query = search_config["query"]
    location = search_config.get("location", "")
    remote_only = search_config.get("remote_only", False)

    if remote_only:
        full_query = f"{query} remote"
    else:
        full_query = f"{query} {location}".strip()

    params: dict[str, Any] = {
        "query": full_query,
        "page": "1",
        "num_pages": str(num_pages),
        "date_posted": date_posted,
        "employment_types": "FULLTIME",
    }
    if remote_only:
        params["remote_jobs_only"] = "true"

    resp = requests.get(JSEARCH_BASE, headers=headers, params=params, timeout=15)
    resp.raise_for_status()

    data = resp.json()
    if data.get("status") != "OK":
        raise ValueError(f"JSearch returned non-OK status: {data.get('status')}")

    return [_normalize(job) for job in data.get("data", [])]
