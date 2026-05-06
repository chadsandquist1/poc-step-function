import json
import os
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("S3_BUCKET", "email-digest-poc-123456789")


def _envelope(exec_id="exec-abcd1234", originator="sender@example.com"):
    return {
        "metadata": {
            "correlationId": exec_id,
            "initiatedAt": "2026-05-06T10:00:00+00:00",
            "originator": originator,
            "traceId": "",
        },
        "context": {"executionId": exec_id},
    }


def _email_obj(from_="sender@example.com", subject="Hello", body="Body text",
               received_at=1746518400000):
    return {"from": from_, "subject": subject, "body_text": body, "received_at": received_at}


def _s3_key(exec_id, epoch_ms, from_hash="abc12345"):
    return f"emails/{exec_id}/{epoch_ms}_{from_hash}.json"


@pytest.fixture()
def gather(monkeypatch):
    from conftest import load_handler
    h = load_handler("gather_emails")

    mock_s3 = MagicMock()
    monkeypatch.setattr(h, "s3_client", mock_s3)

    return {"handler": h, "s3": mock_s3}


def _setup_s3(mocks, exec_id, emails_by_epoch: dict):
    """
    emails_by_epoch: {epoch_ms: email_dict}  — keys determine filename sort order.
    """
    keys = [_s3_key(exec_id, ep) for ep in sorted(emails_by_epoch)]
    contents = [{"Key": k} for k in keys]

    paginator = MagicMock()
    paginator.paginate.return_value = iter([{"Contents": contents}])
    mocks["s3"].get_paginator.return_value = paginator

    ordered_emails = [emails_by_epoch[ep] for ep in sorted(emails_by_epoch)]
    mocks["s3"].get_object.side_effect = [
        {"Body": MagicMock(read=lambda em=em: json.dumps(em).encode())}
        for em in ordered_emails
    ]
    return keys


class TestSliceShape:
    def test_returns_required_keys(self, gather):
        h, mocks = gather["handler"], gather
        _setup_s3(mocks, "exec-abcd1234", {1746518400000: _email_obj()})
        result = h.handler(_envelope(), None)
        assert "emailCount" in result
        assert "firstEmailAt" in result
        assert "lastEmailAt" in result
        assert "timelineText" in result
        assert "fullBodyText" in result

    def test_no_envelope_fields_in_result(self, gather):
        h, mocks = gather["handler"], gather
        _setup_s3(mocks, "exec-abcd1234", {1746518400000: _email_obj()})
        result = h.handler(_envelope(), None)
        assert "metadata" not in result
        assert "context" not in result
        assert "errors" not in result

    def test_email_count_matches_s3_objects(self, gather):
        h, mocks = gather["handler"], gather
        _setup_s3(mocks, "exec-abcd1234", {
            1746518400000: _email_obj(),
            1746518460000: _email_obj(subject="Second"),
            1746518520000: _email_obj(subject="Third"),
        })
        result = h.handler(_envelope(), None)
        assert result["emailCount"] == 3


class TestSortOrder:
    def test_sorted_ascending_by_epoch_prefix(self, gather):
        h, mocks = gather["handler"], gather
        # Insert in reverse order to test that sorting is by filename, not insertion order
        _setup_s3(mocks, "exec-abcd1234", {
            1746518520000: _email_obj(body="Third"),
            1746518400000: _email_obj(body="First"),
            1746518460000: _email_obj(body="Second"),
        })
        result = h.handler(_envelope(), None)
        body = result["fullBodyText"]
        assert body.index("First") < body.index("Second") < body.index("Third")

    def test_first_email_is_t_plus_zero(self, gather):
        h, mocks = gather["handler"], gather
        _setup_s3(mocks, "exec-abcd1234", {
            1746518400000: _email_obj(subject="First email"),
            1746518460000: _email_obj(subject="Second email"),
        })
        result = h.handler(_envelope(), None)
        first_line = result["timelineText"].split("\n")[0]
        assert first_line.startswith("T+0m")

    def test_subsequent_email_t_plus_minutes(self, gather):
        h, mocks = gather["handler"], gather
        base = 1746518400000
        _setup_s3(mocks, "exec-abcd1234", {
            base:          _email_obj(subject="First",  received_at=base),
            base + 300000: _email_obj(subject="Second", received_at=base + 300000),  # +5 min
        })
        result = h.handler(_envelope(), None)
        lines = result["timelineText"].split("\n")
        assert lines[1].startswith("T+5m")


class TestTimelineFormat:
    def test_timeline_line_format(self, gather):
        h, mocks = gather["handler"], gather
        _setup_s3(mocks, "exec-abcd1234", {
            1746518400000: _email_obj(from_="alice@example.com", subject="Meeting notes",
                                      received_at=1746518400000),
        })
        result = h.handler(_envelope(), None)
        line = result["timelineText"]
        assert "T+0m" in line
        assert "From: alice@example.com" in line
        assert "Subject: Meeting notes" in line

    def test_full_body_text_joins_with_separator(self, gather):
        h, mocks = gather["handler"], gather
        _setup_s3(mocks, "exec-abcd1234", {
            1746518400000: _email_obj(body="Alpha body"),
            1746518460000: _email_obj(body="Beta body"),
        })
        result = h.handler(_envelope(), None)
        assert "Alpha body" in result["fullBodyText"]
        assert "Beta body" in result["fullBodyText"]
        assert "---" in result["fullBodyText"]


class TestTimestamps:
    def test_first_and_last_email_at_are_iso_strings(self, gather):
        h, mocks = gather["handler"], gather
        _setup_s3(mocks, "exec-abcd1234", {
            1746518400000: _email_obj(received_at=1746518400000),
            1746518460000: _email_obj(received_at=1746518460000),
        })
        result = h.handler(_envelope(), None)
        # ISO 8601 format — contains T and +
        assert "T" in result["firstEmailAt"]
        assert "T" in result["lastEmailAt"]
        assert result["firstEmailAt"] != result["lastEmailAt"]

    def test_first_earlier_than_last(self, gather):
        h, mocks = gather["handler"], gather
        _setup_s3(mocks, "exec-abcd1234", {
            1746518400000: _email_obj(received_at=1746518400000),
            1746519000000: _email_obj(received_at=1746519000000),
        })
        result = h.handler(_envelope(), None)
        assert result["firstEmailAt"] < result["lastEmailAt"]


class TestEmptyBucket:
    def test_zero_emails_returns_safe_defaults(self, gather):
        h, mocks = gather["handler"], gather
        paginator = MagicMock()
        paginator.paginate.return_value = iter([{"Contents": []}])
        mocks["s3"].get_paginator.return_value = paginator
        result = h.handler(_envelope(), None)
        assert result["emailCount"] == 0
        assert result["timelineText"] == "(no emails collected)"

    def test_empty_page_no_contents_key(self, gather):
        h, mocks = gather["handler"], gather
        paginator = MagicMock()
        paginator.paginate.return_value = iter([{}])  # no "Contents" key
        mocks["s3"].get_paginator.return_value = paginator
        result = h.handler(_envelope(), None)
        assert result["emailCount"] == 0
