import json
import os
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("DYNAMODB_TABLE", "EmailDigestExecutions")
os.environ.setdefault("BOT_EMAIL", "bot@example.com")


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


def _event(exec_id="exec-abcd1234", originator="sender@example.com",
           is_timeout=False, initiated_at="2026-05-06T00:00:00+00:00",
           email_count=2,
           first_email_at="2026-05-06T10:00:00+00:00",
           last_email_at="2026-05-06T10:15:00+00:00",
           timeline_text="T+0m \u2014 From: sender@example.com | Subject: Hello",
           full_body_text="Body text here.",
           summary_json=None):
    ctx = {"executionId": exec_id}
    if is_timeout:
        ctx["isTimeout"] = True
    else:
        ctx.update({
            "emailCount": email_count,
            "firstEmailAt": first_email_at,
            "lastEmailAt": last_email_at,
            "timelineText": timeline_text,
            "fullBodyText": full_body_text,
        })
        if summary_json is not None:
            ctx["summaryJson"] = summary_json
    return {
        "metadata": {
            "correlationId": exec_id,
            "initiatedAt": initiated_at,
            "originator": originator,
            "traceId": "",
        },
        "context": ctx,
    }


@pytest.fixture()
def digest(monkeypatch):
    from conftest import load_handler
    h = load_handler("build_and_send")

    mock_table = MagicMock()
    mock_ses = MagicMock()

    monkeypatch.setattr(h, "TABLE", mock_table)
    monkeypatch.setattr(h, "ses", mock_ses)

    return {"handler": h, "table": mock_table, "ses": mock_ses}


class TestDigestSend:
    def test_sends_to_originator(self, digest):
        h, mocks = digest["handler"], digest
        mocks["table"].get_item.return_value = {"Item": _exec_item()}
        h.handler(_event(originator="sender@example.com"), None)
        dest = mocks["ses"].send_email.call_args.kwargs["Destination"]["ToAddresses"]
        assert "sender@example.com" in dest

    def test_subject_contains_exec_id_and_count(self, digest):
        h, mocks = digest["handler"], digest
        mocks["table"].get_item.return_value = {"Item": _exec_item()}
        h.handler(_event(email_count=2), None)
        subject = mocks["ses"].send_email.call_args.kwargs["Message"]["Subject"]["Data"]
        assert "exec-abcd1234" in subject
        assert "2" in subject

    def test_body_contains_timeline(self, digest):
        h, mocks = digest["handler"], digest
        mocks["table"].get_item.return_value = {"Item": _exec_item()}
        h.handler(_event(timeline_text="T+0m \u2014 From: alice@example.com | Subject: Hi"), None)
        body = mocks["ses"].send_email.call_args.kwargs["Message"]["Body"]["Text"]["Data"]
        assert "alice@example.com" in body

    def test_body_contains_full_body_text(self, digest):
        h, mocks = digest["handler"], digest
        mocks["table"].get_item.return_value = {"Item": _exec_item()}
        h.handler(_event(full_body_text="Unique content XYZ"), None)
        body = mocks["ses"].send_email.call_args.kwargs["Message"]["Body"]["Text"]["Data"]
        assert "Unique content XYZ" in body

    def test_body_shows_total_count(self, digest):
        h, mocks = digest["handler"], digest
        mocks["table"].get_item.return_value = {"Item": _exec_item()}
        h.handler(_event(email_count=3), None)
        body = mocks["ses"].send_email.call_args.kwargs["Message"]["Body"]["Text"]["Data"]
        assert "Total emails collected: 3" in body

    def test_marks_status_sent(self, digest):
        h, mocks = digest["handler"], digest
        mocks["table"].get_item.return_value = {"Item": _exec_item()}
        h.handler(_event(), None)
        values = [c.kwargs["ExpressionAttributeValues"] for c in mocks["table"].update_item.call_args_list]
        assert any(v.get(":s") == "SENT" for v in values)

    def test_missing_exec_record_does_not_raise(self, digest):
        h, mocks = digest["handler"], digest
        mocks["table"].get_item.return_value = {}
        h.handler(_event(), None)
        mocks["ses"].send_email.assert_not_called()


