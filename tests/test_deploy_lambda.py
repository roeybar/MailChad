"""Pin handler configuration when updating existing Lambda functions."""
import importlib.util
from pathlib import Path

import pytest


class FakeClient:
    def __init__(self):
        self.calls = []

    def get_function(self, **kwargs):
        self.calls.append(("get_function", kwargs))
        return {}

    def update_function_code(self, **kwargs):
        self.calls.append(("update_function_code", kwargs))
        return {"FunctionArn": "arn:aws:lambda:us-east-1:0:function:x"}

    def update_function_configuration(self, **kwargs):
        self.calls.append(("update_function_configuration", kwargs))
        return {}

    def get_waiter(self, name):
        self.calls.append(("get_waiter", name))
        return self

    def wait(self, **kwargs):
        pass

    def put_rule(self, **kwargs):
        self.calls.append(("put_rule", kwargs))
        return {"RuleArn": "arn:aws:events:us-east-1:0:rule/ep-dispatcher-sweep"}

    def add_permission(self, **kwargs):
        self.calls.append(("add_permission", kwargs))
        return {}

    def put_targets(self, **kwargs):
        self.calls.append(("put_targets", kwargs))
        return {}


@pytest.fixture
def deploy_rig(monkeypatch):
    path = Path(__file__).resolve().parent.parent / "deploy" / "deploy_lambda.py"
    spec = importlib.util.spec_from_file_location("deploy_lambda", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fake = FakeClient()
    monkeypatch.setattr(module, "_client", lambda: fake)
    monkeypatch.setattr(module.boto3, "client", lambda *args, **kwargs: fake)
    monkeypatch.setattr(module, "_zip_bytes", lambda path: b"zip")
    monkeypatch.setenv("SQS_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/0/test")
    return module, fake


def test_update_sets_api_handler(deploy_rig):
    module, fake = deploy_rig
    module.deploy_api("x.zip")
    configurations = [kwargs for name, kwargs in fake.calls
                      if name == "update_function_configuration"]
    assert len(configurations) == 1
    assert configurations[0]["Handler"] == "mailman.cloud.lambda_handler.handler"


def test_update_sets_dispatcher_handler(deploy_rig):
    module, fake = deploy_rig
    module.deploy_dispatcher("x.zip")
    configurations = [kwargs for name, kwargs in fake.calls
                      if name == "update_function_configuration"]
    assert len(configurations) == 1
    assert configurations[0]["Handler"] == "mailman.cloud.dispatcher_lambda.handler"
    assert configurations[0]["Environment"]["Variables"]["SQS_QUEUE_URL"] == (
        "https://sqs.us-east-1.amazonaws.com/0/test"
    )
