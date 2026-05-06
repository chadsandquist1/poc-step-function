import json
import logging
import os
import functools
from datetime import datetime, timezone

import boto3
import structlog
from pydantic import BaseModel

try:
    import aws_xray_sdk.core as xray
    _XRAY = True
except ImportError:
    _XRAY = False

s3_client = boto3.client("s3")
BUCKET = os.environ["S3_BUCKET"]

structlog.configure(
    processors=[structlog.processors.JSONRenderer()],
    logger_factory=structlog.PrintLoggerFactory(),
)
_log = structlog.get_logger()


class StepMetadata(BaseModel):
    correlationId: str
    initiatedAt: str
    originator: str
    traceId: str = ""


class StepEnvelope(BaseModel):
    metadata: StepMetadata
    context: dict = {}
    result: dict = {}
    errors: list = []


def sfn_handler(func):
    @functools.wraps(func)
    def wrapper(event, lambda_context):
        envelope = StepEnvelope(**event)
        bound = _log.bind(
            correlation_id=envelope.metadata.correlationId,
            step=func.__name__,
        )
        if _XRAY:
            xray.put_annotation("correlationId", envelope.metadata.correlationId)
        bound.info("step.start")
        try:
            result = func(envelope, lambda_context, bound)
            bound.info("step.success")
            return result
        except Exception as e:
            bound.error("step.error", error=str(e))
            raise
    return wrapper


@sfn_handler
def handler(envelope: StepEnvelope, lambda_context, log):
    exec_id = envelope.context["executionId"]
    log.info("gather_start", exec_id=exec_id)

    paginator = s3_client.get_paginator("list_objects_v2")
    keys = []
    for page in paginator.paginate(Bucket=BUCKET, Prefix=f"emails/{exec_id}/"):
        for obj in page.get("Contents", []):
            keys.append(obj["Key"])

    # Filenames are {epoch_ms}_{from_hash}.json — lexicographic sort = chronological.
    keys.sort()

    emails = []
    for key in keys:
        body = s3_client.get_object(Bucket=BUCKET, Key=key)["Body"].read()
        emails.append(json.loads(body))

    email_count = len(emails)
    log.info("gather_complete", exec_id=exec_id, email_count=email_count)

    if email_count == 0:
        return {
            "emailCount": 0,
            "firstEmailAt": "",
            "lastEmailAt": "",
            "timelineText": "(no emails collected)",
            "fullBodyText": "",
        }

    first_at = int(emails[0].get("received_at", 0))
    last_at = int(emails[-1].get("received_at", 0))

    timeline_lines = []
    for em in emails:
        received_at = int(em.get("received_at", first_at))
        minutes = max(0, int((received_at - first_at) / 60000))
        from_addr = em.get("from", "(unknown)")
        subject = em.get("subject", "(no subject)")
        timeline_lines.append(f"T+{minutes}m \u2014 From: {from_addr} | Subject: {subject}")

    return {
        "emailCount": email_count,
        "firstEmailAt": _fmt_ts(first_at),
        "lastEmailAt": _fmt_ts(last_at),
        "timelineText": "\n".join(timeline_lines),
        "fullBodyText": "\n---\n".join(em.get("body_text", "").strip() for em in emails),
    }


def _fmt_ts(epoch_ms: int) -> str:
    if not epoch_ms:
        return ""
    return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).isoformat()
