resource "aws_s3_bucket" "emails" {
  bucket = local.bucket_name

  tags = {
    Project = "email-digest-poc"
  }
}

resource "aws_s3_bucket_versioning" "emails" {
  bucket = aws_s3_bucket.emails.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "emails" {
  bucket = aws_s3_bucket.emails.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "emails" {
  bucket = aws_s3_bucket.emails.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "emails" {
  bucket = aws_s3_bucket.emails.id

  rule {
    id     = "expire-30d"
    status = "Enabled"
    filter {}
    expiration {
      days = 30
    }
  }
}

# SES must be permitted to write raw inbound emails into the incoming/ prefix.
resource "aws_s3_bucket_policy" "ses_write" {
  bucket = aws_s3_bucket.emails.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AllowSESPuts"
      Effect    = "Allow"
      Principal = { Service = "ses.amazonaws.com" }
      Action    = "s3:PutObject"
      Resource  = "${aws_s3_bucket.emails.arn}/incoming/*"
      Condition = {
        StringEquals = {
          "aws:Referer" = local.account_id
        }
      }
    }]
  })
}
