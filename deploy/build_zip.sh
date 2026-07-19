#!/bin/bash
set -e
rm -rf /tmp/lambda-pkg && mkdir /tmp/lambda-pkg
pip install -q --root-user-action=ignore --target /tmp/lambda-pkg -r /cloud/requirements-lambda.txt
cp -r /cloud/app /tmp/lambda-pkg/app
cp /cloud/lambda_handler.py /tmp/lambda-pkg/lambda_handler.py
cp /cloud/dispatcher_lambda.py /tmp/lambda-pkg/dispatcher_lambda.py
python3 /deploy/mkzip.py
