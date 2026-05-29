"""Lambda entrypoint: scan for orphaned Spark EC2s and post a Slack summary.

Reuses find_orphans() and build_slack() from the CLI tools so the message
formatting matches what engineers already see when generate_outputs.py is
run by hand. Designed to be triggered on a daily EventBridge schedule.

Env vars (all read at invocation time):
    SLACK_WEBHOOK_SECRET_ID  Secrets Manager id holding the incoming webhook
                             URL. Value may be the raw URL or a JSON object
                             with a "webhook_url" key.
    REGION                   AWS region for EC2/Azkaban lookups (default:
                             us-west-2). Distinct from AWS_REGION because the
                             function may run in a different region than the
                             one it scans.
    HOURS                    Stopped-longer-than threshold (default: 24).
    VERIFY_TLS               "true" to verify Azkaban TLS (default: false —
                             prod/stage use self-signed certs).
"""

import json
import logging
import os

import boto3
import requests
import urllib3

from generate_outputs import build_slack
from orphaned_spark_instances import AzkabanError, MissingTagError, find_orphans

LOG = logging.getLogger()
LOG.setLevel(logging.INFO)


def get_slack_webhook(session, region, secret_id):
    """Return the Slack incoming-webhook URL from Secrets Manager.

    The secret value may be either the raw URL or a JSON object. Across
    GridX, different Slack-webhook secrets use different JSON keys, so we
    accept any of `webhook`, `url`, or `webhook_url`:
      - slack/long_running_job  -> {"webhook": "..."}
      - slack/webhook/prod      -> {"url": "..."}
    """
    sm = session.client("secretsmanager", region_name=region)
    raw = sm.get_secret_value(SecretId=secret_id)["SecretString"]
    if raw.lstrip().startswith("{"):
        d = json.loads(raw)
        url = d.get("webhook") or d.get("url") or d.get("webhook_url")
        if not url:
            raise KeyError(
                f"secret {secret_id!r}: expected key 'webhook', 'url', or "
                f"'webhook_url', got {sorted(d)}")
        return url
    return raw.strip()


def post_to_slack(webhook_url, text):
    resp = requests.post(webhook_url, json={"text": text}, timeout=15)
    resp.raise_for_status()


def lambda_handler(event, context):
    region = os.environ.get("REGION", "us-west-2")
    hours = float(os.environ.get("HOURS", "24"))
    verify_tls = os.environ.get("VERIFY_TLS", "").lower() == "true"
    secret_id = os.environ["SLACK_WEBHOOK_SECRET_ID"]

    if not verify_tls:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    session = boto3.Session()
    webhook_url = get_slack_webhook(session, region, secret_id)

    try:
        orphans = find_orphans(
            session, region, hours, verify_tls, log=LOG.info,
        )
    except (MissingTagError, AzkabanError) as e:
        # Audit could not complete — tell Slack so the scan isn't silently
        # broken, then re-raise so the Lambda invocation is marked failed and
        # CloudWatch alarms / DLQs fire.
        post_to_slack(
            webhook_url,
            f":x: orphan-spark scan failed: `{e}`",
        )
        raise

    LOG.info("found %d orphans", len(orphans))

    if not orphans:
        post_to_slack(
            webhook_url,
            f":white_check_mark: orphan-spark scan: no orphaned spark "
            f"instances older than {hours:g}h.",
        )
        return {"orphans": 0}

    post_to_slack(webhook_url, build_slack(orphans, hours))
    return {"orphans": len(orphans)}
