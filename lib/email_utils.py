import smtplib
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from config import Settings

log = logging.getLogger(__name__)

GMAIL_SMTP_SERVER = "smtp.gmail.com"
GMAIL_SMTP_PORT = 587
SCORE_GREEN_THRESHOLD = 70

SCORE_COLOR = {
    "green":  "#22c55e",
    "yellow": "#eab308",
}

_JOB_CARD = """\
<tr>
  <td style="padding:16px 0;border-bottom:1px solid #e5e7eb;">
    <table width="100%" cellpadding="0" cellspacing="0">
      <tr><td>
        <span style="display:inline-block;background:{score_bg};color:#fff;
                     font-weight:700;font-size:15px;padding:3px 10px;
                     border-radius:20px;margin-bottom:6px;">{score}/100</span>
        {badges}
      </td></tr>
      <tr><td style="font-size:17px;font-weight:700;color:#111827;padding-bottom:2px;">
        {title}
      </td></tr>
      <tr><td style="font-size:14px;color:#6b7280;padding-bottom:8px;">
        {company} &nbsp;·&nbsp; 📍 {location}{posted}
      </td></tr>
      <tr><td>
        <a href="{url}" style="display:inline-block;background:#2563eb;color:#fff;
                               text-decoration:none;padding:8px 18px;border-radius:6px;
                               font-size:14px;font-weight:600;">View Job →</a>
      </td></tr>
    </table>
  </td>
</tr>"""

_EMAIL_WRAPPER = """\
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f9fafb;
             font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f9fafb;padding:32px 0;">
    <tr><td align="center">
      <table width="600" cellpadding="0" cellspacing="0"
             style="background:#fff;border-radius:12px;padding:32px;
                    box-shadow:0 1px 3px rgba(0,0,0,.1);max-width:600px;width:100%;">
        <tr><td style="padding-bottom:24px;border-bottom:2px solid #e5e7eb;">
          <h1 style="margin:0;font-size:22px;color:#111827;">📋 {count} new jobs today</h1>
          <p style="margin:6px 0 0;color:#6b7280;font-size:14px;">
            Top match: <strong>{top_score}/100</strong> &nbsp;·&nbsp; {date}
          </p>
        </td></tr>
        <tr><td>
          <table width="100%" cellpadding="0" cellspacing="0">{cards}</table>
        </td></tr>
        <tr><td style="padding-top:24px;border-top:1px solid #e5e7eb;
                       font-size:12px;color:#9ca3af;text-align:center;">
          Sent by your job jigsaw{edit_link}
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""


def _badge(text: str, bg: str) -> str:
    return (
        f'<span style="display:inline-block;background:{bg};color:#fff;'
        f'font-size:12px;padding:2px 8px;border-radius:12px;'
        f'margin-left:6px;font-weight:500;">{text}</span>'
    )


def build_email_html(jobs: list[dict], date_str: str, site_url: str = "") -> str:
    cards = []
    for job in jobs:
        score = job.get("score", 0)
        score_bg = SCORE_COLOR["green"] if score >= SCORE_GREEN_THRESHOLD else SCORE_COLOR["yellow"]

        badges = ""
        if job.get("is_remote"):
            badges += _badge("Remote", "#7c3aed")
        if jtype := job.get("job_type", ""):
            badges += _badge(jtype.replace("_", " ").title(), "#374151")

        posted = job.get("date_posted", "")
        posted_str = f" &nbsp;·&nbsp; Posted: {posted}" if posted else ""

        cards.append(_JOB_CARD.format(
            score=score,
            score_bg=score_bg,
            badges=badges,
            title=job.get("title", ""),
            company=job.get("company", ""),
            location=job.get("location", ""),
            posted=posted_str,
            url=job.get("job_url", "#"),
        ))

    edit_link = (
        f' &nbsp;·&nbsp; <a href="{site_url}" style="color:#2563eb;">Edit profile</a>'
        if site_url else ""
    )

    return _EMAIL_WRAPPER.format(
        count=len(jobs),
        top_score=jobs[0]["score"] if jobs else 0,
        date=date_str,
        cards="\n".join(cards),
        edit_link=edit_link,
    )


def send_email(settings: Settings, subject: str, html: str) -> str:
    """Send an HTML email. Returns the Message-ID header value."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings.gmail_from
    msg["To"] = settings.gmail_to
    msg.attach(MIMEText(html, "html"))

    log.info("Sending email to %s — %s", settings.gmail_to, subject)
    with smtplib.SMTP(GMAIL_SMTP_SERVER, GMAIL_SMTP_PORT) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.login(settings.gmail_from, settings.gmail_app_password)
        smtp.sendmail(settings.gmail_from, settings.gmail_to, msg.as_string())
    log.info("Email sent.")
    return msg["Message-ID"] or ""


def send_plain_email(settings: Settings, subject: str, body: str,
                     in_reply_to: str = "") -> tuple[str, None]:
    """Send a plain-text email, optionally as a reply. Returns (Message-ID, None)."""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings.gmail_from
    msg["To"] = settings.gmail_to
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.attach(MIMEText(body, "plain"))

    log.info("Sending plain email to %s — %s", settings.gmail_to, subject)
    with smtplib.SMTP(GMAIL_SMTP_SERVER, GMAIL_SMTP_PORT) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.login(settings.gmail_from, settings.gmail_app_password)
        smtp.sendmail(settings.gmail_from, settings.gmail_to, msg.as_string())
    log.info("Plain email sent.")
    return msg["Message-ID"] or "", None
