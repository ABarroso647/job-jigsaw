#!/usr/bin/env python3
"""Poll Gmail for replies to a pending insights proposal, process them."""

import email
import imaplib
import json
import logging
import sys
from datetime import datetime, timezone
from email.header import decode_header
from pathlib import Path

import requests
import yaml

from config import get_settings
from email_utils import send_plain_email
from proposal import (
    apply_diff, append_history, build_history_entry,
    clear_proposal, load_proposal, parse_intent, save_proposal,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

PROFILE_PATH = Path("/data/profile.yaml")
JOBS_DB      = Path("/data/jobs.db")


def _decode_str(val) -> str:
    if not val:
        return ""
    parts = decode_header(val)
    result = []
    for chunk, enc in parts:
        if isinstance(chunk, bytes):
            result.append(chunk.decode(enc or "utf-8", errors="replace"))
        else:
            result.append(chunk)
    return "".join(result)


def _get_text_body(msg) -> str:
    """Extract plain-text body from an email.Message."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            return payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
    return ""


def _strip_quoted(text: str) -> str:
    """Remove quoted reply lines (lines starting with '>') and trailing whitespace."""
    lines = [l for l in text.splitlines() if not l.startswith(">")]
    return "\n".join(lines).strip()


def _fetch_reply(settings, proposal: dict) -> tuple[str, str] | None:
    """
    Search Gmail IMAP for an unread reply to the proposal's message_id.
    Returns (uid_str, body_text) or None.
    """
    target_id = proposal.get("message_id", "")
    if not target_id:
        return None

    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(settings.gmail_from, settings.gmail_app_password)
        mail.select("inbox")

        # Search for unseen messages referencing our message ID
        _, data = mail.search(None, f'(UNSEEN HEADER "In-Reply-To" "{target_id}")')
        uids = data[0].split()
        if not uids:
            # Also try References header (some clients use that)
            _, data = mail.search(None, f'(UNSEEN HEADER "References" "{target_id}")')
            uids = data[0].split()

        if not uids:
            mail.logout()
            return None

        uid = uids[-1]  # take most recent
        _, msg_data = mail.fetch(uid, "(RFC822)")
        raw = msg_data[0][1]
        msg = email.message_from_bytes(raw)
        body = _strip_quoted(_get_text_body(msg))

        # Mark as read
        mail.store(uid, "+FLAGS", "\\Seen")
        mail.logout()

        return uid.decode(), body

    except Exception as e:
        log.error("IMAP error: %s", e)
        return None


def _call_openrouter(settings, prompt: str) -> str:
    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {settings.openrouter_api_key}"},
        json={
            "model": settings.openrouter_model,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def _revise_proposal(settings, proposal: dict, user_reply: str) -> dict | None:
    """Ask the model to revise the proposal based on user feedback. Returns updated proposal dict."""
    conversation = proposal.get("conversation", [])
    conversation.append({"role": "user", "content": user_reply})

    history_str = "\n".join(
        f"{'AI' if m['role'] == 'assistant' else 'User'}: {m['content']}"
        for m in conversation
    )

    current_boost    = [i["keyword"] for i in proposal.get("boost_add", [])]
    current_penalize = [i["keyword"] for i in proposal.get("penalize_add", [])]
    current_terms    = proposal.get("terms_add", [])

    prompt = f"""You previously proposed profile changes for a job hunter. The user replied with feedback.
Revise your proposal accordingly.

CURRENT PROPOSAL:
- Boost keywords to add: {', '.join(current_boost) or 'none'}
- Penalize keywords to add: {', '.join(current_penalize) or 'none'}
- Search terms to add: {', '.join(current_terms) or 'none'}

CONVERSATION:
{history_str}

Return ONLY valid JSON with the revised proposal:
{{
  "boost_add": [{{"keyword": "...", "weight": 10}}, ...],
  "penalize_add": [{{"keyword": "...", "weight": -20}}, ...],
  "terms_add": ["...", ...],
  "summary": "1-2 sentence description of changes and why"
}}"""

    content = _call_openrouter(settings, prompt)

    # Strip markdown code fences if present
    if "```" in content:
        content = content.split("```")[1]
        if content.startswith("json"):
            content = content[4:]
        content = content.strip()

    try:
        result = json.loads(content)
    except Exception as e:
        log.warning("Revision JSON parse failed: %s | raw: %.200s", e, content)
        return None

    conversation.append({"role": "assistant", "content": json.dumps(result)})
    return {
        **proposal,
        "boost_add":    result.get("boost_add", []),
        "penalize_add": result.get("penalize_add", []),
        "terms_add":    result.get("terms_add", []),
        "summary":      result.get("summary", ""),
        "conversation": conversation,
    }


def _proposal_body(proposal: dict) -> str:
    boost    = [i["keyword"] for i in proposal.get("boost_add", [])]
    penalize = [i["keyword"] for i in proposal.get("penalize_add", [])]
    terms    = proposal.get("terms_add", [])
    lines = [proposal.get("summary", ""), ""]
    if boost:
        lines.append(f"Boost keywords to add: {', '.join(boost)}")
    if penalize:
        lines.append(f"Penalize keywords to add: {', '.join(penalize)}")
    if terms:
        lines.append(f"Search terms to add: {', '.join(terms)}")
    if not (boost or penalize or terms):
        lines.append("(No changes proposed)")
    lines += ["", "Reply APPROVE to apply, REJECT to discard, or tell me what to change."]
    return "\n".join(lines)


def main() -> None:
    proposal = load_proposal()
    if not proposal:
        log.info("No active proposal — nothing to do.")
        return

    log.info("Active proposal found (message_id=%s), checking for replies...", proposal.get("message_id"))
    settings = get_settings()

    result = _fetch_reply(settings, proposal)
    if not result:
        log.info("No unread replies yet.")
        return

    _, body = result
    log.info("Reply received: %.100s", body)

    intent = parse_intent(body)
    log.info("Parsed intent: %s", intent)

    if intent == "approve":
        with open(PROFILE_PATH) as f:
            profile = yaml.safe_load(f)
        applied = apply_diff(profile, proposal)
        with open(PROFILE_PATH, "w") as f:
            yaml.dump(profile, f, allow_unicode=True, sort_keys=False, default_flow_style=False)

        entry = build_history_entry(proposal, applied)
        append_history(entry)
        clear_proposal()

        counts = []
        if applied["boost"]:    counts.append(f"{len(applied['boost'])} boost keyword(s)")
        if applied["penalize"]: counts.append(f"{len(applied['penalize'])} penalize keyword(s)")
        if applied["terms"]:    counts.append(f"{len(applied['terms'])} search term(s)")
        change_str = ", ".join(counts) if counts else "no new items"

        send_plain_email(
            settings,
            subject="Re: Job Jigsaw — Profile update proposal",
            body=f"Done! Applied: {change_str}.\n\n{proposal.get('summary', '')}",
            in_reply_to=proposal.get("message_id"),
        )
        log.info("Proposal approved and applied: %s", change_str)

    elif intent == "reject":
        clear_proposal()
        send_plain_email(
            settings,
            subject="Re: Job Jigsaw — Profile update proposal",
            body="Got it — no changes made.",
            in_reply_to=proposal.get("message_id"),
        )
        log.info("Proposal rejected.")

    else:  # freeform
        log.info("Freeform reply — revising proposal...")
        revised = _revise_proposal(settings, proposal, body)
        if not revised:
            send_plain_email(
                settings,
                subject="Re: Job Jigsaw — Profile update proposal",
                body="Sorry, I had trouble understanding that. Please reply APPROVE, REJECT, or describe your changes more clearly.",
                in_reply_to=proposal.get("message_id"),
            )
            return

        msg_id, _ = send_plain_email(
            settings,
            subject="Re: Job Jigsaw — Profile update proposal",
            body=_proposal_body(revised),
            in_reply_to=proposal.get("message_id"),
        )
        revised["message_id"] = msg_id
        save_proposal(revised)
        log.info("Revised proposal sent.")


if __name__ == "__main__":
    main()
