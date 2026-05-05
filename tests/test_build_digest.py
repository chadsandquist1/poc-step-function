import json
import os
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("DYNAMODB_TABLE", "EmailDigestExecutions")
os.environ.setdefault("S3_BUCKET", "email-digest-poc-123456789")
os.environ.setdefault("BOT_EMAIL", "bot@example.com")


def _email_obj(from_="sender@example.com", subject="Test", body="Body text",
               received_at=1700000000000):
    return {
        "from": from_,
        "to": "bot@example.com",
        "subject": subject,
        "body_text": body,
        "received_at": received_at,
    }


def _exec_item(exec_id="exec-abcd1234", originator="sender@example.com",
               email_count=2, created_at=1700000000000):
    return {
        "pk": f"EXEC#{exec_id}",
        "executionId": exec_id,
        "originator": originator,
        "status": "COLLECTING",
        "emailCount": email_count,
        "createdAt": created_at,
    }


@pytest.fixture()
def digest(monkeypatch):
    from conftest import load_handler
    h = load_handler("build_and_send")

    mock_s3 = MagicMock()
    mock_table = MagicMock()
    mock_ses = MagicMock()

    monkeypatch.setattr(h, "s3_client", mock_s3)
    monkeypatch.setattr(h, "TABLE", mock_table)
    monkeypatch.setattr(h, "ses", mock_ses)

    return {"handler": h, "s3": mock_s3, "table": mock_table, "ses": mock_ses}


def _setup_s3_emails(mocks, emails: list[dict]):
    pages = [{"Contents": [{"Key": f"emails/exec-abcd1234/{i}.json"} for i in range(len(emails))]}]
    paginator = MagicMock()
    paginator.paginate.return_value = iter(pages)
    mocks["s3"].get_paginator.return_value = paginator
    mocks["s3"].get_object.side_effect = [
        {"Body": MagicMock(read=lambda em=em: json.dumps(em).encode())}
        for em in emails
    ]


class TestDigestSend:
    def test_sends_to_originator(self, digest):
        h, mocks = digest["handler"], digest
        mocks["table"].get_item.return_value = {"Item": _exec_item()}
        _setup_s3_emails(mocks, [_email_obj()])
        h.handler({"executionId": "exec-abcd1234"}, None)
        dest = mocks["ses"].send_email.call_args.kwargs["Destination"]["ToAddresses"]
        assert "sender@example.com" in dest

    def test_subject_contains_exec_id_and_count(self, digest):
        h, mocks = digest["handler"], digest
        mocks["table"].get_item.return_value = {"Item": _exec_item()}
        _setup_s3_emails(mocks, [_email_obj(), _email_obj(subject="Follow up")])
        h.handler({"executionId": "exec-abcd1234"}, None)
        subject = mocks["ses"].send_email.call_args.kwargs["Message"]["Subject"]["Data"]
        assert "exec-abcd1234" in subject
        assert "2" in subject

    def test_body_contains_all_senders(self, digest):
        h, mocks = digest["handler"], digest
        mocks["table"].get_item.return_value = {"Item": _exec_item()}
        _setup_s3_emails(mocks, [
            _email_obj(from_="alice@example.com"),
            _email_obj(from_="bob@example.com"),
        ])
        h.handler({"executionId": "exec-abcd1234"}, None)
        body = mocks["ses"].send_email.call_args.kwargs["Message"]["Body"]["Text"]["Data"]
        assert "alice@example.com" in body
        assert "bob@example.com" in body

    def test_body_contains_email_bodies(self, digest):
        h, mocks = digest["handler"], digest
        mocks["table"].get_item.return_value = {"Item": _exec_item()}
        _setup_s3_emails(mocks, [_email_obj(body="Unique content XYZ")])
        h.handler({"executionId": "exec-abcd1234"}, None)
        body = mocks["ses"].send_email.call_args.kwargs["Message"]["Body"]["Text"]["Data"]
        assert "Unique content XYZ" in body

    def test_body_shows_total_count(self, digest):
        h, mocks = digest["handler"], digest
        mocks["table"].get_item.return_value = {"Item": _exec_item(email_count=3)}
        _setup_s3_emails(mocks, [_email_obj(), _email_obj(), _email_obj()])
        h.handler({"executionId": "exec-abcd1234"}, None)
        body = mocks["ses"].send_email.call_args.kwargs["Message"]["Body"]["Text"]["Data"]
        assert "Total emails collected: 3" in body

    def test_emails_sorted_by_received_at(self, digest):
        h, mocks = digest["handler"], digest
        mocks["table"].get_item.return_value = {"Item": _exec_item()}
        _setup_s3_emails(mocks, [
            _email_obj(body="Second", received_at=1700000002000),
            _email_obj(body="First", received_at=1700000001000),
        ])
        h.handler({"executionId": "exec-abcd1234"}, None)
        body = mocks["ses"].send_email.call_args.kwargs["Message"]["Body"]["Text"]["Data"]
        assert body.index("First") < body.index("Second")

    def test_marks_status_sent(self, digest):
        h, mocks = digest["handler"], digest
        mocks["table"].get_item.return_value = {"Item": _exec_item()}
        _setup_s3_emails(mocks, [_email_obj()])
        h.handler({"executionId": "exec-abcd1234"}, None)
        values = [c.kwargs["ExpressionAttributeValues"] for c in mocks["table"].update_item.call_args_list]
        assert any(v.get(":s") == "SENT" for v in values)


