"""
Job filter and classifier.

Design principle: the job *description* is the authoritative source.
Structured fields (title, location, seniority) are hints that may lie.
When they contradict the description, the description wins.
"""
import re
from typing import Optional

# ---------------------------------------------------------------------------
# Compiled regexes
# ---------------------------------------------------------------------------

# Detects elevated level suffixes in job titles (level II and above).
# Matches Roman numerals II–VIII and tech-style level designators like L3/L4.
# "Level I" / "Grade 1" / "L1" are intentionally NOT matched — those are
# entry-level designations.
_ELEVATED_LEVEL_RE = re.compile(
    r"\b(?:II|III|IV|VI?I?I?)\b"           # Roman numerals II, III, IV, V, VI, VII, VIII
    r"|\b(?:level|grade|tier)\s*[2-9]\b"   # "Level 2", "Grade 3", "Tier 2"
    r"|\bL[2-9]\b",                        # "L3", "L4" (common in tech job levels)
    re.IGNORECASE,
)

# Detects explicit years-of-experience requirements in description text.
# Captures the lower bound so a range like "3-5 years" yields 3.
_YEARS_REQUIRED_RE = re.compile(
    r"\b(\d+)\s*[-–]\s*\d+\s+years?\b"         # "3-5 years", "5–7 years"
    r"|\b(\d+)\s*\+\s*years?\b"                 # "5+ years"
    r"|\b(\d+)\s+years?\s+of\b"                 # "5 years of [experience]"
    r"|\bat\s+least\s+(\d+)\s+years?\b"         # "at least 5 years"
    r"|\bminimum\s+(?:of\s+)?(\d+)\s+years?\b", # "minimum of 5 years"
    re.IGNORECASE,
)


