import json
import logging as _logging
import os

import boto3

ddb = boto3.resource("dynamodb")
TABLE = ddb.Table(os.environ["DYNAMODB_TABLE"])

_logger = _logging.getLogger()
_logger.setLevel(_logging.INFO)


def _log(level: str, message: str, **fields):
    _logger.log(
        getattr(_logging, level.upper()),
        json.dumps({"level": level, "message": message, **fields}),
    )


def handler(event, context):
    # Parameters sends: metadata, executionId (from $.context.executionId), taskToken
    exec_id = event["executionId"]
    task_token = event["taskToken"]

    TABLE.update_item(
        Key={"pk": f"EXEC#{exec_id}"},
        UpdateExpression="SET taskToken = :t",
        ExpressionAttributeValues={":t": task_token},
    )
    _log("info", "token_registered", exec_id=exec_id)
