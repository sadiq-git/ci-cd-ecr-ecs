import os, json, logging, boto3
log = logging.getLogger(); log.setLevel(logging.INFO)

BEDROCK_ENABLED = os.environ.get("BEDROCK_ENABLED","1") == "1"
BEDROCK_REGION  = os.environ.get("BEDROCK_REGION","us-east-1")
BEDROCK_MODEL   = os.environ.get("BEDROCK_MODEL","amazon.titan-text-lite-v1")
ASSUME_ARN      = os.environ.get("BEDROCK_ASSUME_ROLE_ARN")  # arn:aws:iam::120792917748:role/BedrockInvokeFromPoc

def _bedrock_client():
    if not ASSUME_ARN:
        return boto3.client("bedrock-runtime", region_name=BEDROCK_REGION)
    sts = boto3.client("sts")
    creds = sts.assume_role(RoleArn=ASSUME_ARN, RoleSessionName="poc-bedrock")["Credentials"]
    sess = boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
        region_name=BEDROCK_REGION
    )
    return sess.client("bedrock-runtime")

def handler(event, context):
    log.info("Agentic trigger: %s", json.dumps(event))
    if not BEDROCK_ENABLED:
        return {"statusCode": 200, "body": json.dumps({"ok": True, "note": "Bedrock disabled"})}
    try:
        client = _bedrock_client()
        body = {"inputText": "Say hi in one short line."}
        resp = client.invoke_model(
            modelId=BEDROCK_MODEL,
            body=json.dumps(body),
            accept="application/json",
            contentType="application/json",
        )
        data = json.loads(resp["body"].read())
        text = (data.get("results") or [{}])[0].get("outputText")
        log.info("Bedrock text: %s", text)
        return {"statusCode": 200, "body": json.dumps({"ok": True, "text": text, "raw": data})}
    except Exception as e:
        log.exception("bedrock failed")
        return {"statusCode": 500, "body": json.dumps({"ok": False, "error": str(e)})}