class TestTimeoutNotice:
    def _timeout_event(self):
        return {
            "executionId": "exec-abcd1234",
            "timeoutError": {"Error": "States.Timeout", "Cause": "timed out"},
        }

    def test_sends_timeout_notice(self, digest):
        h, mocks = digest["handler"], digest
        mocks["table"].get_item.return_value = {"Item": _exec_item()}
        h.handler(self._timeout_event(), None)
        mocks["ses"].send_email.assert_called_once()
        subject = mocks["ses"].send_email.call_args.kwargs["Message"]["Subject"]["Data"]
        assert "NEVER FOLLOWED UP" in subject

    def test_timeout_body_contains_exec_id(self, digest):
        h, mocks = digest["handler"], digest
        mocks["table"].get_item.return_value = {"Item": _exec_item()}
        h.handler(self._timeout_event(), None)
        body = mocks["ses"].send_email.call_args.kwargs["Message"]["Body"]["Text"]["Data"]
        assert "exec-abcd1234" in body

    def test_timeout_body_contains_email_count(self, digest):
        h, mocks = digest["handler"], digest
        mocks["table"].get_item.return_value = {"Item": _exec_item(email_count=3)}
        h.handler(self._timeout_event(), None)
        body = mocks["ses"].send_email.call_args.kwargs["Message"]["Body"]["Text"]["Data"]
        assert "3" in body

    def test_timeout_does_not_list_s3_objects(self, digest):
        h, mocks = digest["handler"], digest
        mocks["table"].get_item.return_value = {"Item": _exec_item()}
        h.handler(self._timeout_event(), None)
        mocks["s3"].get_paginator.assert_not_called()

    def test_timeout_marks_status_sent(self, digest):
        h, mocks = digest["handler"], digest
        mocks["table"].get_item.return_value = {"Item": _exec_item()}
        h.handler(self._timeout_event(), None)
        values = [c.kwargs["ExpressionAttributeValues"] for c in mocks["table"].update_item.call_args_list]
        assert any(v.get(":s") == "SENT" for v in values)

    def test_missing_exec_record_does_not_raise(self, digest):
        h, mocks = digest["handler"], digest
        mocks["table"].get_item.return_value = {}
        h.handler({"executionId": "exec-abcd1234"}, None)
        mocks["ses"].send_email.assert_not_called()
