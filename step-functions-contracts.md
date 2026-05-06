# Step Functions: State Contracts, Brittleness & Logging

## Mandatory Envelope Contract

Every Step Function execution **must** start with this envelope shape. Never start
an execution with a bare payload (e.g. `{"executionId": "abc"}`).

```python
sfn.start_execution(
    stateMachineArn=STATE_MACHINE_ARN,
    input=json.dumps({
        "metadata": {
            "correlationId": "<business-key>",   # e.g. execId, caseId, documentId
            "initiatedAt": datetime.utcnow().isoformat(),
            "originator": "<source-system-or-user>",
            "traceId": "<xray-trace-id>"
        },
        "context": {},    # accumulated state — grows across steps
        "result":  {},    # current step output — cleared by each merge Pass
        "errors":  []     # non-fatal errors accumulated across steps
    })
)
```

`metadata` is **never mutated** after execution starts.  
`context` **accumulates** — every merge Pass adds to it, never removes.  
`result` is **ephemeral** — cleared by every merge Pass after promotion.  
`errors` **accumulates** — non-fatal errors are appended, never overwritten.

---

## Task State Rules

Every `Task` state **must** include all four of the following. No exceptions.

### 1. `Parameters` — Send a clean, intentional payload to the resource

Never pass `"Payload.$": "$"` (the full envelope). Send only what the Lambda needs.

```json
"Parameters": {
  "FunctionName": "MyLambda",
  "Payload": {
    "metadata.$": "$.metadata",
    "context.$":  "$.context"
  }
}
```

### 2. `ResultSelector` — Unwrap the resource response before grafting

Strip Lambda's wire format (`StatusCode`, `SdkHttpMetadata`, `Payload` wrapper)
so downstream states never need to know about it.

```json
"ResultSelector": {
  "extractedText.$": "$.Payload.extractedText",
  "confidence.$":    "$.Payload.confidence"
}
```

For direct SDK integrations (DynamoDB, SQS, etc.) unwrap type descriptors here:
```json
"ResultSelector": {
  "taskToken.$": "$.Item.taskToken.S",
  "status.$":    "$.Item.status.S"
}
```

### 3. `ResultPath` — Park output without destroying the envelope

Always park at `$.result`. Never omit `ResultPath` (omitting it replaces the
entire envelope with the Task output).

```json
"ResultPath": "$.result"
```

Use `"ResultPath": null` only for fire-and-forget side-effects where the output
is intentionally discarded.

### 4. `Catch` — Route every error, never let failures bubble up raw

Every Task must catch at minimum `States.ALL`. Specific error types should be
caught first and routed to appropriate handlers.

```json
"Catch": [
  {
    "ErrorEquals": ["ValidationError"],
    "ResultPath": "$.error",
    "Next": "HandleValidationError"
  },
  {
    "ErrorEquals": ["States.ALL"],
    "ResultPath": "$.error",
    "Next": "HandleUnexpectedError"
  }
]
```

Always use `"ResultPath": "$.error"` on Catch so the error detail is available
to the error handler without destroying the envelope.

### `Retry` — Add a retry budget to every Task

```json
"Retry": [
  {
    "ErrorEquals": ["States.TaskFailed", "Lambda.ServiceException", "Lambda.TooManyRequestsException"],
    "MaxAttempts": 3,
    "IntervalSeconds": 2,
    "BackoffRate": 2
  }
]
```

---

## Pass State Rules (Merge States)

Every Task state **must** be followed by a merge Pass state unless it is the
terminal state. The merge Pass promotes `$.result` into `$.context` and clears
`$.result` for the next Task.

```json
"MergeMyTaskResult": {
  "Type": "Pass",
  "Parameters": {
    "metadata.$": "$.metadata",
    "errors.$":   "$.errors",
    "result":     {},
    "context": {
      "previousField.$":  "$.context.previousField",
      "newField.$":       "$.result.newField"
    }
  },
  "Next": "NextTask"
}
```

**Rules for Pass states:**
- Always explicitly list every `$.context` field you want to keep — unlisted fields are silently dropped
- Always reset `"result": {}` — never carry stale result into the next Task
- Never reference `$.result.Payload.*` — that means `ResultSelector` is missing on the preceding Task
- Never reference fields that were not present on the incoming envelope — Step Functions will throw at runtime

---

## Lambda Handler Rules

Every Lambda invoked by Step Functions **must** validate the envelope at entry
using Pydantic. Fail fast with a clear error rather than a `KeyError` three
steps later.

```python
from pydantic import BaseModel
from typing import Any

class StepMetadata(BaseModel):
    correlationId: str
    initiatedAt: str
    originator: str
    traceId: str = ""

class StepEnvelope(BaseModel):
    metadata: StepMetadata
    context: dict[str, Any] = {}
    result: dict[str, Any] = {}
    errors: list[dict] = []

def handler(event, lambda_context):
    envelope = StepEnvelope(**event)   # raises ValidationError on bad shape
    # ... work only on envelope.context
    return { "myField": "myValue" }    # return only your slice — not the full envelope
```

Lambda **returns only its own slice** — not the full envelope. The envelope is
reconstructed by the merge Pass state.

---

## Logging Rules

### Structured Logging Decorator

All Step Functions Lambdas must use the `sfn_handler` decorator. Never use bare
`print()` or unstructured `logging.info()`.

