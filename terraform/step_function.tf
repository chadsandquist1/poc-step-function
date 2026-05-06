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
    Comment = "Email digest: wait for FINAL email (15m timeout) then send consolidated digest"
    StartAt = "WaitForFinal"
    States = {

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
        # Output from SendTaskSuccess is intentionally discarded — executionId is already in context.
        ResultPath     = null
        TimeoutSeconds = 900
        Retry = [{
          ErrorEquals    = ["Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.TooManyRequestsException"]
          MaxAttempts    = 3
          IntervalSeconds = 2
          BackoffRate    = 2
        }]
        Catch = [
          {
            ErrorEquals = ["States.Timeout"]
            ResultPath  = "$.timeoutError"
            Next        = "MergeTimeoutContext"
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
            isTimeout       = false
          }
        }
        Next = "BuildAndSendDigest"
      }

      MergeTimeoutContext = {
        Type = "Pass"
        Parameters = {
          "metadata.$" = "$.metadata"
          "errors.$"   = "$.errors"
          result       = {}
          context = {
            "executionId.$" = "$.context.executionId"
            isTimeout       = true
          }
        }
        Next = "BuildAndSendDigest"
      }

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
          ErrorEquals    = ["Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.TooManyRequestsException"]
          MaxAttempts    = 3
          IntervalSeconds = 2
          BackoffRate    = 2
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
