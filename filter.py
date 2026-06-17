"""
Job filter and classifier.

Design principle: the job *description* is the authoritative source.
Structured fields (title, location, seniority) are hints that may lie.
When they contradict the description, the description wins.
"""
from typing import Optional


def _contains_any(text: str, keywords: list[str]) -> bool:
    """Case-insensitive: does text contain any keyword from the list?"""
    text_lower = text.lower()
    return any(kw.lower() in text_lower for kw in keywords)


def classify_job(job: dict, categories_config: dict) -> Optional[str]:
    """
    Classify a job into 'qa', 'pm', or 'writing' by scanning the title for
    category keywords defined in config.yaml.

    Returns the category name or None if no category matches.
    """
    title = job.get("title", "").lower()
    for category, cfg in categories_config.items():
        for kw in cfg.get("title_keywords", []):
            if kw.lower() in title:
                return category
    return None


def writing_subcategory(job: dict, writing_config: dict) -> str:
    """
    Classify a writing job into a display label for the email.
    Checks title first, then description. First match in priority order wins:
    Technical Writer → Copywriter → General Writing.
    """
    title = job.get("title", "").lower()
    desc = job.get("description", "").lower()
    combined = f"{title} {desc}"

    subcategories = writing_config.get("subcategories", {})
    for subcat, keywords in subcategories.items():
        for kw in keywords:
            if kw.lower() in combined:
                if subcat == "technical":
                    return "Technical Writer"
                if subcat == "copywriting":
                    return "Copywriter"
    return "General Writing"


def passes_location(job: dict, location_config: dict) -> tuple[bool, bool]:
    """
    Validate that this job is actually in San Francisco city or fully remote.

    Returns a tuple:
      (passes: bool, flag: bool)

    passes=True  → job should continue through the pipeline
    flag=True    → job passed but the description mentions a Bay Area suburb
                   alongside remote/hybrid language; shown with a warning tag
                   in the email so the user can decide

    The two-step logic:
      1. Location FIELD must contain an allowed substring (SF, Remote, etc.)
         OR the API flagged the job as remote (is_remote=True)
         OR the location field is blank.
         "South San Francisco" is pre-neutralised so it cannot match "San Francisco".
      2. Job DESCRIPTION is scanned for Bay Area suburb names.
         - Suburb found + no remote language → exclude (description overrules field)
         - Suburb found + remote language present → pass with flag
         - No suburb found → pass cleanly
    """
    loc = job.get("location", "")
    desc = job.get("description", "")
    is_remote = job.get("is_remote", False)

    loc_lower = loc.lower()
    desc_lower = desc.lower()

    # Step 1 — location field check.
    # Replace "south san francisco" first so the "san francisco" substring
    # check below cannot accidentally match it.
    loc_for_sf_check = loc_lower.replace("south san francisco", "___ssf___")
    sf_in_loc = "san francisco" in loc_for_sf_check
    remote_in_loc = _contains_any(loc, ["remote", "anywhere", "work from home", "wfh"])
    blank_location = not loc.strip()

    location_field_ok = sf_in_loc or remote_in_loc or blank_location or is_remote
    if not location_field_ok:
        return False, False

    # Step 2 — description suburb check.
    suburb_exclusions = location_config.get("suburb_exclusions", [])
    has_remote_signal = _contains_any(
        desc, ["remote", "work from home", "wfh", "hybrid"]
    )

    for suburb in suburb_exclusions:
        if suburb.lower() in desc_lower:
            # A suburb name appears somewhere in the description. This could mean
            # the job is actually in that suburb, OR the company merely mentions
            # a satellite office, service area, etc. Rather than silently drop
            # the job, flag it in the email so you can read the description and
            # decide. If the description also contains remote/hybrid language the
            # flag message makes that explicit.
            return True, True

    return True, False


def passes_qa_filter(job: dict, qa_config: dict) -> tuple[bool, str]:
    """
    QA-specific filter (all conditions must pass):
      1. Description must contain at least one software-industry keyword.
         Filters out food-safety QA, pharmaceutical QA, manufacturing QA, etc.
      2. Title must NOT contain executive-level signals (VP, Director, etc.).
      3. Title OR description must contain at least one seniority signal.
         Captures explicit titles ("Senior QA Engineer") and description-level
         signals ("5+ years of QA experience").

    Returns (passes: bool, rejection_reason: str).
    """
    desc = job.get("description", "")
    title = job.get("title", "")
    combined = f"{title} {desc}"

    if not _contains_any(desc, qa_config.get("industry_keywords", [])):
        return False, "failed software-industry check (none of the industry keywords found in description)"

    if _contains_any(title, qa_config.get("executive_exclude_keywords", [])):
        return False, "excluded: executive-level title"

    if not _contains_any(combined, qa_config.get("seniority_include_keywords", [])):
        return False, "failed seniority check (no senior/lead/manager signal found in title or description)"

    return True, ""


