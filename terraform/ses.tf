resource "aws_ses_receipt_rule_set" "main" {
  rule_set_name = var.ses_rule_set_name
}

resource "aws_ses_active_receipt_rule_set" "main" {
  rule_set_name = aws_ses_receipt_rule_set.main.rule_set_name
}

resource "aws_ses_receipt_rule" "ingest" {
  name          = "email-ingest"
  rule_set_name = aws_ses_receipt_rule_set.main.rule_set_name
  enabled       = true
  scan_enabled  = true

  # Action 1: store raw email in S3 so Lambda can read the full body
  s3_action {
    bucket_name       = aws_s3_bucket.emails.bucket
    object_key_prefix = "incoming/"
    position          = 1
  }

  # Action 2: invoke EmailIngest Lambda asynchronously
  lambda_action {
    function_arn    = aws_lambda_function.email_ingest.arn
    invocation_type = "Event"
    position        = 2
  }

  depends_on = [
    aws_s3_bucket_policy.ses_write,
    aws_lambda_permission.ses_invoke,
  ]
}
