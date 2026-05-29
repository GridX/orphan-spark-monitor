# orphan-spark Lambda — Terraform

Daily Lambda that runs the orphan-spark finder and posts the grouped Slack
summary (the same format `generate_outputs.py` produces) to the channel wired
to `slack/webhook/prod`.

## Deploy

```bash
# 1. Build the Lambda package (handler + finder/reporter + pip deps).
../lambda/build.sh

# 2. Init / plan / apply. Pass your VPC subnets and SGs.
terraform init
terraform apply \
  -var='subnet_ids=["subnet-aaa","subnet-bbb"]' \
  -var='security_group_ids=["sg-xxx"]'
```

The subnets must be able to reach `live.internal.gridx.com` and the three
Azkaban hosts (`azkaban.internal.gridx.com`,
`azkaban-stage.internal.gridx.com`, `azkaban.uat.gridx.com`). For the Slack
POST and Secrets Manager calls, the route must also reach the public internet
(NAT gateway) or have a Secrets Manager VPC endpoint.

## Schedule

Default `cron(30 3 ? * MON-FRI *)` = 09:00 IST Mon–Fri. Override with
`-var=schedule_expression='cron(...)'` (UTC).

## Manual invoke (smoke test)

```bash
aws lambda invoke --function-name orphan-spark-scan /tmp/out.json
cat /tmp/out.json
```
