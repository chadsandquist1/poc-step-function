import json
import os
import re
import time
import uuid

import boto3

from mime_parser import from_hash, parse_mime

s3_client = boto3.client("s3")
ddb = boto3.resource("dynamodb")
sfn = boto3.client("stepfunctions")
ses = boto3.client("ses")

TABLE = ddb.Table(os.environ["DYNAMODB_TABLE"])
BUCKET = os.environ["S3_BUCKET"]
SFN_ARN = os.environ["SFN_ARN"]
BOT_EMAIL = os.environ["BOT_EMAIL"]

EXEC_ID_RE = re.compile(r"\b(exec-[a-f0-9]+)\b", re.IGNORECASE)


def handler(event, context):
    record = event["Records"][0]
    message_id = record["ses"]["mail"]["messageId"]

    raw = s3_client.get_object(Bucket=BUCKET, Key=f"incoming/{message_id}")["Body"].read()
    parsed = parse_mime(raw)
    parsed["received_at"] = int(time.time() * 1000)

    subject = parsed["subject"] or ""
    in_reply_to = parsed["in_reply_to"] or ""

    exec_id = _resolve_exec_id(subject, in_reply_to)

    if exec_id is None:
        _handle_new(parsed)
    elif "FINAL" in subject.upper():
        _handle_final(exec_id, parsed)
    else:
        _handle_followup(exec_id, parsed)


def _resolve_exec_id(subject: str, in_reply_to: str):
    m = EXEC_ID_RE.search(subject)
    if m:
        return m.group(1).lower()

    if in_reply_to:
        resp = TABLE.get_item(
            Key={"pk": f"MSGID#{in_reply_to}"},
            ConsistentRead=True,
        )
        item = resp.get("Item")
        if item:
            return item["executionId"]

    return None


def _handle_new(parsed: dict):
    exec_id = "exec-" + uuid.uuid4().hex[:8]

    _store_email_s3(exec_id, parsed)

    TABLE.put_item(Item={
        "pk": f"EXEC#{exec_id}",
        "executionId": exec_id,
        "originator": parsed["from"],
        "status": "COLLECTING",
        "sfnExecutionArn": "",
        "emailCount": 1,
        "createdAt": parsed["received_at"],
        "updatedAt": parsed["received_at"],
    })

    sfn_resp = sfn.start_execution(
        stateMachineArn=SFN_ARN,
        name=exec_id,
        input=json.dumps({"executionId": exec_id}),
    )

    TABLE.update_item(
        Key={"pk": f"EXEC#{exec_id}"},
        UpdateExpression="SET sfnExecutionArn = :arn",
        ExpressionAttributeValues={":arn": sfn_resp["executionArn"]},
    )

    reply_resp = ses.send_email(
        Source=BOT_EMAIL,
        Destination={"ToAddresses": [parsed["from"]]},
        Message={
            "Subject": {"Data": f"[{exec_id}] Re: {parsed['subject']}"},
            "Body": {"Text": {"Data": (
                f"Your email has been received.\n\n"
                f"Thread ID: {exec_id}\n\n"
                f"To add follow-up emails, reply to this message or include "
                f"'{exec_id}' anywhere in the subject line.\n\n"
                f"To finalize and receive your digest, send an email with subject:\n"
                f"  {exec_id} - FINAL\n\n"
                f"This thread expires in 15 minutes if no FINAL email is received."
            )}},
        },
        ReplyToAddresses=[BOT_EMAIL],
    )

    out_msg_id = reply_resp["MessageId"]

    TABLE.put_item(Item={
        "pk": f"MSGID#{out_msg_id}",
        "executionId": exec_id,
    })

    TABLE.update_item(
        Key={"pk": f"EXEC#{exec_id}"},
        UpdateExpression="ADD messageIds :mid",
        ExpressionAttributeValues={":mid": {out_msg_id}},
    )


def _handle_final(exec_id: str, parsed: dict):
    _store_email_s3(exec_id, parsed)

    resp = TABLE.get_item(
        Key={"pk": f"EXEC#{exec_id}"},
        ConsistentRead=True,
    )
    item = resp.get("Item")
    if not item:
        print(f"[WARN] No execution record for {exec_id}; ignoring FINAL.")
        return

    task_token = item.get("taskToken")
    if not task_token:
        print(f"[WARN] No task token for {exec_id}; RegisterTaskToken may not have run yet.")
        return

    try:
        sfn.send_task_success(
            taskToken=task_token,
            output=json.dumps({"executionId": exec_id}),
        )
    except sfn.exceptions.InvalidToken:
        print(f"[WARN] InvalidToken for {exec_id}; execution already completed or token expired.")
    except sfn.exceptions.TaskTimedOut:
        print(f"[WARN] TaskTimedOut for {exec_id}; execution timed out before FINAL arrived.")

    TABLE.update_item(
        Key={"pk": f"EXEC#{exec_id}"},
        UpdateExpression="SET #st = :s, updatedAt = :now",
        ExpressionAttributeNames={"#st": "status"},
        ExpressionAttributeValues={
            ":s": "SENT",
            ":now": int(time.time() * 1000),
        },
    )


def _handle_followup(exec_id: str, parsed: dict):
    _store_email_s3(exec_id, parsed)

    TABLE.update_item(
        Key={"pk": f"EXEC#{exec_id}"},
        UpdateExpression="ADD emailCount :one SET updatedAt = :now",
        ExpressionAttributeValues={
            ":one": 1,
            ":now": int(time.time() * 1000),
        },
    )


def _store_email_s3(exec_id: str, parsed: dict):
    key = f"emails/{exec_id}/{parsed['received_at']}_{from_hash(parsed['from'])}.json"
    s3_client.put_object(
        Bucket=BUCKET,
        Key=key,
        Body=json.dumps(parsed),
        ContentType="application/json",
    )