```python
import structlog, functools
from pydantic import BaseModel

log = structlog.get_logger()

def sfn_handler(func):
    @functools.wraps(func)
    def wrapper(event, context):
        envelope = StepEnvelope(**event)
        bound = log.bind(
            correlation_id=envelope.metadata.correlationId,
            step=func.__name__,
        )
        bound.info("step.start")
        try:
            result = func(envelope, context, bound)
            bound.info("step.success")
            return result
        except Exception as e:
            bound.error("step.error", error=str(e))
            raise
    return wrapper

@sfn_handler
def handler(envelope: StepEnvelope, ctx, log):
    ...
```

### X-Ray Propagation

Every Lambda must annotate X-Ray with the `correlationId` so CloudWatch Logs,
X-Ray traces, and DynamoDB audit records share a single traceable key.

```python
import aws_xray_sdk.core as xray

xray.put_annotation("correlationId", envelope.metadata.correlationId)
```

### Terraform: CloudWatch Logging on the State Machine

Every state machine must include a `logging_configuration` block. `ALL` level is
required in non-production. `ERROR` is acceptable in production if cost is a
concern, but `include_execution_data` must remain `true`.

```hcl
resource "aws_cloudwatch_log_group" "sfn" {
  name              = "/aws/states/${var.state_machine_name}"
  retention_in_days = 14
}

resource "aws_sfn_state_machine" "main" {
  # ... existing config ...

  logging_configuration {
    level                  = "ALL"
    include_execution_data = true
    log_destination        = "${aws_cloudwatch_log_group.sfn.arn}:*"
  }
}
```

---

## `$` and `$$` Reference Rules

| Syntax | Meaning | Example |
|--------|---------|---------|
| `$` | Current state input (your envelope) | `"$.context.documentId"` |
| `$$` | Step Functions runtime metadata | `"$$.Task.Token"`, `"$$.Execution.Id"` |
| `.$` suffix on key | Evaluate value as JSONPath, not a literal string | `"correlationId.$": "$.metadata.correlationId"` |

**Never omit `.$`** when the value is a reference. Step Functions will silently
pass the literal string `"$.metadata.correlationId"` to your Lambda instead of
the actual value — no error is thrown.

Static values use plain keys with no `.$`:
```json
"stage": "production"
```

Dynamic values always use `.$`:
```json
"correlationId.$": "$.metadata.correlationId"
```

---

## The Five Transform Order (per Task state)

These execute in order. Understanding the sequence prevents misuse:

```
1. InputPath       Filter incoming envelope before state runs     (default: $)
2. Parameters      Reshape what is sent TO the resource
3. ResultSelector  Reshape what comes BACK from the resource
4. ResultPath      Graft shaped result onto the envelope
5. OutputPath      Filter final envelope before passing to next state (default: $)
```

`ResultSelector` scope is the **raw resource output only** — it cannot see `$.context`.  
`Parameters` (in a Pass state) scope is the **full envelope** — it can see everything.

---

## Complete Task + Merge Pattern (Reference)

```json
"MyTask": {
  "Type": "Task",
  "Resource": "arn:aws:states:::lambda:invoke",
  "Parameters": {
    "FunctionName": "MyLambda",
    "Payload": {
      "metadata.$": "$.metadata",
      "context.$":  "$.context"
    }
  },
  "ResultSelector": {
    "myField.$":    "$.Payload.myField",
    "myMetric.$":   "$.Payload.myMetric"
  },
  "ResultPath": "$.result",
  "Retry": [
    {
      "ErrorEquals": ["States.ALL"],
      "MaxAttempts": 3,
      "IntervalSeconds": 2,
      "BackoffRate": 2
    }
  ],
  "Catch": [
    {
      "ErrorEquals": ["States.ALL"],
      "ResultPath": "$.error",
      "Next": "HandleError"
    }
  ],
  "Next": "MergeMyTaskResult"
},

"MergeMyTaskResult": {
  "Type": "Pass",
  "Parameters": {
    "metadata.$": "$.metadata",
    "errors.$":   "$.errors",
    "result":     {},
    "context": {
      "previousField.$": "$.context.previousField",
      "myField.$":       "$.result.myField",
      "myMetric.$":      "$.result.myMetric"
    }
  },
  "Next": "NextTask"
}
```

---

## Checklist: Before Merging Any Step Function Change

- [ ] Execution `start_execution` input includes `metadata`, `context`, `result`, `errors`
- [ ] Every Task has `Parameters` sending only needed fields
- [ ] Every Task has `ResultSelector` unwrapping the resource response
- [ ] Every Task has `ResultPath: "$.result"`
- [ ] Every Task has `Catch` with at minimum `States.ALL`
- [ ] Every Task has `Retry`
- [ ] Every Task is followed by a merge Pass (unless terminal)
- [ ] Every merge Pass explicitly lists all `$.context` fields to preserve
- [ ] Every merge Pass resets `"result": {}`
- [ ] No merge Pass references `$.result.Payload.*`
- [ ] Every Lambda validates with `StepEnvelope` Pydantic model
- [ ] Every Lambda uses `sfn_handler` decorator
- [ ] Every Lambda annotates X-Ray with `correlationId`
- [ ] State machine Terraform has `logging_configuration` block
