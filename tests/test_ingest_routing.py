import json
import os
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("DYNAMODB_TABLE", "EmailDigestExecutions")
os.environ.setdefault("S3_BUCKET", "email-digest-poc-123456789")
os.environ.setdefault("SFN_ARN", "arn:aws:states:us-east-1:123456789:stateMachine:EmailDigest")
os.environ.setdefault("BOT_EMAIL", "bot@example.com")


def _ses_event(message_id="msg001"):
    return {"Records": [{"ses": {"mail": {"messageId": message_id}}}]}


def _raw_email(subject="Hello", from_="sender@example.com",
               message_id="<inbound001@mail.example.com>", in_reply_to=""):
    lines = [
        f"From: {from_}",
        "To: bot@example.com",
        f"Subject: {subject}",
        f"Message-ID: {message_id}",
    ]
    if in_reply_to:
        lines.append(f"In-Reply-To: {in_reply_to}")
    lines += ["", "Email body text here."]
    return "\n".join(lines).encode()


@pytest.fixture()
def ingest(monkeypatch):
    from conftest import load_handler
    h = load_handler("email_ingest")

    mock_s3 = MagicMock()
    mock_table = MagicMock()
    mock_sfn = MagicMock()
    mock_ses = MagicMock()

    mock_s3.get_object.return_value = {"Body": MagicMock(read=lambda: _raw_email())}
    mock_sfn.start_execution.return_value = {
        "executionArn": "arn:aws:states:us-east-1:123:execution:EmailDigest:exec-abcd1234"
    }
    mock_ses.send_email.return_value = {"MessageId": "aws-outbound-001"}
    mock_sfn.exceptions.InvalidToken = type("InvalidToken", (Exception,), {})
    mock_sfn.exceptions.TaskTimedOut = type("TaskTimedOut", (Exception,), {})

    monkeypatch.setattr(h, "s3_client", mock_s3)
    monkeypatch.setattr(h, "TABLE", mock_table)
    monkeypatch.setattr(h, "sfn", mock_sfn)
    monkeypatch.setattr(h, "ses", mock_ses)

    return {"handler": h, "s3": mock_s3, "table": mock_table, "sfn": mock_sfn, "ses": mock_ses}


class TestNewEmail:
    def test_starts_step_function(self, ingest):
        h, mocks = ingest["handler"], ingest
        mocks["s3"].get_object.return_value = {
            "Body": MagicMock(read=lambda: _raw_email(subject="Brand new email"))
        }
        h.handler(_ses_event(), None)
        mocks["sfn"].start_execution.assert_called_once()
        assert mocks["sfn"].start_execution.call_args.kwargs["stateMachineArn"] == os.environ["SFN_ARN"]

    def test_stores_email_in_s3(self, ingest):
        h, mocks = ingest["handler"], ingest
        mocks["s3"].get_object.return_value = {
            "Body": MagicMock(read=lambda: _raw_email(subject="New thread"))
        }
        h.handler(_ses_event(), None)
        put_calls = mocks["s3"].put_object.call_args_list
        assert any("emails/" in c.kwargs["Key"] for c in put_calls)

    def test_replies_to_sender_with_exec_id(self, ingest):
        h, mocks = ingest["handler"], ingest
        mocks["s3"].get_object.return_value = {
            "Body": MagicMock(read=lambda: _raw_email(from_="alice@example.com"))
        }
        h.handler(_ses_event(), None)
        mocks["ses"].send_email.assert_called_once()
        dest = mocks["ses"].send_email.call_args.kwargs["Destination"]["ToAddresses"]
        assert "alice@example.com" in dest

    def test_reply_subject_contains_exec_id(self, ingest):
        h, mocks = ingest["handler"], ingest
        mocks["s3"].get_object.return_value = {
            "Body": MagicMock(read=lambda: _raw_email())
        }
        h.handler(_ses_event(), None)
        subject = mocks["ses"].send_email.call_args.kwargs["Message"]["Subject"]["Data"]
        assert "exec-" in subject

    def test_writes_msgid_reverse_lookup(self, ingest):
        h, mocks = ingest["handler"], ingest
        mocks["s3"].get_object.return_value = {
            "Body": MagicMock(read=lambda: _raw_email())
        }
        h.handler(_ses_event(), None)
        msgid_puts = [
            c for c in mocks["table"].put_item.call_args_list
            if c.kwargs["Item"]["pk"].startswith("MSGID#")
        ]
        assert len(msgid_puts) == 1


