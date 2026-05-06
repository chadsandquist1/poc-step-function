import json
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


def handler(event, context):
    # SFN passes the full state; unwrap Payload if present (lambda:invoke wrapping)
    payload = event.get("Payload", event)
    exec_id = payload["executionId"]
    is_timeout = "timeoutError" in payload

    resp = TABLE.get_item(
        Key={"pk": f"EXEC#{exec_id}"},
        ConsistentRead=True,
    )
    item = resp.get("Item")
    if not item:
        print(f"[ERROR] No execution record for {exec_id}; cannot send.")
        return

    originator = item["originator"]

    if is_timeout:
        _send_timeout_notice(exec_id, originator, item)
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


def _send_timeout_notice(exec_id: str, originator: str, item: dict):
    created = _fmt_ts(item.get("createdAt", 0))
    email_count = int(item.get("emailCount", 0))

    body = (
        f"=== Thread Expired: {exec_id} ===\n\n"
        f"Your email thread was opened but no FINAL email was received within 15 minutes.\n\n"
        f"Thread ID:              {exec_id}\n"
        f"Opened:                 {created}\n"
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


def _fmt_ts(epoch_ms) -> str:
    if not epoch_ms:
        return "(unknown)"
    return datetime.fromtimestamp(int(epoch_ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
