# Backend configuration for Production environment
# AWS Account: 743512984079 (shared with UAT)

bucket         = "gx-tg-terraform-state"
key            = "orphan-spark/prod/terraform.tfstate"
region         = "us-west-2"
encrypt        = true
dynamodb_table = "terraform-locks-orphan-spark-prod"
