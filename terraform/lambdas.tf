data "archive_file" "email_ingest" {
  type        = "zip"
  source_dir  = "${path.module}/../src/email_ingest"
  output_path = "${path.module}/.builds/email_ingest.zip"
}

data "archive_file" "register_token" {
  type        = "zip"
  source_dir  = "${path.module}/../src/register_token"
  output_path = "${path.module}/.builds/register_token.zip"
}

data "archive_file" "build_and_send" {
  type        = "zip"
  source_dir  = "${path.module}/../src/build_and_send"
  output_path = "${path.module}/.builds/build_and_send.zip"
}

resource "aws_lambda_function" "email_ingest" {
  function_name    = "EmailIngest"
  role             = aws_iam_role.email_ingest.arn
  handler          = "handler.handler"
  runtime          = "python3.12"
  filename         = data.archive_file.email_ingest.output_path
  source_code_hash = data.archive_file.email_ingest.output_base64sha256
  timeout          = 30

  environment {
    variables = {
      DYNAMODB_TABLE = aws_dynamodb_table.executions.name
      S3_BUCKET      = aws_s3_bucket.emails.bucket
      SFN_ARN        = aws_sfn_state_machine.digest.arn
      BOT_EMAIL      = var.bot_email
    }
  }
}

resource "aws_lambda_function" "register_token" {
  function_name    = "RegisterTaskToken"
  role             = aws_iam_role.register_token.arn
  handler          = "handler.handler"
  runtime          = "python3.12"
  filename         = data.archive_file.register_token.output_path
  source_code_hash = data.archive_file.register_token.output_base64sha256
  timeout          = 10

  environment {
    variables = {
      DYNAMODB_TABLE = aws_dynamodb_table.executions.name
    }
  }
}

resource "aws_lambda_function" "build_and_send" {
  function_name    = "BuildAndSendDigest"
  role             = aws_iam_role.build_and_send.arn
  handler          = "handler.handler"
  runtime          = "python3.12"
  filename         = data.archive_file.build_and_send.output_path
  source_code_hash = data.archive_file.build_and_send.output_base64sha256
  timeout          = 30

  environment {
    variables = {
      DYNAMODB_TABLE = aws_dynamodb_table.executions.name
      S3_BUCKET      = aws_s3_bucket.emails.bucket
      BOT_EMAIL      = var.bot_email
    }
  }
}

resource "aws_lambda_permission" "ses_invoke" {
  statement_id   = "AllowSESInvoke"
  action         = "lambda:InvokeFunction"
  function_name  = aws_lambda_function.email_ingest.function_name
  principal      = "ses.amazonaws.com"
  source_account = local.account_id
}