class TestDigestSummary:
    def _summary_json(self, summary="Thread about cats.", key_points=None):
        return json.dumps({
            "summary": summary,
            "keyPoints": key_points or ["Point one", "Point two"],
        })

    def test_renders_summary_section_when_present(self, digest):
        h, mocks = digest["handler"], digest
        mocks["table"].get_item.return_value = {"Item": _exec_item()}
        h.handler(_event(summary_json=self._summary_json()), None)
        body = mocks["ses"].send_email.call_args.kwargs["Message"]["Body"]["Text"]["Data"]
        assert "## Summary" in body
        assert "Thread about cats." in body

    def test_renders_key_points_as_bullets(self, digest):
        h, mocks = digest["handler"], digest
        mocks["table"].get_item.return_value = {"Item": _exec_item()}
        h.handler(_event(summary_json=self._summary_json(key_points=["Alpha", "Beta"])), None)
        body = mocks["ses"].send_email.call_args.kwargs["Message"]["Body"]["Text"]["Data"]
        assert "\u2022 Alpha" in body
        assert "\u2022 Beta" in body

    def test_no_summary_section_when_absent(self, digest):
        h, mocks = digest["handler"], digest
        mocks["table"].get_item.return_value = {"Item": _exec_item()}
        h.handler(_event(summary_json=None), None)
        body = mocks["ses"].send_email.call_args.kwargs["Message"]["Body"]["Text"]["Data"]
        assert "## Summary" not in body

    def test_malformed_summary_json_omits_section(self, digest):
        h, mocks = digest["handler"], digest
        mocks["table"].get_item.return_value = {"Item": _exec_item()}
        h.handler(_event(summary_json="not valid json {{{"), None)
        body = mocks["ses"].send_email.call_args.kwargs["Message"]["Body"]["Text"]["Data"]
        assert "## Summary" not in body

    def test_summary_appears_before_email_list(self, digest):
        h, mocks = digest["handler"], digest
        mocks["table"].get_item.return_value = {"Item": _exec_item()}
        h.handler(_event(summary_json=self._summary_json()), None)
        body = mocks["ses"].send_email.call_args.kwargs["Message"]["Body"]["Text"]["Data"]
        assert body.index("## Summary") < body.index("=== Email Digest")


class TestTimeoutNotice:
    def test_sends_timeout_notice(self, digest):
        h, mocks = digest["handler"], digest
        mocks["table"].get_item.return_value = {"Item": _exec_item()}
        h.handler(_event(is_timeout=True), None)
        mocks["ses"].send_email.assert_called_once()
        subject = mocks["ses"].send_email.call_args.kwargs["Message"]["Subject"]["Data"]
        assert "NEVER FOLLOWED UP" in subject

    def test_timeout_body_contains_exec_id(self, digest):
        h, mocks = digest["handler"], digest
        mocks["table"].get_item.return_value = {"Item": _exec_item()}
        h.handler(_event(is_timeout=True), None)
        body = mocks["ses"].send_email.call_args.kwargs["Message"]["Body"]["Text"]["Data"]
        assert "exec-abcd1234" in body

    def test_timeout_body_contains_email_count(self, digest):
        h, mocks = digest["handler"], digest
        mocks["table"].get_item.return_value = {"Item": _exec_item(email_count=3)}
        h.handler(_event(is_timeout=True), None)
        body = mocks["ses"].send_email.call_args.kwargs["Message"]["Body"]["Text"]["Data"]
        assert "3" in body

    def test_timeout_marks_status_sent(self, digest):
        h, mocks = digest["handler"], digest
        mocks["table"].get_item.return_value = {"Item": _exec_item()}
        h.handler(_event(is_timeout=True), None)
        values = [c.kwargs["ExpressionAttributeValues"] for c in mocks["table"].update_item.call_args_list]
        assert any(v.get(":s") == "SENT" for v in values)

    def test_missing_exec_record_does_not_raise(self, digest):
        h, mocks = digest["handler"], digest
        mocks["table"].get_item.return_value = {}
        h.handler(_event(is_timeout=True), None)
        mocks["ses"].send_email.assert_not_called()
