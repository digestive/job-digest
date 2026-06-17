"""
Email builder and sender.

Renders an HTML digest email from the filtered job list and sends it via
Gmail SMTP using an app password (no OAuth required).

Credentials are read exclusively from environment variables — never from
config or source files:
  GMAIL_ADDRESS       your Gmail address used as the sender
  GMAIL_APP_PASSWORD  the 16-character app password from Google Account settings
  RECIPIENT_EMAIL     delivery address (falls back to GMAIL_ADDRESS if unset)
"""
import os
import smtplib
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from jinja2 import BaseLoader, Environment

# ---------------------------------------------------------------------------
# HTML email template
# ---------------------------------------------------------------------------
_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body  { font-family: Arial, sans-serif; font-size: 14px; color: #222;
          background: #f5f5f5; margin: 0; padding: 16px; }
  .wrap { max-width: 680px; margin: 0 auto; background: #fff;
          border-radius: 6px; padding: 28px 32px; }
  h1    { font-size: 20px; margin: 0 0 4px; }
  .sub  { color: #555; font-size: 13px; margin: 0 0 24px; }
  h2    { font-size: 15px; background: #f0f0f0; border-left: 4px solid #555;
          padding: 7px 12px; margin: 28px 0 12px; }
  .job  { border-bottom: 1px solid #eee; padding: 12px 0; }
  .job:last-child { border-bottom: none; }
  .title       { font-size: 15px; font-weight: bold; margin-bottom: 3px; }
  .title a     { color: #1a0dab; text-decoration: none; }
  .title a:hover { text-decoration: underline; }
  .meta        { color: #555; font-size: 12px; margin-bottom: 5px; }
  .excerpt     { color: #444; font-size: 13px; }
  .tag         { display: inline-block; font-size: 11px; border-radius: 3px;
                 padding: 1px 6px; margin-left: 5px; border: 1px solid; }
  .tag-warn    { background: #fff3cd; color: #856404; border-color: #ffc107; }
  .tag-tech    { background: #d1ecf1; color: #0c5460; border-color: #bee5eb; }
  .tag-copy    { background: #d4edda; color: #155724; border-color: #c3e6cb; }
  .tag-general { background: #e2e3e5; color: #383d41; border-color: #d6d8db; }
  .empty       { color: #888; font-style: italic; font-size: 13px; }
  .footer      { margin-top: 28px; font-size: 11px; color: #aaa;
                 border-top: 1px solid #eee; padding-top: 12px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>{{ subject_prefix }}</h1>
  <p class="sub">{{ date_label }} &nbsp;·&nbsp; {{ total }} new job{{ 's' if total != 1 else '' }} found</p>

  {% for section_key, section_label, section_jobs in sections %}
  <h2>{{ section_label }} &nbsp;({{ section_jobs | length }})</h2>
  {% if section_jobs %}
    {% for job in section_jobs %}
    <div class="job">
      <div class="title">
        <a href="{{ job.url | e }}">{{ job.title | e }}</a>
        {%- if job.category == 'writing' %}
          {%- set sc = job.get('writing_subcategory', 'General Writing') %}
          {%- if 'Technical' in sc %}
            <span class="tag tag-tech">{{ sc }}</span>
          {%- elif 'Copy' in sc %}
            <span class="tag tag-copy">{{ sc }}</span>
          {%- else %}
            <span class="tag tag-general">{{ sc }}</span>
          {%- endif %}
        {%- endif %}
        {%- for flag in job.flags %}
          <span class="tag tag-warn">{{ flag }}</span>
        {%- endfor %}
      </div>
      <div class="meta">
        {{ job.company | e }}
        {%- if job.location %} &nbsp;·&nbsp; {{ job.location | e }}{% endif %}
        {%- if job.salary %}  &nbsp;·&nbsp; {{ job.salary | e }}{% endif %}
        {%- if job.posted_at %} &nbsp;·&nbsp; {{ job.posted_at[:10] }}{% endif %}
      </div>
      {% if job.excerpt %}
      <div class="excerpt">{{ job.excerpt | e }}</div>
      {% endif %}
    </div>
    {% endfor %}
  {% else %}
    <p class="empty">No new jobs in this category today.</p>
  {% endif %}
  {% endfor %}

  <div class="footer">Generated {{ generated_at }} &nbsp;·&nbsp; Job Digest</div>
</div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _excerpt(description: str, max_len: int = 220) -> str:
    """
    Pull a readable snippet from a job description.
    Tries to find a clean sentence boundary; falls back to a hard truncation.
    """
    text = " ".join(description.split())  # collapse whitespace
    for sep in (". ", "! ", "? "):
        idx = text.find(sep)
        if 40 < idx < max_len:
            return text[: idx + 1]
    if len(text) <= max_len:
        return text
    return text[:max_len].rstrip() + "…"


def _sort_key(job: dict) -> str:
    return job.get("posted_at") or ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_html(jobs: list[dict], subject_prefix: str) -> str:
    """Render the HTML email body for the given list of filtered jobs."""
    date_label = datetime.now(timezone.utc).strftime("%B %d, %Y")
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    enriched = [{**j, "excerpt": _excerpt(j.get("description", ""))} for j in jobs]

    qa_jobs = sorted(
        [j for j in enriched if j["category"] == "qa"], key=_sort_key, reverse=True
    )
    pm_jobs = sorted(
        [j for j in enriched if j["category"] == "pm"], key=_sort_key, reverse=True
    )
    writing_jobs = sorted(
        [j for j in enriched if j["category"] == "writing"],
        key=_sort_key,
        reverse=True,
    )

    sections = [
        ("qa", "Quality Assurance", qa_jobs),
        ("pm", "Product Manager", pm_jobs),
        ("writing", "Writing", writing_jobs),
    ]

    env = Environment(loader=BaseLoader())
    # Expose dict.get() inside Jinja2 templates
    env.globals["dict"] = dict
    tmpl = env.from_string(_TEMPLATE)
    return tmpl.render(
        subject_prefix=subject_prefix,
        date_label=date_label,
        total=len(enriched),
        sections=sections,
        generated_at=generated_at,
    )


def send_email(html_body: str, subject: str) -> None:
    """
    Send the digest email via Gmail SMTP on port 465 (implicit TLS).

    Reads credentials from environment variables:
      GMAIL_ADDRESS, GMAIL_APP_PASSWORD, RECIPIENT_EMAIL
    """
    sender = os.environ["GMAIL_ADDRESS"]
    app_pw = os.environ["GMAIL_APP_PASSWORD"]
    recipient = os.environ.get("RECIPIENT_EMAIL") or sender

    msg = MIMEMultipart("alternative")
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = subject
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(sender, app_pw)
        smtp.send_message(msg)
