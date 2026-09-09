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


def send_password_reset_mail(to_email, reset_url, lang="zh-CN"):
    """Send the password-reset mail (verified-email accounts only)."""
    if lang == "en":
        subject = "Reset your password — Agent Context Sync"
        text = (
            "Hello,\n\n"
            "we received a request to reset your Agent Context Sync password. "
            "Open the link below to choose a new one:\n\n" + reset_url + "\n\n"
            "The link is valid for 30 minutes and can be used once. "
            "If you did not request it, ignore this mail and your password "
            "will stay unchanged.\n")
    else:
        subject = "重置密码 — Agent Context Sync"
        text = (
            "你好，\n\n"
            "我们收到重置你的 Agent Context Sync 密码的请求。"
            "请打开以下链接设置新密码：\n\n" + reset_url + "\n\n"
            "链接 30 分钟内有效且仅可使用一次。若非本人操作，请忽略本邮件，"
            "你的密码将保持不变。\n")
    send_mail(to_email, subject, text)


def send_email_changed_notice(old_email, new_email, lang="zh-CN"):
    """Notify BOTH addresses after a verified email change."""
    if lang == "en":
        subject = "Your security email changed — Agent Context Sync"
        text = (
            "Hello,\n\n"
            "your Agent Context Sync security email was changed from\n"
            + old_email + "\nto\n" + new_email + "\n\n"
            "If this was not you, contact your administrator immediately.\n")
    else:
        subject = "安全邮箱已变更 — Agent Context Sync"
        text = (
            "你好，\n\n"
            "你的 Agent Context Sync 安全邮箱已由\n"
            + old_email + "\n变更为\n" + new_email + "\n\n"
            "若非本人操作，请立即联系管理员。\n")
    send_mail(old_email, subject, text)
    send_mail(new_email, subject, text)
