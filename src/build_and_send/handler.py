import json
import logging as _logging
import os
import time

import boto3

ddb = boto3.resource("dynamodb")
ses = boto3.client("ses")

TABLE = ddb.Table(os.environ["DYNAMODB_TABLE"])
BOT_EMAIL = os.environ["BOT_EMAIL"]

_logger = _logging.getLogger()
_logger.setLevel(_logging.INFO)


def _log(level: str, message: str, **fields):
    _logger.log(
        getattr(_logging, level.upper()),
        json.dumps({"level": level, "message": message, **fields}),
    )


def handler(event, context):
    if "metadata" not in event:
        # Stranded execution started before envelope contract was deployed.
        _log("warn", "legacy_event_ignored", raw_keys=list(event.keys()))
        return {}
    metadata = event["metadata"]
    ctx = event["context"]
    exec_id = ctx["executionId"]
    is_timeout = ctx.get("isTimeout", False)
    originator = metadata["originator"]

    _log("info", "handler_start", exec_id=exec_id, is_timeout=is_timeout)

    resp = TABLE.get_item(
        Key={"pk": f"EXEC#{exec_id}"},
        ConsistentRead=True,
    )
    item = resp.get("Item")
    if not item:
        _log("error", "no_execution_record", exec_id=exec_id)
        return {}

    if is_timeout:
        _send_timeout_notice(exec_id, originator, metadata["initiatedAt"], item)
    else:
        _send_digest(exec_id, originator, ctx)

    TABLE.update_item(
        Key={"pk": f"EXEC#{exec_id}"},
        UpdateExpression="SET #st = :s, updatedAt = :now",
        ExpressionAttributeNames={"#st": "status"},
        ExpressionAttributeValues={
            ":s": "SENT",
            ":now": int(time.time() * 1000),
        },
    )
    _log("info", "handler_complete", exec_id=exec_id)
    return {}


def _send_digest(exec_id: str, originator: str, ctx: dict):
    email_count = ctx.get("emailCount", 0)
    first_email_at = ctx.get("firstEmailAt", "")
    last_email_at = ctx.get("lastEmailAt", "")
    timeline_text = ctx.get("timelineText", "")
    full_body_text = ctx.get("fullBodyText", "")

    # Parse AI summary — raw JSON string from Bedrock via context.
    # Invalid JSON or absent key both result in no summary section.
    digest_summary = None
    summary_json = ctx.get("summaryJson")
    if summary_json:
        try:
            digest_summary = json.loads(summary_json)
        except (json.JSONDecodeError, TypeError, ValueError):
            _log("warn", "summary_parse_failed", exec_id=exec_id)

    divider = "-" * 48
    lines = []

    if digest_summary:
        lines += [
            "## Summary",
            digest_summary.get("summary", ""),
            "",
            "Key Points:",
        ]
        for point in digest_summary.get("keyPoints", []):
            lines.append(f"\u2022 {point}")
        lines += ["", divider, ""]

    lines += [
        f"=== Email Digest for Thread: {exec_id} ===",
        f"Total emails collected: {email_count}",
        f"First email: {first_email_at}",
        f"Last email:  {last_email_at}",
        "",
        "Timeline:",
        timeline_text,
        "",
        divider,
        "Full Content:",
        "",
        full_body_text,
    ]

    ses.send_email(
        Source=BOT_EMAIL,
        Destination={"ToAddresses": [originator]},
        Message={
            "Subject": {"Data": f"[{exec_id}] Digest ({email_count} email{'s' if email_count != 1 else ''})"},
            "Body": {"Text": {"Data": "\n".join(lines)}},
        },
    )
    _log("info", "digest_sent", exec_id=exec_id, email_count=email_count)


def _send_timeout_notice(exec_id: str, originator: str, initiated_at: str, item: dict):
    email_count = int(item.get("emailCount", 0))

    body = (
        f"=== Thread Expired: {exec_id} ===\n\n"
        f"Your email thread was opened but no FINAL email was received within 15 minutes.\n\n"
        f"Thread ID:              {exec_id}\n"
        f"Opened:                 {initiated_at}\n"
        f"Emails collected:       {email_count}\n\n"
        f"No digest was sent. To start a new thread, send a new email."
    )

    ses.send_email(
        Source=BOT_EMAIL,
        Destination={"ToAddresses": [originator]},
        Message={
            "Subject": {"Data": f"[{exec_id}] Thread expired \u2014 NEVER FOLLOWED UP"},
            "Body": {"Text": {"Data": body}},
        },
    )
    _log("info", "timeout_notice_sent", exec_id=exec_id, email_count=email_count)
