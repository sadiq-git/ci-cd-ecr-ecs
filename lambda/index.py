import os, json, logging, boto3, base64, time

log = logging.getLogger()
log.setLevel(logging.INFO)

BEDROCK_ENABLED = os.getenv("BEDROCK_ENABLED", "1") == "1"
BEDROCK_REGION  = os.getenv("BEDROCK_REGION", "us-east-1")
BEDROCK_MODEL   = os.getenv("BEDROCK_MODEL", "amazon.titan-text-lite-v1")
ASSUME_ROLE_ARN = os.getenv("BEDROCK_ASSUME_ROLE_ARN")

ecs = boto3.client("ecs")
events_map = {
    "SERVICE_TASK_CONFIGURATION_FAILURE": "Task failed to start (bad image tag, ports, memory limits, or env).",
    "SERVICE_STEADY_STATE": "Service reached steady state.",
}

def _bedrock_client():
    if not BEDROCK_ENABLED:
        return None
    sess = boto3.Session()
    if ASSUME_ROLE_ARN:
        sts = sess.client("sts")
        creds = sts.assume_role(RoleArn=ASSUME_ROLE_ARN, RoleSessionName="bedrockCross")[ "Credentials" ]
        return boto3.client(
            "bedrock-runtime",
            region_name=BEDROCK_REGION,
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
        )
    return sess.client("bedrock-runtime", region_name=BEDROCK_REGION)

def plan_with_bedrock(summary, detail):
    if not BEDROCK_ENABLED:
        return {"safe_action":"note","note":"Bedrock disabled; no action"}
    br = _bedrock_client()
    prompt = {
        "role": "system",
        "content": [
            {"text":
             "You are a cautious SRE assistant. Propose ONE safe_action among: "
             "note | force_redeploy | scale_up | rollback.\n"
             "Return strict JSON: {\"diagnosis\":\"...\",\"confidence\":0-1,"
             "\"safe_action\":\"...\",\"note\":\"...\"}.\n"
             "Only act if it’s obviously safe."}
        ]
    }
    user = {
        "role": "user",
        "content": [{"text": f"ECS event summary: {summary}\nDetail: {json.dumps(detail)[:3000]}"}]
    }

    body = {"inputText": json.dumps([prompt, user])}
    try:
        resp = br.invoke_model(modelId=BEDROCK_MODEL, body=json.dumps(body).encode("utf-8"))
        data = json.loads(resp["body"].read())
        text = data["results"][0]["outputText"]
        # Extract JSON if the model wrapped it
        start = text.find("{")
        end   = text.rfind("}")
        plan = json.loads(text[start:end+1]) if start!=-1 and end!=-1 else {"safe_action":"note","note":text}
        return plan
    except Exception as e:
        log.exception("Bedrock plan failed")
        return {"safe_action":"note","note":f"Bedrock error: {e}"}

def maybe_execute(plan, detail):
    action = plan.get("safe_action","note")
    note   = plan.get("note","")
    # Extract cluster/service if present
    cluster = detail.get("clusterArn","").split("/")[-1] if "clusterArn" in detail else None
    group   = detail.get("group","")  # e.g. service:agentic-poc-service
    service = group.split(":")[1] if group.startswith("service:") else None

    if not cluster or not service:
        log.info("No cluster/service in event; action=note")
        return {"executed": False, "reason": "missing cluster/service"}

    if action == "force_redeploy":
        ecs.update_service(cluster=cluster, service=service, forceNewDeployment=True)
        return {"executed": True, "action": action}

    if action == "scale_up":
        ecs.update_service(cluster=cluster, service=service, desiredCount=1)
        return {"executed": True, "action": action}

    if action == "rollback":
        # naive rollback: set lastKnownTaskDefinition if present
        # (For real rollback keep a history in SSM/Dynamo.)
        desc = ecs.describe_services(cluster=cluster, services=[service])["services"][0]
        # nothing to revert to here; keep as note unless you track versions
        return {"executed": False, "reason":"no rollback history; consider SSM param store"}

    # default is note
    return {"executed": False, "action": "note", "note": note}

def handler(event, context):
    log.info(f"Agentic trigger: {json.dumps(event)}")
    # Normalize: ECS events or manual test payloads
    detail_type = event.get("detail-type")
    if not detail_type:
        # manual ping
        summary = f"Manual trigger: {event}"
        detail  = {}
    else:
        summary = events_map.get(event.get("detail",{}).get("eventName",""), detail_type)
        detail  = event.get("detail", {})

    plan  = plan_with_bedrock(summary, detail)
    acted = maybe_execute(plan, detail)
    out = {"ok": True, "summary": summary, "plan": plan, "acted": acted}
    log.info("Plan: " + json.dumps(out))
    return {"statusCode": 200, "body": json.dumps(out)}
