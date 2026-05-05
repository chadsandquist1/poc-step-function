import email
import hashlib


def parse_mime(raw_bytes: bytes) -> dict:
    msg = email.message_from_bytes(raw_bytes)
    return {
        "from": msg.get("From", ""),
        "to": msg.get("To", ""),
        "subject": msg.get("Subject", ""),
        "message_id": _strip_brackets(msg.get("Message-ID", "")),
        "in_reply_to": _strip_brackets(msg.get("In-Reply-To", "")),
        "body_text": _extract_text(msg),
    }


def _extract_text(msg) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            disposition = part.get("Content-Disposition", "")
            if ct == "text/plain" and "attachment" not in disposition:
                charset = part.get_content_charset() or "utf-8"
                return part.get_payload(decode=True).decode(charset, errors="replace")
        return ""
    if msg.get_content_type() == "text/plain":
        charset = msg.get_content_charset() or "utf-8"
        return msg.get_payload(decode=True).decode(charset, errors="replace")
    return ""


def _strip_brackets(value: str) -> str:
    return value.strip().strip("<>")


def from_hash(from_address: str) -> str:
    return hashlib.md5(from_address.encode()).hexdigest()[:8]
