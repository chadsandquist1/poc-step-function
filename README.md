# Email Digest Step Function POC

A serverless email consolidation system built on AWS SES, S3, DynamoDB, and Step Functions.

## How It Works

1. Send any email to the configured `bot_email` address → receive an auto-reply with an `exec-XXXXXXXX` ID
2. Send follow-up emails by replying or including the exec ID in the subject
3. Send a final email with subject `{exec-id} - FINAL` to trigger digest delivery
4. Receive a consolidated digest of all collected emails sent to the original sender

If no FINAL email arrives within **30 minutes**, the original sender receives a "never followed up" notice.

## Deployment

### Prerequisites
- AWS CLI configured
- Terraform >= 1.5
- SES-verified domain or email identity (SES inbound requires `us-east-1`, `us-west-2`, or `eu-west-1`)
- SES must be out of sandbox (or recipient addresses must be verified)

### Deploy

```bash
cd terraform
terraform init
terraform apply -var="bot_email=digest-bot@yourdomain.com"
```

### Variables

| Variable | Default | Description |
|---|---|---|
| `aws_region` | `us-east-1` | AWS region |
| `bot_email` | _(required)_ | Verified SES address for sending/receiving |
| `ses_rule_set_name` | `email-digest-poc` | SES receipt rule set name |

## Running Tests

```bash
pip install -r requirements-test.txt
pytest tests/ -v
```

## Project Layout

```
/
├── claude.md                     # architecture spec
├── README.md
├── requirements-test.txt
├── terraform/
│   ├── main.tf
│   ├── variables.tf
│   ├── ses.tf
│   ├── s3.tf
│   ├── dynamodb.tf
│   ├── iam.tf
│   ├── lambdas.tf
│   ├── step_function.tf
│   └── .gitignore
├── src/
│   ├── email_ingest/
│   │   ├── handler.py
│   │   └── mime_parser.py
│   ├── register_token/
│   │   └── handler.py
│   └── build_and_send/
│       └── handler.py
└── tests/
    ├── conftest.py
    ├── test_parser.py
    ├── test_ingest_routing.py
    └── test_build_digest.py
```

---

## Things to Consider

These questions were raised during design and intentionally deferred for this POC. Revisit them before production use.

### 1. Race Condition: FINAL before task token is registered
The Step Function starts → invokes `RegisterTaskToken` → token written to DDB. If a FINAL email arrives in that small window before the token is persisted, `EmailIngest` reads `taskToken = None` and silently drops the request. The sender would have to resend the FINAL email. In production, consider a retry loop with exponential backoff, or a DynamoDB Streams-triggered approach.

### 2. SES at-least-once delivery
SES can deliver the same inbound email more than once. A duplicate first email will start two separate Step Function executions with two different exec IDs. The original sender receives two reply emails. Mitigation: add a conditional put on the inbound `Message-ID` before starting execution.

### 3. Digest recipient scope
Currently the digest is sent only to the originator (first sender). If multiple contributors sent follow-up emails, they receive no output. Consider CC'ing all unique senders in the thread.

### 4. Cross-sender thread injection
Anyone who learns an `exec-XXXXXXXX` ID can inject emails into that thread by including the ID in the subject. The injected content will appear in the final digest. Mitigation: restrict accepted senders to those who have previously participated in the thread.

### 5. Multiple FINAL emails
Sending two FINAL emails causes the second `send_task_success` call to fail with `InvalidToken` or `TaskTimedOut` (the execution is already complete). The error is caught and logged but the second email is still stored in S3. The digest is only sent once.

### 6. SES Message-ID format
This implementation uses the AWS internal message ID returned by `ses.send_email()` (e.g. `0102018f3a1b2c3d`) as the key for `In-Reply-To` reverse lookups. Some email clients may format the `In-Reply-To` header differently (e.g. with angle brackets or a domain suffix like `@email.amazonses.com`). Test end-to-end with your actual email client before relying on `In-Reply-To` matching.

### 7. Orphaned data after timeout
When the 30-minute timeout fires, S3 still holds all collected emails and DDB still has the record. The lifecycle policy expires S3 objects after 30 days, but DDB records persist indefinitely. Consider a TTL attribute on DDB items or a cleanup step in the timeout notification path.

### 8. DynamoDB read consistency
`EmailIngest` uses strongly-consistent reads when looking up task tokens. This adds latency (~double) and costs twice as many read capacity units. For high volume, consider eventually-consistent reads with an accept-duplicate-FINAL policy.

### 9. Follow-up acknowledgment
Follow-up emails receive no acknowledgment — contributors have no confirmation their message was received. If they mistype the exec ID, their email silently starts a new thread. Consider replying to follow-ups with a brief confirmation.

### 10. Lambda payload size limit
SES passes inbound email metadata to Lambda; the raw body is fetched separately from S3. However, the S3 object may still be large if the email has no attachments but a long body. This implementation does not enforce a size limit on body extraction. Emails over ~6 MB (Lambda response limit) in the raw MIME could cause issues.

### 11. Forwarded email thread joining
A forwarded email preserves the original `Message-ID` in the body but sets a new outer `Message-ID` header. If someone forwards the bot's reply, the `In-Reply-To` points to the bot's original message ID, silently joining the thread from a different sender's mailbox.

### 12. Email count accuracy
`emailCount` in DynamoDB is incremented only on follow-up emails — not on the initial email or the FINAL email. The digest displays the count from S3 object listing (accurate), not from this attribute. `emailCount` is cosmetic only.
