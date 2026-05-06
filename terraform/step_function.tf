resource "aws_cloudwatch_log_group" "sfn" {
  name              = "/aws/states/EmailDigest"
  retention_in_days = 14
}

resource "aws_sfn_state_machine" "digest" {
  name     = "EmailDigest"
  role_arn = aws_iam_role.sfn.arn

  logging_configuration {
    level                  = "ALL"
    include_execution_data = true
    log_destination        = "${aws_cloudwatch_log_group.sfn.arn}:*"
  }

  definition = jsonencode({
    Comment = "Email digest: wait for FINAL → gather emails → AI summarize → send digest; timeout → send expiry notice"
    StartAt = "WaitForFinal"
    States = {

      # ── Wait for the sender to signal FINAL ──────────────────────────────────
      WaitForFinal = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke.waitForTaskToken"
        Parameters = {
          FunctionName = aws_lambda_function.register_token.arn
          Payload = {
            "metadata.$"    = "$.metadata"
            "executionId.$" = "$.context.executionId"
            "taskToken.$"   = "$$.Task.Token"
          }
        }
        ResultPath     = "$.result"
        TimeoutSeconds = 900
        Retry = [{
          ErrorEquals     = ["Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.TooManyRequestsException"]
          MaxAttempts     = 3
          IntervalSeconds = 2
          BackoffRate     = 2
        }]
        Catch = [
          {
            # 15-minute wait expired with no FINAL email — send expiry notice.
            ErrorEquals = ["States.HeartbeatTimeout", "States.Timeout"]
            ResultPath  = "$.error"
            Next        = "SendTimeoutNotice"
          },
          {
            ErrorEquals = ["States.ALL"]
            ResultPath  = "$.error"
            Next        = "HandleError"
          }
        ]
        Next = "MergeWaitResult"
      }

      MergeWaitResult = {
        Type = "Pass"
        Parameters = {
          "metadata.$" = "$.metadata"
          "errors.$"   = "$.errors"
          result       = {}
          context = {
            "executionId.$" = "$.context.executionId"
          }
        }
        Next = "GatherEmailsForSummary"
      }

      # ── Gather all thread emails from S3 ─────────────────────────────────────
      GatherEmailsForSummary = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = aws_lambda_function.gather_emails.arn
          Payload = {
            "metadata.$" = "$.metadata"
            "context.$"  = "$.context"
          }
        }
        ResultSelector = {
          "emailCount.$"   = "$.Payload.emailCount"
          "firstEmailAt.$" = "$.Payload.firstEmailAt"
          "lastEmailAt.$"  = "$.Payload.lastEmailAt"
          "timelineText.$" = "$.Payload.timelineText"
          "fullBodyText.$" = "$.Payload.fullBodyText"
        }
        ResultPath = "$.result"
        Retry = [{
          ErrorEquals     = ["States.ALL"]
          MaxAttempts     = 2
          IntervalSeconds = 5
          BackoffRate     = 2
        }]
        Catch = [{
          ErrorEquals = ["States.ALL"]
          ResultPath  = "$.error"
          Next        = "HandleError"
        }]
        Next = "MergeGatherResult"
      }

      MergeGatherResult = {
        Type = "Pass"
        Parameters = {
          "metadata.$" = "$.metadata"
          "errors.$"   = "$.errors"
          result       = {}
          context = {
            "executionId.$"  = "$.context.executionId"
            "emailCount.$"   = "$.result.emailCount"
            "firstEmailAt.$" = "$.result.firstEmailAt"
            "lastEmailAt.$"  = "$.result.lastEmailAt"
            "timelineText.$" = "$.result.timelineText"
            "fullBodyText.$" = "$.result.fullBodyText"
          }
        }
        Next = "SummarizeDigest"
      }

      # ── Direct Bedrock call — no Lambda wrapper ───────────────────────────────
      SummarizeDigest = {
        Type     = "Task"
        Resource = "arn:aws:states:::bedrock:invokeModel"
        Parameters = {
          ModelId     = "anthropic.claude-haiku-4-5-20251001-v1:0"
          ContentType = "application/json"
          Accept      = "application/json"
          Body = {
            anthropic_version = "bedrock-2023-05-31"
            max_tokens        = 512
            system            = "You are a concise email thread summarizer. Output only valid JSON with no markdown, no code fences, no extra text."
            messages = [{
              role        = "user"
              "content.$" = "States.Format('Summarize this email thread. Return a JSON object with exactly two keys: \"summary\" (a 2-3 sentence string) and \"keyPoints\" (an array of short strings).\n\nTimeline:\n{}\n\nFull content:\n{}', $.context.timelineText, $.context.fullBodyText)"
            }]
          }
        }
        ResultSelector = {
          "rawResponse.$"  = "$.Body.content[0].text"
          "inputTokens.$"  = "$.Body.usage.input_tokens"
          "outputTokens.$" = "$.Body.usage.output_tokens"
        }
        ResultPath = "$.result"
        Retry = [{
          ErrorEquals     = ["Bedrock.ThrottlingException", "Bedrock.ServiceUnavailableException"]
          MaxAttempts     = 3
          IntervalSeconds = 5
          BackoffRate     = 2
        }]
        Catch = [{
          # Any Bedrock failure (quota, malformed response, etc.) — skip summary gracefully.
          ErrorEquals = ["States.ALL"]
          ResultPath  = "$.error"
          Next        = "MergeSummarizeFallback"
        }]
        Next = "MergeDigestSummary"
      }

      # Bedrock failed — carry context forward without a summary.
      MergeSummarizeFallback = {
        Type = "Pass"
        Parameters = {
          "metadata.$" = "$.metadata"
          "errors.$"   = "$.errors"
          result       = {}
          context = {
            "executionId.$"  = "$.context.executionId"
            "emailCount.$"   = "$.context.emailCount"
            "firstEmailAt.$" = "$.context.firstEmailAt"
            "lastEmailAt.$"  = "$.context.lastEmailAt"
            "timelineText.$" = "$.context.timelineText"
            "fullBodyText.$" = "$.context.fullBodyText"
          }
        }
        Next = "BuildAndSendDigest"
      }

      # Bedrock succeeded — promote rawResponse string into context.
      # BuildAndSendDigest parses summaryJson with json.loads + try/except
      # so malformed Claude responses are handled gracefully there too.
      MergeDigestSummary = {
        Type = "Pass"
        Parameters = {
          "metadata.$" = "$.metadata"
          "errors.$"   = "$.errors"
          result       = {}
          context = {
            "executionId.$"  = "$.context.executionId"
            "emailCount.$"   = "$.context.emailCount"
            "firstEmailAt.$" = "$.context.firstEmailAt"
            "lastEmailAt.$"  = "$.context.lastEmailAt"
            "timelineText.$" = "$.context.timelineText"
            "fullBodyText.$" = "$.context.fullBodyText"
            "summaryJson.$"  = "$.result.rawResponse"
          }
        }
        Next = "BuildAndSendDigest"
      }

      # ── Send the final digest email ───────────────────────────────────────────
      BuildAndSendDigest = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = aws_lambda_function.build_and_send.arn
          Payload = {
            "metadata.$" = "$.metadata"
            "context.$"  = "$.context"
          }
        }
        ResultSelector = {
          "statusCode.$" = "$.StatusCode"
        }
        ResultPath = "$.result"
        Retry = [{
          ErrorEquals     = ["Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.TooManyRequestsException"]
          MaxAttempts     = 3
          IntervalSeconds = 2
          BackoffRate     = 2
        }]
        Catch = [{
          ErrorEquals = ["States.ALL"]
          ResultPath  = "$.error"
          Next        = "HandleError"
        }]
        End = true
      }

      # ── Timeout path: 15-minute wait expired, no FINAL received ──────────────
      SendTimeoutNotice = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = aws_lambda_function.build_and_send.arn
          Payload = {
            "metadata.$" = "$.metadata"
            context = {
              "executionId.$" = "$.context.executionId"
              isTimeout       = true
            }
          }
        }
        ResultSelector = {
          "statusCode.$" = "$.StatusCode"
        }
        ResultPath = "$.result"
        Retry = [{
          ErrorEquals     = ["Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.TooManyRequestsException"]
          MaxAttempts     = 3
          IntervalSeconds = 2
          BackoffRate     = 2
        }]
        Catch = [{
          ErrorEquals = ["States.ALL"]
          ResultPath  = "$.error"
          Next        = "HandleError"
        }]
        End = true
      }

      HandleError = {
        Type      = "Fail"
        ErrorPath = "$.error.Error"
        CausePath = "$.error.Cause"
      }
    }
  })
}
