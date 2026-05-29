name        = "orphan-spark-scan"
scan_region = "us-west-2"
hours       = 24
verify_tls  = false

# 03:30 UTC Mon-Fri = 09:00 IST Mon-Fri.
schedule_expression = "cron(30 3 ? * MON-FRI *)"

# VPC: gridx-oregon (vpc-925c96f6).
# Three primary private subnets (one per AZ), mirroring prod-pricing-engine Lambdas.
subnet_ids = [
  "subnet-0bb89742", # gridx-private-2a
  "subnet-15503172", # gridx-private-2b
  "subnet-973e3dce", # gridx-private-2c
]

# azkaban SG — egress all, lets the Lambda reach Azkaban + live.internal.
security_group_ids = ["sg-062b337a"]

slack_webhook_secret_id = "slack/webhook/prod"
