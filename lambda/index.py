import json, logging
logger = logging.getLogger(); logger.setLevel(logging.INFO)

def handler(event, context):
    logger.info("deploy_event=%s", json.dumps(event))
    # TODO: later call Bedrock Agent / Flows here
    return {"statusCode": 200, "body": json.dumps({"ok": True, "received": event})}