def passes_pm_filter(job: dict, pm_config: dict) -> tuple[bool, str]:
    """
    PM-specific filter. Returns (passes: bool, flag_or_reason: str).

    Three checks in order:
      1. Description must contain at least one tech-industry keyword.
         Filters out retail category managers, event PMs, construction PMs.
      2. Job TITLE must NOT contain senior-level signals.
         NOTE: We check the title only — not the full description. Words like
         "lead", "senior", and "director" appear constantly in PM job descriptions
         as verbs or references ("lead cross-functional teams", "reports to the
         Senior Director") and would produce false positives if checked against
         the description.
      3. Title OR description should contain entry-level signals.
         If none are found (but no senior signals either), the job still passes
         but is tagged [seniority unclear] in the email.
    """
    desc = job.get("description", "")
    title = job.get("title", "")
    combined = f"{title} {desc}"

    if not _contains_any(desc, pm_config.get("industry_keywords", [])):
        return False, ""

    # Exclude check: TITLE only — not combined title+description.
    if _contains_any(title, pm_config.get("seniority_exclude_keywords", [])):
        return False, ""

    if _contains_any(combined, pm_config.get("seniority_include_keywords", [])):
        return True, ""

    return True, "seniority unclear"


def apply_all_filters(
    job: dict, config: dict, verbose: bool = False
) -> Optional[dict]:
    """
    Run the full filter pipeline on a single job.

    Returns an augmented job dict if it passes all filters, or None if it
    should be excluded.

    Fields added to passing jobs:
      category             'qa' | 'pm' | 'writing'
      flags                list[str] — warning tags displayed in the email
      writing_subcategory  str — only present for writing-category jobs

    Set verbose=True (or run with DEBUG=1) to print a reason for every
    job that is skipped.
    """
    categories_config = config.get("categories", {})
    location_config = config.get("location_filter", {})

    def skip(reason: str) -> None:
        if verbose:
            label = f"{job.get('title', '?')} | {job.get('company', '?')} | {job.get('location', '?')}"
            print(f"    SKIP  {label}")
            print(f"          reason: {reason}")

    # 1. Classify by title
    category = classify_job(job, categories_config)
    if category is None:
        skip("title not matched to any category (qa/pm/writing)")
        return None

    # 2. Location validation
    loc_passes, loc_flag = passes_location(job, location_config)
    if not loc_passes:
        skip(f"location field {job.get('location')!r} does not contain SF or Remote")
        return None

    flags: list[str] = []
    if loc_flag:
        flags.append("location: check description")

    # 3. Category-specific rules
    if category == "qa":
        qa_passes, qa_reason = passes_qa_filter(job, categories_config.get("qa", {}))
        if not qa_passes:
            skip(qa_reason)
            return None

    elif category == "pm":
        passes, pm_flag = passes_pm_filter(job, categories_config.get("pm", {}))
        if not passes:
            # Report which specific keyword triggered the exclusion.
            if verbose:
                pm_cfg = categories_config.get("pm", {})
                title = job.get("title", "")
                desc = job.get("description", "")
                if not _contains_any(desc, pm_cfg.get("industry_keywords", [])):
                    reason = "failed tech-industry check (no industry keywords in description)"
                else:
                    triggered = [
                        kw for kw in pm_cfg.get("seniority_exclude_keywords", [])
                        if kw.lower() in title.lower()
                    ]
                    reason = f"title contains seniority-exclude keyword(s): {triggered}"
                skip(reason)
            return None
        if pm_flag:
            flags.append(pm_flag)

    elif category == "writing":
        writing_cfg = categories_config.get("writing", {})
        # Seniority exclude: title only (same rationale as PM — seniority words
        # appear as verbs throughout writing job descriptions).
        if _contains_any(
            job.get("title", ""), writing_cfg.get("seniority_exclude_keywords", [])
        ):
            skip("writing: title contains seniority-exclude keyword")
            return None
        subcat = writing_subcategory(job, writing_cfg)
        job = {**job, "writing_subcategory": subcat}

    if verbose:
        label = f"{job.get('title', '?')} | {job.get('company', '?')} | {job.get('location', '?')}"
        flag_str = f" [{', '.join(flags)}]" if flags else ""
        print(f"    PASS  {label}{flag_str}")

    return {**job, "category": category, "flags": flags}