class TestFollowupBySubject:
    def test_increments_email_count(self, ingest):
        h, mocks = ingest["handler"], ingest
        mocks["s3"].get_object.return_value = {
            "Body": MagicMock(read=lambda: _raw_email(subject="exec-abcd1234 update"))
        }
        h.handler(_ses_event(), None)
        exprs = [c.kwargs["UpdateExpression"] for c in mocks["table"].update_item.call_args_list]
        assert any("emailCount" in e for e in exprs)

    def test_does_not_start_new_execution(self, ingest):
        h, mocks = ingest["handler"], ingest
        mocks["s3"].get_object.return_value = {
            "Body": MagicMock(read=lambda: _raw_email(subject="exec-abcd1234 another update"))
        }
        h.handler(_ses_event(), None)
        mocks["sfn"].start_execution.assert_not_called()


class TestFollowupByInReplyTo:
    def test_resolves_exec_id_via_msgid_lookup(self, ingest):
        h, mocks = ingest["handler"], ingest
        mocks["s3"].get_object.return_value = {
            "Body": MagicMock(read=lambda: _raw_email(
                subject="Re: something unrelated",
                in_reply_to="<aws-outbound-001>",
            ))
        }
        mocks["table"].get_item.return_value = {
            "Item": {"pk": "MSGID#aws-outbound-001", "executionId": "exec-abcd1234"}
        }
        h.handler(_ses_event(), None)
        mocks["sfn"].start_execution.assert_not_called()
        exprs = [c.kwargs["UpdateExpression"] for c in mocks["table"].update_item.call_args_list]
        assert any("emailCount" in e for e in exprs)


class TestFinalEmail:
    def _setup_final(self, mocks, task_token="token-xyz"):
        mocks["s3"].get_object.return_value = {
            "Body": MagicMock(read=lambda: _raw_email(subject="exec-abcd1234 - FINAL"))
        }
        mocks["table"].get_item.return_value = {
            "Item": {
                "pk": "EXEC#exec-abcd1234",
                "taskToken": task_token,
                "originator": "sender@example.com",
            }
        }

    def test_calls_send_task_success(self, ingest):
        h, mocks = ingest["handler"], ingest
        self._setup_final(mocks)
        h.handler(_ses_event(), None)
        mocks["sfn"].send_task_success.assert_called_once()
        assert mocks["sfn"].send_task_success.call_args.kwargs["taskToken"] == "token-xyz"

    def test_marks_status_sent(self, ingest):
        h, mocks = ingest["handler"], ingest
        self._setup_final(mocks)
        h.handler(_ses_event(), None)
        values = [
            c.kwargs["ExpressionAttributeValues"]
            for c in mocks["table"].update_item.call_args_list
        ]
        assert any(v.get(":s") == "SENT" for v in values)

    def test_invalid_token_is_caught_not_raised(self, ingest):
        h, mocks = ingest["handler"], ingest
        self._setup_final(mocks)
        mocks["sfn"].send_task_success.side_effect = mocks["sfn"].exceptions.InvalidToken()
        h.handler(_ses_event(), None)  # must not raise

    def test_task_timed_out_is_caught_not_raised(self, ingest):
        h, mocks = ingest["handler"], ingest
        self._setup_final(mocks)
        mocks["sfn"].send_task_success.side_effect = mocks["sfn"].exceptions.TaskTimedOut()
        h.handler(_ses_event(), None)  # must not raise

    def test_missing_exec_record_is_ignored(self, ingest):
        h, mocks = ingest["handler"], ingest
        mocks["s3"].get_object.return_value = {
            "Body": MagicMock(read=lambda: _raw_email(subject="exec-abcd1234 - FINAL"))
        }
        mocks["table"].get_item.return_value = {}
        h.handler(_ses_event(), None)
        mocks["sfn"].send_task_success.assert_not_called()

    def test_missing_task_token_is_ignored(self, ingest):
        h, mocks = ingest["handler"], ingest
        mocks["s3"].get_object.return_value = {
            "Body": MagicMock(read=lambda: _raw_email(subject="exec-abcd1234 - FINAL"))
        }
        mocks["table"].get_item.return_value = {
            "Item": {"pk": "EXEC#exec-abcd1234", "originator": "sender@example.com"}
        }
        h.handler(_ses_event(), None)
        mocks["sfn"].send_task_success.assert_not_called()
