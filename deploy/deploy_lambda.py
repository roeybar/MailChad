"""Deploy ep-api or ep-dispatcher Lambda via boto3.

Usage (called from bin/v3 deploy-api / deploy-dispatcher):
  python /deploy/deploy_lambda.py api   <zip_path>
  python /deploy/deploy_lambda.py dispatcher <zip_path>

Env vars consumed (all required for first deploy; update only needs KEY+SECRET):
  AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_DEFAULT_REGION
  DYNAMODB_TABLE, SQS_QUEUE_URL
  LAMBDA_ROLE_ARN (api), LAMBDA_DISPATCHER_ROLE_ARN (dispatcher)
"""
from __future__ import annotations

import json
import os
import sys

import boto3
from botocore.exceptions import ClientError


def _client():
    return boto3.client(
        "lambda",
        region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
    )


def _zip_bytes(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def deploy_api(zip_path: str) -> None:
    lam = _client()
    fn = "ep-api"
    code = {"ZipFile": _zip_bytes(zip_path)}

    try:
        lam.get_function(FunctionName=fn)
        exists = True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceNotFoundException":
            exists = False
        else:
            raise

    if exists:
        r = lam.update_function_code(FunctionName=fn, **code)
        print(f"  updated: {r['FunctionArn']}")
    else:
        role = os.environ["LAMBDA_ROLE_ARN"]
        env_vars = {
            "DYNAMODB_TABLE": os.environ.get("DYNAMODB_TABLE", "ep-v3-prod"),
            "SQS_QUEUE_URL":  os.environ["SQS_QUEUE_URL"],
        }
        r = lam.create_function(
            FunctionName=fn,
            Runtime="python3.12",
            Role=role,
            Handler="mailchad.cloud.lambda_handler.handler",
            Code=code,
            Timeout=30,
            MemorySize=256,
            Environment={"Variables": env_vars},
        )
        print(f"  created: {r['FunctionArn']}")

    print(f"\nNext: configure API Gateway HTTP API -> Lambda proxy -> {fn}")
    print("See deploy/iam/ep-api-policy.json for the execution role policy.")


def _ensure_sweep_schedule(fn_arn: str) -> None:
    """EventBridge rule that fires ep-dispatcher every minute with {"mode":"sweep"}
    so due packs (send_at <= now) get enqueued. Idempotent."""
    region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    events = boto3.client("events", region_name=region)
    lam = _client()
    rule_name = "ep-dispatcher-sweep"
    rate = os.environ.get("SWEEP_RATE", "rate(1 minute)")
    rule = events.put_rule(Name=rule_name, ScheduleExpression=rate, State="ENABLED")
    rule_arn = rule["RuleArn"]
    # Allow EventBridge to invoke the Lambda (idempotent add-permission)
    try:
        lam.add_permission(
            FunctionName="ep-dispatcher", StatementId="ep-dispatcher-sweep-eventbridge",
            Action="lambda:InvokeFunction", Principal="events.amazonaws.com",
            SourceArn=rule_arn,
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceConflictException":
            raise
    events.put_targets(Rule=rule_name, Targets=[{
        "Id": "ep-dispatcher", "Arn": fn_arn,
        "Input": json.dumps({"mode": "sweep"}),
    }])
    print(f"  sweep schedule: {rule_name} ({rate})")


def deploy_dispatcher(zip_path: str) -> None:
    lam = _client()
    fn = "ep-dispatcher"
    code = {"ZipFile": _zip_bytes(zip_path)}
    env_vars = {
        "DYNAMODB_TABLE": os.environ.get("DYNAMODB_TABLE", "ep-v3-prod"),
        "SQS_QUEUE_URL":  os.environ["SQS_QUEUE_URL"],   # sweep enqueues here
    }

    try:
        lam.get_function(FunctionName=fn)
        exists = True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceNotFoundException":
            exists = False
        else:
            raise

    if exists:
        r = lam.update_function_code(FunctionName=fn, **code)
        fn_arn = r["FunctionArn"]
        print(f"  updated: {fn_arn}")
        # Refresh env so SQS_QUEUE_URL is present for the sweep (code update doesn't touch env)
        lam.get_waiter("function_updated").wait(FunctionName=fn)
        lam.update_function_configuration(FunctionName=fn, Environment={"Variables": env_vars})
        lam.get_waiter("function_updated").wait(FunctionName=fn)
        print("  env refreshed (SQS_QUEUE_URL set)")
    else:
        role = os.environ["LAMBDA_DISPATCHER_ROLE_ARN"]
        r = lam.create_function(
            FunctionName=fn,
            Runtime="python3.12",
            Role=role,
            Handler="mailchad.cloud.dispatcher_lambda.handler",
            Code=code,
            Timeout=60,
            MemorySize=256,
            Environment={"Variables": env_vars},
        )
        fn_arn = r["FunctionArn"]
        print(f"  created: {fn_arn}")
        lam.get_waiter("function_active_v2").wait(FunctionName=fn)

        # Wire SQS trigger
        sqs_url = os.environ["SQS_QUEUE_URL"]
        sqs = boto3.client("sqs", region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
        attrs = sqs.get_queue_attributes(QueueUrl=sqs_url, AttributeNames=["QueueArn"])
        queue_arn = attrs["Attributes"]["QueueArn"]
        mapping = lam.create_event_source_mapping(
            FunctionName=fn,
            EventSourceArn=queue_arn,
            BatchSize=10,
        )
        print(f"  SQS trigger: {mapping['UUID']}")

    # Always (re)assert the EventBridge sweep schedule
    _ensure_sweep_schedule(fn_arn)

    print("See deploy/iam/ep-dispatcher-policy.json for the execution role policy.")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: deploy_lambda.py <api|dispatcher> <zip_path>")
        sys.exit(1)
    target, zip_path = sys.argv[1], sys.argv[2]
    if target == "api":
        deploy_api(zip_path)
    elif target == "dispatcher":
        deploy_dispatcher(zip_path)
    else:
        print(f"unknown target: {target}")
        sys.exit(1)
