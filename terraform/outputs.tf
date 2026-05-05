output "state_machine_arn" {
  description = "ARN of the EmailDigest Step Function"
  value       = aws_sfn_state_machine.digest.arn
}

output "s3_bucket_name" {
  description = "S3 bucket holding raw and processed emails"
  value       = aws_s3_bucket.emails.bucket
}

output "dynamodb_table_name" {
  description = "DynamoDB table tracking executions"
  value       = aws_dynamodb_table.executions.name
}

output "email_ingest_function_name" {
  description = "EmailIngest Lambda function name"
  value       = aws_lambda_function.email_ingest.function_name
}
