import json, logging, os, boto3
logger = logging.getLogger(); logger.setLevel(logging.INFO)

bedrock = boto3.client('bedrock-runtime', region_name=os.environ.get('AWS_REGION','ap-south-1'))

def handler(event, context):
    logger.info("Agentic trigger: %s", json.dumps(event))
    prompt = f"Explain this ECS event and suggest an operator action: {json.dumps(event)}"
    try:
        resp = bedrock.invoke_model(
            modelId="anthropic.claude-3-sonnet-20240229-v1:0",
            body=json.dumps({"inputText": prompt})
        )
        body = json.loads(resp["body"].read())
        logger.info("Bedrock analysis: %s", json.dumps(body))
        return {"statusCode": 200, "body": json.dumps({"ok": True, "analysis": body})}
    except Exception as e:
        logger.exception("Bedrock call failed")
        return {"statusCode": 500, "body": json.dumps({"ok": False, "error": str(e)})}
