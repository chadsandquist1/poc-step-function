resource "aws_dynamodb_table" "executions" {
  name         = "EmailDigestExecutions"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"

  attribute {
    name = "pk"
    type = "S"
  }

  tags = {
    Project = "email-digest-poc"
  }
}
