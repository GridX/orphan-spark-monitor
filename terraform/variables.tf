variable "name" {
  description = "Lambda function name (also used as prefix for IAM role and event rule)."
  type        = string
  default     = "orphan-spark-scan"
}

variable "scan_region" {
  description = "AWS region the function should scan EC2 / Azkaban in."
  type        = string
  default     = "us-west-2"
}

variable "hours" {
  description = "Stopped-longer-than threshold passed to the finder (hours)."
  type        = number
  default     = 24
}

variable "verify_tls" {
  description = "Verify Azkaban TLS certs (prod/stage use self-signed certs)."
  type        = bool
  default     = false
}

# 03:30 UTC Mon-Fri = 09:00 IST Mon-Fri.
variable "schedule_expression" {
  description = "EventBridge schedule expression (UTC)."
  type        = string
  default     = "cron(30 3 ? * MON-FRI *)"
}

variable "subnet_ids" {
  description = "Private subnets that can reach live.internal.gridx.com and the Azkaban hosts."
  type        = list(string)
}

variable "security_group_ids" {
  description = "Security groups to attach to the Lambda ENI."
  type        = list(string)
}

variable "slack_webhook_secret_id" {
  description = "Secrets Manager id holding the Slack incoming webhook URL."
  type        = string
  default     = "slack/webhook/prod"
}

variable "azkaban_secret_ids" {
  description = "Secrets Manager ids for the three Azkaban environments (prod/stage/uat)."
  type        = list(string)
  default = [
    "prod/utility-operation/uo_service_db",
    "utility-operation/uo_service_db_stage",
    "uat/utility-operation/uo_service_db",
  ]
}

variable "timeout_seconds" {
  description = "Lambda timeout. Generous because Azkaban lookups are serial per execid."
  type        = number
  default     = 300
}

variable "memory_mb" {
  description = "Lambda memory."
  type        = number
  default     = 512
}
