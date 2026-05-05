import email
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase

from mime_parser import from_hash, parse_mime


def _make_simple(subject="Hello", from_="sender@example.com", body="Plain body",
                 message_id="<abc123@mail.example.com>", in_reply_to=""):
    msg = email.message.Message()
    msg["From"] = from_
    msg["To"] = "bot@example.com"
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    msg.set_payload(body.encode(), charset="utf-8")
    msg.set_type("text/plain")
    return msg.as_bytes()


def _make_multipart(text_body="Text part", with_attachment=False):
    msg = MIMEMultipart("mixed")
    msg["From"] = "sender@example.com"
    msg["To"] = "bot@example.com"
    msg["Subject"] = "Multipart test"
    msg["Message-ID"] = "<multi@example.com>"

    msg.attach(MIMEText(text_body, "plain"))

    if with_attachment:
        att = MIMEBase("application", "octet-stream")
        att.set_payload(b"binarydata")
        att.add_header("Content-Disposition", "attachment", filename="file.bin")
        msg.attach(att)

    return msg.as_bytes()


class TestParseSimple:
    def test_extracts_from(self):
        parsed = parse_mime(_make_simple(from_="alice@example.com"))
        assert parsed["from"] == "alice@example.com"

    def test_extracts_subject(self):
        parsed = parse_mime(_make_simple(subject="Test subject"))
        assert parsed["subject"] == "Test subject"

    def test_extracts_body(self):
        parsed = parse_mime(_make_simple(body="Hello world"))
        assert parsed["body_text"] == "Hello world"

    def test_strips_angle_brackets_from_message_id(self):
        parsed = parse_mime(_make_simple(message_id="<abc123@mail.example.com>"))
        assert parsed["message_id"] == "abc123@mail.example.com"

    def test_strips_angle_brackets_from_in_reply_to(self):
        raw = _make_simple(in_reply_to="<orig@mail.example.com>")
        parsed = parse_mime(raw)
        assert parsed["in_reply_to"] == "orig@mail.example.com"

    def test_missing_in_reply_to_is_empty_string(self):
        parsed = parse_mime(_make_simple())
        assert parsed["in_reply_to"] == ""

    def test_missing_message_id_is_empty_string(self):
        msg = email.message.Message()
        msg["From"] = "a@b.com"
        msg["Subject"] = "X"
        msg.set_payload(b"body")
        msg.set_type("text/plain")
        parsed = parse_mime(msg.as_bytes())
        assert parsed["message_id"] == ""


class TestParseMultipart:
    def test_extracts_text_part(self):
        parsed = parse_mime(_make_multipart(text_body="Multipart text"))
        assert parsed["body_text"] == "Multipart text"

    def test_attachment_does_not_appear_in_body(self):
        parsed = parse_mime(_make_multipart(text_body="Real body", with_attachment=True))
        assert parsed["body_text"] == "Real body"
        assert "binarydata" not in parsed["body_text"]

    def test_html_only_returns_empty_body(self):
        msg = MIMEMultipart("mixed")
        msg["From"] = "a@b.com"
        msg["Subject"] = "HTML only"
        msg["Message-ID"] = "<h@example.com>"
        msg.attach(MIMEText("<p>html</p>", "html"))
        parsed = parse_mime(msg.as_bytes())
        assert parsed["body_text"] == ""


class TestFromHash:
    def test_returns_eight_hex_chars(self):
        h = from_hash("user@example.com")
        assert len(h) == 8
        assert all(c in "0123456789abcdef" for c in h)

    def test_same_address_same_hash(self):
        assert from_hash("user@example.com") == from_hash("user@example.com")

    def test_different_addresses_different_hashes(self):
        assert from_hash("alice@example.com") != from_hash("bob@example.com")
