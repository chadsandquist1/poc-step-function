resource "aws_sfn_state_machine" "digest" {
  name     = "EmailDigest"
  role_arn = aws_iam_role.sfn.arn

  definition = jsonencode({
    Comment = "Email digest: wait for FINAL email (30m timeout) then send consolidated digest"
    StartAt = "WaitForFinal"
    States = {
      WaitForFinal = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke.waitForTaskToken"
        Parameters = {
          FunctionName      = aws_lambda_function.register_token.arn
          "Payload" = {
            "executionId.$" = "$.executionId"
            "taskToken.$"   = "$$.Task.Token"
          }
        }
        TimeoutSeconds = 1800
        Catch = [{
          ErrorEquals = ["States.Timeout"]
          ResultPath  = "$.timeoutError"
          Next        = "BuildAndSendDigest"
        }]
        Next = "BuildAndSendDigest"
      }
      BuildAndSendDigest = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = aws_lambda_function.build_and_send.arn
          "Payload.$"  = "$"
        }
        End = true
      }
    }
  })
}
