import html
import re
import unicodedata
from datetime import datetime
from typing import Dict, List, Optional


def render_ticket_html(ticket: Dict, comments: List[Dict]) -> str:
    title = f"Zendesk Ticket {ticket['id']}: {ticket.get('subject') or '(no subject)'}"
    sections = [
        _metadata_row("Ticket ID", ticket.get("id")),
        _metadata_row("Subject", ticket.get("subject")),
        _metadata_row("Status", ticket.get("status")),
        _metadata_row("Created At", ticket.get("created_at")),
        _metadata_row("Updated At", ticket.get("updated_at")),
        _metadata_row("Requester ID", ticket.get("requester_id")),
        _metadata_row("Assignee ID", ticket.get("assignee_id")),
        _metadata_row("Organization ID", ticket.get("organization_id")),
    ]

    description = _format_text_block(ticket.get("description"))
    sorted_comments = sorted(comments, key=lambda comment: comment.get("created_at") or "")
    comments_html = "\n".join(_render_comment(comment) for comment in sorted_comments) or "<p>No comments found.</p>"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    body {{ font-family: Arial, sans-serif; line-height: 1.5; margin: 2rem; color: #1f2937; }}
    h1, h2 {{ color: #111827; }}
    dl {{ display: grid; grid-template-columns: 180px 1fr; gap: 0.5rem 1rem; }}
    dt {{ font-weight: 700; }}
    dd {{ margin: 0; }}
    .block {{ border: 1px solid #d1d5db; border-radius: 8px; padding: 1rem; margin-bottom: 1rem; background: #f9fafb; }}
    .meta {{ color: #4b5563; font-size: 0.95rem; margin-bottom: 0.5rem; }}
    pre {{ white-space: pre-wrap; word-break: break-word; margin: 0; font-family: inherit; }}
  </style>
</head>
<body>
  <h1>{html.escape(title)}</h1>
  <h2>Ticket Metadata</h2>
  <dl>
    {''.join(sections)}
  </dl>
  <h2>Description</h2>
  <div class="block">{description}</div>
  <h2>Comment History</h2>
  {comments_html}
</body>
</html>
"""


def build_ticket_filename(ticket: Dict, suffix: str = "") -> str:
    subject = ticket.get("subject") or ""
    slug = _slugify(subject)
    if not slug:
        slug = f"ticket-{ticket['id']}"
    return f"{slug}{suffix}.html"


def _metadata_row(label: str, value: Optional[object]) -> str:
    return f"<dt>{html.escape(label)}</dt><dd>{html.escape(_stringify(value))}</dd>"


def _render_comment(comment: Dict) -> str:
    created_at = _format_datetime(comment.get("created_at"))
    author_id = _stringify(comment.get("author_id"))
    public = _stringify(comment.get("public"))
    body = _format_text_block(comment.get("html_body") or comment.get("body"))
    return f"""
<div class="block">
  <div class="meta">Created: {html.escape(created_at)} | Author ID: {html.escape(author_id)} | Public: {html.escape(public)}</div>
  {body}
</div>
"""


def _format_text_block(value: Optional[object]) -> str:
    if value is None or value == "":
        return "<p></p>"

    text = str(value)
    if "<" in text and ">" in text:
        return text
    return f"<pre>{html.escape(text)}</pre>"


def _format_datetime(value: Optional[object]) -> str:
    if not value:
        return ""
    try:
        normalized = str(value).replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).isoformat()
    except ValueError:
        return str(value)


def _stringify(value: Optional[object]) -> str:
    return "" if value is None else str(value)


def _slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^A-Za-z0-9]+", "-", ascii_value).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    return slug[:120] or ""
