"""Outbound mail for email verification (optional feature).

Everything is dormant until SMTP is configured (config.smtp_configured()).
No third-party SDK: plain smtplib with STARTTLS/SSL so the dependency list
stays zero and mainland deployments can point at any provider (DirectMail /
QQ / 163...). Credentials come from the environment ONLY.

Delivery problems raise MailerError with the reason logged; callers turn it
into a user-visible "please retry / resend later" state, never a crash.
"""
import logging
import smtplib
import ssl
from email.mime.text import MIMEText
from email.utils import formataddr

from config import (SMTP_FROM, SMTP_HOST, SMTP_PASSWORD, SMTP_PORT,
                    SMTP_USER, smtp_configured)

log = logging.getLogger("mailer")


class MailerError(RuntimeError):
    """Mail could not be sent (SMTP unreachable / auth failed / rejected)."""


def _smtp_connect():
    if SMTP_PORT == 465:
        ctx = ssl.create_default_context()
        return smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=20, context=ctx)
    s = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20)
    s.starttls(context=ssl.create_default_context())
    return s


def send_mail(to_email, subject, text):
    """Send a plain-text mail. Raises MailerError on any failure."""
    if not smtp_configured():
        raise MailerError("SMTP is not configured")
    msg = MIMEText(text, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = formataddr(("Agent Context Sync", SMTP_FROM))
    msg["To"] = to_email
    try:
        s = _smtp_connect()
        try:
            s.login(SMTP_USER, SMTP_PASSWORD)
            s.sendmail(SMTP_FROM, [to_email], msg.as_string())
        finally:
            try:
                s.quit()
            except Exception:
                s.close()
    except Exception as e:  # network / auth / rejection
        log.warning("SMTP send to %s failed: %s", to_email, e)
        raise MailerError("mail_send_failed") from e


def send_verification_mail(to_email, verify_url, lang="zh-CN"):
    """Send the account-activation / email-binding mail."""
    if lang == "en":
        subject = "Verify your email — Agent Context Sync"
        text = (
            "Hello,\n\n"
            "verify your email address to activate your Agent Context Sync "
            "account:\n\n" + verify_url + "\n\n"
            "The link is valid for 30 minutes and can be used once. "
            "If you did not request it, ignore this mail.\n")
    else:
        subject = "验证邮箱 — Agent Context Sync"
        text = (
            "你好，\n\n"
            "请验证你的邮箱以激活 Agent Context Sync 账户：\n\n"
            + verify_url + "\n\n"
            "链接 30 分钟内有效且仅可使用一次。若非本人操作，请忽略本邮件。\n")
    send_mail(to_email, subject, text)
