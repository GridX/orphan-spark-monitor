name        = "orphan-spark-scan"
scan_region = "us-west-2"
hours       = 24
verify_tls  = false

# 03:30 UTC Mon-Fri = 09:00 IST Mon-Fri.
schedule_expression = "cron(30 3 ? * MON-FRI *)"

# TODO: fill in the private subnets and security group(s) that can reach
# live.internal.gridx.com and the three Azkaban hosts.
subnet_ids         = ["subnet-REPLACE_ME_A", "subnet-REPLACE_ME_B"]
security_group_ids = ["sg-REPLACE_ME"]

slack_webhook_secret_id = "slack/webhook/prod"
