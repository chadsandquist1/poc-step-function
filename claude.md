# Email Digest Step Function POC

## Overview
A serverless email consolidation system that:
- Receives emails via SES, stores them in S3 (no attachments)
- Starts a Step Function execution per "thread"
- Replies to sender with the execution ID
- Joins follow-up emails (matched by execution ID in subject OR by `In-Reply-To` header)
- Sends consolidated digest when an email arrives with subject `{execution_id} - FINAL`

## Confirmed Decisions
| # | Decision |
|---|---|
| 1 | **Simplest ingestion**: SES Receipt Rule → Lambda directly (no SNS/SQS) |
| 2 | **Digest trigger**: email with subject `{execution_id} - FINAL` |
| 3 | **Digest scope**: one digest per execution ID, all related emails |
| 4 | **Follow-up detection**: BOTH execution ID in subject AND reply (`In-Reply-To` header lookup) |

## Architecture

```
                    ┌──────────────┐
   Inbound email →  │ SES Receipt  │
                    │     Rule     │
                    └──────┬───────┘
                           ▼
                    ┌──────────────────┐         ┌──────────┐
                    │ EmailIngest λ    │ ──────▶ │ S3 (raw) │
                    │ - parse MIME     │         └──────────┘
                    │ - strip attach.  │
                    │ - resolve execId │ ──────▶ ┌──────────┐
                    │ - route          │         │ DynamoDB │
                    └──┬─────────┬─────┘         └──────────┘
                       │         │
       NEW email       │         │  FOLLOW-UP w/ "FINAL"
                       ▼         ▼
              ┌─────────────┐  ┌────────────────────────┐
              │ StartExec + │  │ SendTaskSuccess(token) │
              │ reply w/ ID │  └─────────┬──────────────┘
              └──────┬──────┘            │
                     ▼                   │
        ┌────────────────────────┐       │
        │   Step Function (Std)  │       │
        │  ┌──────────────────┐  │       │
        │  │ Task:            │  │       │
        │  │ WaitForToken     │◀─┼───────┘
        │  │ (timeout 7d)     │  │
        │  └────────┬─────────┘  │
        │           ▼            │
        │  ┌──────────────────┐  │       ┌──────────┐
        │  │ BuildAndSend λ   │ ─┼─────▶ │ SES Send │
        │  └──────────────────┘  │       └──────────┘
        └────────────────────────┘
```

## AWS Resources (minimum set)
- **1** SES verified domain/identity + **1** receipt rule set
- **1** S3 bucket: `email-digest-poc-{account}` (versioned, lifecycle 30d)
- **1** DynamoDB table: `EmailDigestExecutions` (PK only)
- **3** Lambdas: `EmailIngest`, `RegisterTaskToken`, `BuildAndSendDigest`
- **1** Step Function (Standard)
- IAM roles (one per Lambda + one for SFN)

## Domain Objects

### DynamoDB: `EmailDigestExecutions`
Single-table design. Partition Key only (PK).

| Attribute | Type | Notes |
|---|---|---|
| `pk` | S | `EXEC#{execId}` or `MSGID#{messageId}` |
| `executionId` | S | e.g. `exec-7f3a` (short, subject-friendly) |
| `originator` | S | First sender's email |
| `status` | S | `COLLECTING` \| `SENT` |
| `taskToken` | S | Step Function callback token (only on `EXEC#`) |
| `sfnExecutionArn` | S | |
| `messageIds` | SS | Set of outbound `Message-ID`s we've sent (for reply matching) |
| `emailCount` | N | |
| `createdAt` / `updatedAt` | N | epoch ms |

`MSGID#{messageId}` items act as a reverse-lookup → `executionId` for `In-Reply-To` matching.

### S3 Layout
```
s3://email-digest-poc-{account}/
└── emails/{executionId}/{epoch_ms}_{from_hash}.json
```
Each object: `{from, to, subject, body_text, received_at}`. **Attachments dropped during MIME parse.**

