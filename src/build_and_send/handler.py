import json
import logging as _logging
import os
import time
from datetime import datetime, timezone

import boto3

s3_client = boto3.client("s3")
ddb = boto3.resource("dynamodb")
ses = boto3.client("ses")

TABLE = ddb.Table(os.environ["DYNAMODB_TABLE"])
BUCKET = os.environ["S3_BUCKET"]
BOT_EMAIL = os.environ["BOT_EMAIL"]

_logger = _logging.getLogger()
_logger.setLevel(_logging.INFO)


def _log(level: str, message: str, **fields):
    _logger.log(
        getattr(_logging, level.upper()),
        json.dumps({"level": level, "message": message, **fields}),
    )


def handler(event, context):
    # Envelope: {metadata: {correlationId, initiatedAt, originator}, context: {executionId, isTimeout}}
    metadata = event["metadata"]
    ctx = event["context"]
    exec_id = ctx["executionId"]
    is_timeout = ctx["isTimeout"]
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
        emails = _fetch_emails(exec_id)
        _send_digest(exec_id, originator, emails)

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


def _fetch_emails(exec_id: str) -> list[dict]:
    emails = []
    paginator = s3_client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=BUCKET, Prefix=f"emails/{exec_id}/"):
        for obj in page.get("Contents", []):
            body = s3_client.get_object(Bucket=BUCKET, Key=obj["Key"])["Body"].read()
            emails.append(json.loads(body))
    emails.sort(key=lambda e: e.get("received_at", 0))
    return emails


def _send_digest(exec_id: str, originator: str, emails: list[dict]):
    total = len(emails)
    divider = "-" * 48

    lines = [
        f"=== Email Digest for Thread: {exec_id} ===",
        f"Total emails collected: {total}",
        "",
    ]

    for i, em in enumerate(emails, start=1):
        received = _fmt_ts(em.get("received_at", 0))
        lines += [
            divider,
            f"Email {i} of {total}",
            f"From:     {em.get('from', '(unknown)')}",
            f"Received: {received}",
            f"Subject:  {em.get('subject', '(no subject)')}",
            "",
            em.get("body_text", "").strip(),
            "",
        ]

    ses.send_email(
        Source=BOT_EMAIL,
        Destination={"ToAddresses": [originator]},
        Message={
            "Subject": {"Data": f"[{exec_id}] Digest ({total} email{'s' if total != 1 else ''})"},
            "Body": {"Text": {"Data": "\n".join(lines)}},
        },
    )
    _log("info", "digest_sent", exec_id=exec_id, total=total)


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
            "Subject": {"Data": f"[{exec_id}] Thread expired — NEVER FOLLOWED UP"},
            "Body": {"Text": {"Data": body}},
        },
    )
    _log("info", "timeout_notice_sent", exec_id=exec_id, email_count=email_count)


def _fmt_ts(epoch_ms) -> str:
    if not epoch_ms:
        return "(unknown)"
    return datetime.fromtimestamp(int(epoch_ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
