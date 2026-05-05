variable "aws_region" {
  description = "AWS region. SES inbound is only available in us-east-1, us-west-2, and eu-west-1."
  type        = string
  default     = "us-east-1"
}

variable "bot_email" {
  description = "SES-verified email address used to send and receive digest emails (e.g. digest-bot@yourdomain.com)."
  type        = string
}

variable "ses_rule_set_name" {
  description = "Name of the SES active receipt rule set."
  type        = string
  default     = "email-digest-poc"
}