## Step Function State Machine
```json
{
  "Comment": "Email digest with token-based wait",
  "StartAt": "WaitForFinal",
  "States": {
    "WaitForFinal": {
      "Type": "Task",
      "Resource": "arn:aws:states:::lambda:invoke.waitForTaskToken",
      "Parameters": {
        "FunctionName": "RegisterTaskToken",
        "Payload": {
          "executionId.$": "$.executionId",
          "taskToken.$": "$$.Task.Token"
        }
      },
      "TimeoutSeconds": 604800,
      "Next": "BuildAndSendDigest"
    },
    "BuildAndSendDigest": {
      "Type": "Task",
      "Resource": "arn:aws:states:::lambda:invoke",
      "Parameters": {
        "FunctionName": "BuildAndSendDigest",
        "Payload.$": "$"
      },
      "End": true
    }
  }
}
```

> **Why `.waitForTaskToken` instead of a literal `Wait` state?** A `Wait` state in SFN is strictly time-based and not interruptible by external events. Since the digest trigger is an inbound `FINAL` email, the proper pattern is `.waitForTaskToken` — the execution stays paused until `SendTaskSuccess` is called from `EmailIngest`. The 7-day timeout is a safety net.
>
> If you'd prefer a literal `Wait` for POC purity, we can prepend a short fixed `Wait` (e.g. 1h) as a "settle" period before the token task — flag it and I'll add it.

### `RegisterTaskToken` Lambda
Tiny helper — persists `taskToken` to the `EXEC#` row in DynamoDB, then returns. Required because `.waitForTaskToken` needs *something* to record the token before the state machine can be resumed.

## EmailIngest Routing Logic
```
Parse MIME → from, subject, body_text, message_id, in_reply_to

execId = None
if match("^(exec-[a-z0-9]+)", subject):
    execId = matched_group
elif in_reply_to and lookup MSGID#{in_reply_to} → found:
    execId = found.executionId

if execId is None:
    # NEW thread
    execId = generate()  # short id like exec-7f3a
    store_email_s3(execId, parsed)
    ddb.put EXEC#{execId} (status=COLLECTING)
    sfn.start_execution(input={executionId: execId})
    out_msg_id = ses.send_reply(subject="[{execId}] Re: {original}")
    ddb.put MSGID#{out_msg_id} → execId
    ddb.update EXEC#{execId} ADD messageIds {out_msg_id}

elif "FINAL" in subject.upper():
    store_email_s3(execId, parsed)
    token = ddb.get(EXEC#{execId}).taskToken
    sfn.send_task_success(token, output={executionId: execId})

else:
    # follow-up
    store_email_s3(execId, parsed)
    ddb.update EXEC#{execId} emailCount += 1
```

## Reply Matching Detail
When we send the initial reply, we capture the SES `MessageId` and write `MSGID#{id} → executionId`. Inbound emails carry `In-Reply-To: <our-message-id>` — that bridges replies that don't include the exec ID in the subject.

## Project Layout
```
/
├── claude.md                         # this file
├── README.md
├── terraform/
│   ├── main.tf
│   ├── variables.tf
│   ├── ses.tf
│   ├── s3.tf
│   ├── dynamodb.tf
│   ├── iam.tf
│   ├── lambdas.tf
│   ├── step_function.tf
│   └── .gitignore                    # *.tfstate, *.tfvars
├── src/
│   ├── email_ingest/
│   │   ├── handler.py
│   │   └── parser.py                 # MIME + attachment strip
│   ├── register_token/
│   │   └── handler.py
│   └── build_and_send/
│       └── handler.py                # gather S3 + format + SES send
└── tests/
    ├── test_parser.py
    ├── test_ingest_routing.py
    └── test_build_digest.py
```

## Security / Hygiene
- No secrets, no `*.tfstate`, no `*.tfvars` in git — `.gitignore` enforced
- All IAM roles least-privilege (per-Lambda)
- S3 bucket: block public access, SSE-S3, versioning
- SES sandbox check noted in README (verified identity required)
- Sender domain verified in SES; SPF/DKIM aligned for reply deliverability

## Open Questions Before I Build
1. **Domain**: SES-verified domain ready, or use a `var.bot_email` placeholder (e.g. `digest-bot@yourdomain.com`)?
2. **Region**: `us-east-1` default? (SES inbound is region-limited.)
3. **Wait flavor**: `.waitForTaskToken` only, or prepend a literal `Wait` for POC demo flair?
4. **Follow-up acks**: Reply to every follow-up, or stay silent until the FINAL digest?

Reply with answers or "go with defaults" and I'll generate Terraform + Lambdas + tests.

---
**Status**: Plan finalized, awaiting build approval
