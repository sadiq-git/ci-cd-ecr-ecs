# lambda/index.py
import os, json, time, logging, boto3
from botocore.config import Config

log = logging.getLogger()
log.setLevel(logging.INFO)

BEDROCK_ENABLED = os.environ.get("BEDROCK_ENABLED","1") == "1"
BEDROCK_REGION  = os.environ.get("BEDROCK_REGION","us-east-1")
BEDROCK_MODEL   = os.environ.get("BEDROCK_MODEL","amazon.titan-text-lite-v1")
ASSUME_ARN      = os.environ.get("BEDROCK_ASSUME_ROLE_ARN")  # cross-account role in Bedrock acct

cfg = Config(retries={"max_attempts": 3, "mode": "standard"})

def _assume_bedrock():
    if not ASSUME_ARN:
        return boto3.client("bedrock-runtime", region_name=BEDROCK_REGION, config=cfg)
    sts = boto3.client("sts", config=cfg)
    creds = sts.assume_role(RoleArn=ASSUME_ARN, RoleSessionName="poc-self-heal")["Credentials"]
    sess = boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
        region_name=BEDROCK_REGION,
    )
    return sess.client("bedrock-runtime", config=cfg)

def _ecs():
    return boto3.client("ecs", config=cfg)

def _pull_recent_service_events(cluster_arn: str, service_arn_or_name: str, limit=6):
    ecs = _ecs()
    svc = ecs.describe_services(cluster=cluster_arn, services=[service_arn_or_name])["services"][0]
    events = svc.get("events", [])[:limit]
    return [{"message": e["message"], "createdAt": e["createdAt"].isoformat()} for e in events]

PROMPT = """You are a DevOps SRE assistant. Given the following AWS ECS event and recent service events, diagnose the most likely root cause and propose ONE safe action.

Return STRICT JSON with keys:
- diagnosis: short string
- confidence: number between 0 and 1
- safe_action: one of ["none","force_redeploy","scale_up","note"]
- note: short operator note

EVENT:
{event}

RECENT_SERVICE_EVENTS:
{svc_events}
"""

def _ask_bedrock(event, svc_events):
    br = _assume_bedrock()
    body = {"inputText": PROMPT.format(event=json.dumps(event)[:5000],
                                       svc_events=json.dumps(svc_events)[:5000])}
    resp = br.invoke_model(
        modelId=BEDROCK_MODEL,
        accept="application/json",
        contentType="application/json",
        body=json.dumps(body),
    )
    data = json.loads(resp["body"].read())
    text = (data.get("results") or [{}])[0].get("outputText", "{}")
    # try parse JSON from model output (allow leading text/newlines)
    start = text.find("{")
    end = text.rfind("}")
    parsed = json.loads(text[start:end+1]) if start != -1 and end != -1 else {"diagnosis": text, "safe_action":"note", "confidence":0.5, "note":"LLM returned free text"}
    return parsed, data

def _maybe_remediate(event, plan):
    safe_action = (plan.get("safe_action") or "none").lower()
    if safe_action not in {"force_redeploy","scale_up"}:
        return "no-op"

    detail = event.get("detail", {})
    cluster_arn = detail.get("clusterArn") or event.get("resources", [""])[0]
    group = detail.get("group", "")  # e.g., service:agentic-poc-service
    service_name = group.split(":")[1] if ":" in group else None
    if not (cluster_arn and service_name):
        return "missing-cluster-or-service"

    ecs = _ecs()
    if safe_action == "force_redeploy":
        ecs.update_service(cluster=cluster_arn, service=service_name, forceNewDeployment=True)
        return f"forced new deployment for {service_name}"

    if safe_action == "scale_up":
        # very conservative: +1 desired if currently 0
        svc = ecs.describe_services(cluster=cluster_arn, services=[service_name])["services"][0]
        desired = svc.get("desiredCount", 0)
        if desired == 0:
            ecs.update_service(cluster=cluster_arn, service=service_name, desiredCount=1)
            return f"scaled {service_name} from 0 -> 1"
        return "scale_up_skipped_nonzero_desired"

def handler(event, context):
    log.info("Self-heal trigger: %s", json.dumps(event))
    if not BEDROCK_ENABLED:
        return {"statusCode": 200, "body": json.dumps({"ok": True, "note":"bedrock disabled"})}

    try:
        detail = event.get("detail", {})
        cluster_arn = detail.get("clusterArn")
        group = detail.get("group","")
        service = group.split(":")[1] if ":" in group else None
        svc_events = _pull_recent_service_events(cluster_arn, service) if (cluster_arn and service) else []

        t0 = time.time()
        plan, raw = _ask_bedrock(event, svc_events)
        t1 = time.time()

        action_result = _maybe_remediate(event, plan)
        t2 = time.time()

        log.info("Plan: %s", json.dumps(plan))
        log.info("Bedrock latency: %.0fms, Remediation time: %.0fms",
                 (t1-t0)*1000, (t2-t1)*1000)
        return {"statusCode": 200, "body": json.dumps({"ok": True, "plan": plan, "action": action_result})}
    except Exception as e:
        log.exception("self-heal failed")
        return {"statusCode": 500, "body": json.dumps({"ok": False, "error": str(e)})}
