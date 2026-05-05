import os

import boto3

ddb = boto3.resource("dynamodb")
TABLE = ddb.Table(os.environ["DYNAMODB_TABLE"])


def handler(event, context):
    exec_id = event["executionId"]
    task_token = event["taskToken"]

    TABLE.update_item(
        Key={"pk": f"EXEC#{exec_id}"},
        UpdateExpression="SET taskToken = :t",
        ExpressionAttributeValues={":t": task_token},
    )