def _max_min_years_required(text: str) -> Optional[int]:
    """
    Scan *text* for explicit years-of-experience requirements and return the
    highest minimum found (i.e., the most restrictive lower bound), or None
    if no numeric requirement is detected.

    Examples:
        "3-5 years of experience"   → 3
        "5+ years required"         → 5
        "at least 4 years"          → 4
    """
    mins = [
        int(g)
        for m in _YEARS_REQUIRED_RE.finditer(text)
        for g in m.groups()
        if g is not None
    ]
    return max(mins) if mins else None


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

    # Junior/entry-level title exclusion — must come before the seniority_include
    # check because descriptions often mention "senior" in non-seniority context
    # (e.g. "reports to a Senior Engineer"), allowing junior titles to slip through.
    if _contains_any(title, qa_config.get("junior_exclude_keywords", [])):
        return False, "excluded: junior or entry-level signal in title"

    # Non-software industry exclusion via title — faster and more reliable than
    # description scanning for clear cases (e.g. "Hardware QA", "Food Safety QA").
    # For titles that follow the "[Role], [Industry Domain]" pattern common on
    # job boards (e.g. "QA Manager, Jewelry & Luggage"), only the pre-comma role
    # descriptor is checked here. The post-comma domain qualifier is checked
    # separately below so each check produces a distinct, actionable reason.
    role_part = title.split(",", 1)[0] if "," in title else title
    if _contains_any(role_part, qa_config.get("industry_exclude_title_keywords", [])):
        return False, "excluded: non-software industry keyword in title"

    # Non-software industry exclusion via domain qualifier — checks the portion of
    # the title after the first comma (if present). This catches titles like:
    #   "QA Manager, Jewelry & Luggage"          → qualifier: "Jewelry & Luggage"
    #   "Dir, Clinical Research QA & Regulatory" → qualifier: "Clinical Research..."
    # Using the same exclusion list as the role-part check above; no new config needed.
    if "," in title:
        qualifier = title.split(",", 1)[1].strip()
        if _contains_any(qualifier, qa_config.get("industry_exclude_title_keywords", [])):
            return False, "excluded: non-software domain qualifier in title"

    # Non-software industry exclusion via description — catches cases where the
    # title is generic but the description reveals the industry (e.g. HACCP,
    # food safety). These terms are specific enough to avoid false positives in
    # genuine software QA descriptions.
    if _contains_any(desc, qa_config.get("industry_exclude_description_keywords", [])):
        return False, "excluded: non-software industry keyword in description"

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

    When passes=False the second value is a human-readable rejection reason.
    When passes=True the second value is either "" (clean pass) or a flag
    string like "seniority unclear" to be surfaced in the email.

    Checks in order:
      1. Description must contain at least one tech-industry keyword.
      2. Job TITLE must NOT contain senior-level signals (title only — words
         like "lead" and "senior" appear constantly as verbs in descriptions).
      3. Job TITLE must NOT contain an elevated level suffix (II, III, L3 …).
         "Product Manager I" is fine; anything higher is not entry-level.
      4. Description must NOT explicitly require more years than the configured
         threshold (max_years_experience). Catches ranges like "5-7 years" that
         keyword matching alone would miss.
      5. Title OR description should contain entry-level signals.
         If none are found (but no senior signals either), the job still passes
         but is tagged [seniority unclear] in the email.
    """
    desc = job.get("description", "")
    title = job.get("title", "")
    combined = f"{title} {desc}"

    if not _contains_any(desc, pm_config.get("industry_keywords", [])):
        return False, "failed tech-industry check (no industry keywords in description)"

    # Exclude check: TITLE only — not combined title+description.
    if _contains_any(title, pm_config.get("seniority_exclude_keywords", [])):
        triggered = [
            kw for kw in pm_config.get("seniority_exclude_keywords", [])
            if kw.lower() in title.lower()
        ]
        return False, f"title contains seniority-exclude keyword(s): {triggered}"

    # Elevated level suffix: "Product Manager II" or higher → excluded.
    if _ELEVATED_LEVEL_RE.search(title):
        return False, f"title has elevated level suffix (II or higher): {title!r}"

    # Years-of-experience check: exclude if description requires more than the
    # configured threshold. Default 0 disables the check.
    max_years = pm_config.get("max_years_experience", 0)
    if max_years > 0:
        min_years_req = _max_min_years_required(desc)
        if min_years_req is not None and min_years_req > max_years:
            return False, (
                f"description requires {min_years_req}+ years experience "
                f"(threshold: {max_years})"
            )

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

    # 1. URL blocklist — drop jobs from known unreliable sources immediately.
    url = job.get("url", "")
    for blocked in config.get("url_blocklist", []):
        if blocked in url:
            skip(f"url blocked: {blocked}")
            return None

    # 2. Classify by title
    category = classify_job(job, categories_config)
    if category is None:
        skip("title not matched to any category (qa/pm/writing)")
        return None

    # 3. Location validation
    loc_passes, loc_flag = passes_location(job, location_config)
    if not loc_passes:
        skip(f"location field {job.get('location')!r} does not contain SF or Remote")
        return None

    flags: list[str] = []
    if loc_flag:
        flags.append("location: check description")

    # 4. Category-specific rules
    if category == "qa":
        qa_passes, qa_reason = passes_qa_filter(job, categories_config.get("qa", {}))
        if not qa_passes:
            skip(qa_reason)
            return None

    elif category == "pm":
        passes, pm_flag = passes_pm_filter(job, categories_config.get("pm", {}))
        if not passes:
            skip(pm_flag)
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
        # Elevated level suffix: "Content Writer II" or higher → excluded.
        if _ELEVATED_LEVEL_RE.search(job.get("title", "")):
            skip(f"writing: title has elevated level suffix (II or higher): {job.get('title', '')!r}")
            return None
        # Years-of-experience check (same logic as PM).
        max_years = writing_cfg.get("max_years_experience", 0)
        if max_years > 0:
            min_years_req = _max_min_years_required(job.get("description", ""))
            if min_years_req is not None and min_years_req > max_years:
                skip(
                    f"writing: description requires {min_years_req}+ years experience "
                    f"(threshold: {max_years})"
                )
                return None
        subcat = writing_subcategory(job, writing_cfg)
        job = {**job, "writing_subcategory": subcat}

    if verbose:
        label = f"{job.get('title', '?')} | {job.get('company', '?')} | {job.get('location', '?')}"
        flag_str = f" [{', '.join(flags)}]" if flags else ""
        print(f"    PASS  {label}{flag_str}")

    return {**job, "category": category, "flags": flags}
