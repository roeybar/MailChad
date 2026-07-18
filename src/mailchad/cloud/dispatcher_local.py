"""Local SQS poll wrapper - dev/staging only (spec Phase 3).

Polls SQS queue in a loop and calls dispatcher_lambda.handler() with the
same record shape Lambda uses. Identical code path; no Lambda runtime needed.

Run as: python -m cloud.dispatcher_local
Or via docker-compose ep-v3-dispatcher service.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time

import boto3

log = logging.getLogger("dispatcher.local")

SQS_QUEUE_URL = os.environ["SQS_QUEUE_URL"]
SQS_ENDPOINT  = os.environ.get("SQS_ENDPOINT")
POLL_WAIT_S   = int(os.environ.get("SQS_POLL_WAIT_S", "20"))   # long-poll seconds
MAX_MESSAGES  = int(os.environ.get("SQS_MAX_MESSAGES", "10"))


def _sqs_client():
    kwargs = dict(region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
    if SQS_ENDPOINT:
        kwargs["endpoint_url"] = SQS_ENDPOINT
    return boto3.client("sqs", **kwargs)


def run_forever() -> None:
    from dispatcher_lambda import handler

    sqs = _sqs_client()
    log.info("local dispatcher polling %s (wait=%ss, batch=%s)",
             SQS_QUEUE_URL, POLL_WAIT_S, MAX_MESSAGES)

    running = True

    def _stop(sig, frame):
        nonlocal running
        log.info("signal %s - stopping", sig)
        running = False

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    while running:
        try:
            resp = sqs.receive_message(
                QueueUrl=SQS_QUEUE_URL,
                MaxNumberOfMessages=MAX_MESSAGES,
                WaitTimeSeconds=POLL_WAIT_S,
                AttributeNames=["ApproximateReceiveCount"],
            )
        except Exception as e:
            log.warning("SQS receive error: %s - sleeping 5s", e)
            time.sleep(5)
            continue

        messages = resp.get("Messages", [])
        if not messages:
            continue

        # Reshape to Lambda record format
        records = [
            {
                "body":            m["Body"],
                "receiptHandle":   m["ReceiptHandle"],
                "messageId":       m["MessageId"],
                "attributes":      m.get("Attributes", {}),
            }
            for m in messages
        ]

        to_delete = []
        for record in records:
            try:
                handler({"Records": [record]}, None)
                to_delete.append(record["receiptHandle"])
            except Exception as e:
                # Retriable - leave on queue (visibility timeout will reset)
                log.warning("record %s failed (will retry): %s",
                            record["messageId"], e)

        # Delete successfully processed messages
        for handle in to_delete:
            try:
                sqs.delete_message(QueueUrl=SQS_QUEUE_URL, ReceiptHandle=handle)
            except Exception as e:
                log.warning("delete_message failed: %s", e)

    log.info("dispatcher_local stopped")


if __name__ == "__main__":
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )
    # Ensure cloud/ is on path so `from mailchad.cloud import ...` works
    cloud_dir = os.path.dirname(os.path.abspath(__file__))
    if cloud_dir not in sys.path:
        sys.path.insert(0, cloud_dir)
    run_forever()
