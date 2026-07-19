"""Create DynamoDB table + SQS queue (+ DLQ) for ep-v3.

Idempotent - safe to run against an existing environment.
With DYNAMODB_ENDPOINT / SQS_ENDPOINT set -> local dev (DynamoDB Local + localstack).
Without -> real AWS (needs valid creds + region in environment).

Usage:
  python deploy/setup_tables.py
  # or via bin/v3:
  bin/v3 setup-tables
"""
from __future__ import annotations

import os
import sys

import boto3
from botocore.exceptions import ClientError


def _dynamo_resource():
    kwargs = dict(region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
    ep = os.environ.get("DYNAMODB_ENDPOINT")
    if ep:
        kwargs["endpoint_url"] = ep
    return boto3.resource("dynamodb", **kwargs)


def _sqs_client():
    kwargs = dict(region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
    ep = os.environ.get("SQS_ENDPOINT")
    if ep:
        kwargs["endpoint_url"] = ep
    return boto3.client("sqs", **kwargs)


def setup_dynamo() -> str:
    table_name = os.environ.get("DYNAMODB_TABLE", "ep-v3-dev")
    resource = _dynamo_resource()
    try:
        resource.create_table(
            TableName=table_name,
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[
                {"AttributeName": "pk",       "AttributeType": "S"},
                {"AttributeName": "sk",       "AttributeType": "S"},
                {"AttributeName": "row_key",  "AttributeType": "S"},
                {"AttributeName": "status",   "AttributeType": "S"},
                {"AttributeName": "send_at",  "AttributeType": "S"},
            ],
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "row-key-index",
                    "KeySchema": [
                        {"AttributeName": "row_key", "KeyType": "HASH"},
                        {"AttributeName": "sk",      "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": "status-index",
                    "KeySchema": [
                        {"AttributeName": "status",  "KeyType": "HASH"},
                        {"AttributeName": "send_at", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
        )
        print(f"  created DynamoDB table: {table_name}")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceInUseException":
            print(f"  DynamoDB table already exists: {table_name}")
        else:
            raise
    return table_name


def setup_sqs() -> tuple[str, str]:
    sqs = _sqs_client()
    region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

    # DLQ first (main queue references its ARN)
    dlq_name = "ep-send-dlq"
    try:
        dlq = sqs.create_queue(QueueName=dlq_name, Attributes={"MessageRetentionPeriod": "604800"})
        dlq_url = dlq["QueueUrl"]
        print(f"  created SQS DLQ: {dlq_name}")
    except ClientError as e:
        if e.response["Error"]["Code"] == "QueueAlreadyExists":
            dlq_url = sqs.get_queue_url(QueueName=dlq_name)["QueueUrl"]
            print(f"  SQS DLQ already exists: {dlq_name}")
        else:
            raise

    dlq_attrs = sqs.get_queue_attributes(QueueUrl=dlq_url, AttributeNames=["QueueArn"])
    dlq_arn = dlq_attrs["Attributes"]["QueueArn"]

    # Main send queue
    queue_name = "ep-send-queue"
    redrive = f'{{"deadLetterTargetArn":"{dlq_arn}","maxReceiveCount":"3"}}'
    try:
        q = sqs.create_queue(
            QueueName=queue_name,
            Attributes={
                "VisibilityTimeout":       "60",
                "MessageRetentionPeriod":  "86400",
                "RedrivePolicy":           redrive,
            },
        )
        queue_url = q["QueueUrl"]
        print(f"  created SQS queue: {queue_name}")
    except ClientError as e:
        if e.response["Error"]["Code"] == "QueueAlreadyExists":
            queue_url = sqs.get_queue_url(QueueName=queue_name)["QueueUrl"]
            print(f"  SQS queue already exists: {queue_name}")
        else:
            raise

    return queue_url, dlq_url


if __name__ == "__main__":
    print("Setting up DynamoDB + SQS...")
    table = setup_dynamo()
    queue_url, dlq_url = setup_sqs()
    print(f"\nReady:")
    print(f"  table:  {table}")
    print(f"  queue:  {queue_url}")
    print(f"  dlq:    {dlq_url}")
