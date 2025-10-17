import json, os, re, logging, boto3, time
from botocore.config import Config

log = logging.getLogger()
log.setLevel(logging.INFO)

# ---------- env ----------
BEDROCK_ENABLED       = os.getenv("BEDROCK_ENABLED", "1") == "1"
BEDROCK_REGION        = os.getenv("BEDROCK_REGION", "us-east-1")
BEDROCK_MODEL         = os.getenv("BEDROCK_MODEL", "amazon.nova-pro-v1:0")
ASSUME_ROLE_ARN       = os.getenv("BEDROCK_ASSUME_ROLE_ARN")  # cross-account role in 120792917748
SELF_HEAL_MODE        = os.getenv("SELF_HEAL_MODE", "passive")  # passive|active
MAX_ACTIONS_PER_INVOC = int(os.getenv("MAX_ACTIONS_PER_INVOC", "1"))

# Optional helpers (used when events are missing some fields)
AWS_REGION            = os.getenv("AWS_REGION", "ap-south-1")
DEFAULT_CLUSTER       = os.getenv("ECS_CLUSTER_NAME", "agentic-poc-cluster")
DEFAULT_SERVICE       = os.getenv("ECS_SERVICE_NAME", "agentic-poc-service")
ASG_NAME              = os.getenv("ASG_NAME", "agentic-poc-asg")

ecs = boto3.client("ecs", region_name=AWS_REGION)
asg = boto3.client("autoscaling", region_name=AWS_REGION)

# ---------- STS -> Bedrock cross-account ----------
def _assume(role_arn: str, region: str):
    sts = boto3.client("sts")
    creds = sts.assume_role(
        RoleArn=role_arn,
        RoleSessionName="bedrockNovaInvoke"
    )["Credentials"]
    bedrock = boto3.client(
        "bedrock-runtime",
        region_name=region,
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
        config=Config(retries={"max_attempts": 3, "mode": "standard"})
    )
    return bedrock

# ---------- Prompt for Nova ----------
def _to_prompt(event: dict) -> str:
    snippet = json.dumps(event.get("detail", {}))[:2000]
    return f"""
You are an SRE DevOps agent. You receive AWS ECS events and must output a single JSON plan.

Context:
- Event type: {event.get('detail-type')}
- Raw detail (truncated): {snippet}

Goals:
1) Diagnose the situation.
2) Recommend exactly ONE safe action from this list:
   - "note" (no-op)
   - "force_redeploy"  (ecs.update_service forceNewDeployment=true)
   - "scale_service"   (requires "desiredCount": number)
   - "restart_unhealthy_task" (Stop 1 task; ECS will replace it if service desiredCount>0)
   - "scale_capacity"  (ASG Min/Desired to at least 1, then ensure service desiredCount>=1)

Rules:
- If detail.reason includes "RESOURCE:INSTANCE" (capacity shortage), prefer "scale_capacity".
- Always return STRICT JSON with keys: "diagnosis", "confidence" (0..1), "action",
  and optional "desiredCount".
- Keep actions safe. If uncertain, use "note".

Return ONLY JSON. Example:
{{
  "diagnosis": "Deployment stuck; image pull error",
  "confidence": 0.86,
  "action": "force_redeploy"
}}
""".strip()

# ---------- Model invocation ----------
def _invoke_bedrock(bedrock, model_id: str, prompt: str) -> str:
    # Nova Pro expects "messages" format
    if "nova" in model_id:
        body = {
            "messages": [
                {"role": "user", "content": [{"text": prompt}]}
            ],
            "inferenceConfig": {"maxTokens": 512, "temperature": 0.2}
        }
    elif "titan" in model_id:
        body = {"inputText": prompt}
    else:
        body = {
            "messages": [
                {"role": "user", "content": [{"text": prompt}]}
            ],
            "inferenceConfig": {"maxTokens": 512, "temperature": 0.2}
        }

    resp = bedrock.invoke_model(
        modelId=model_id,
        body=json.dumps(body),
        contentType="application/json",
        accept="application/json"
    )
    txt = resp["body"].read().decode("utf-8")

    # Parse typical Bedrock shapes
    try:
        j = json.loads(txt)
        # nova:
        if "output" in j and "message" in j["output"]:
            parts = j["output"]["message"].get("content", [])
            for p in parts:
                if "text" in p:
                    return p["text"]
        # titan:
        if "results" in j and j["results"]:
            return j["results"][0].get("outputText", "")
    except Exception:
        pass

    return txt

# ---------- Parse JSON from model ----------
def _extract_json_blob(text: str) -> dict:
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        raise ValueError("No JSON found in model output")
    plan = json.loads(m.group(0))
    if "action" not in plan:
        plan["action"] = "note"
    if "confidence" not in plan:
        plan["confidence"] = 0.0
    if "diagnosis" not in plan:
        plan["diagnosis"] = "unspecified"
    return plan

# ---------- Discover ECS target from event ----------
def _discover_cluster_service(detail: dict):
    cluster_arn = detail.get("clusterArn") or detail.get("cluster")
    service_name = None

    grp = detail.get("group")
    if grp and grp.startswith("service:"):
        service_name = grp.split(":", 1)[1]
        return cluster_arn or DEFAULT_CLUSTER, service_name

    if "serviceArn" in detail:
        parts = detail["serviceArn"].split("/")
        return cluster_arn or "/".join(parts[:-1]), parts[-1]

    # Fallback to env defaults if not present
    return cluster_arn or DEFAULT_CLUSTER, DEFAULT_SERVICE

# ---------- Actions ----------
def _action_force_redeploy(cluster: str, service: str):
    log.info(f"force_redeploy {cluster} {service}")
    ecs.update_service(cluster=cluster, service=service, forceNewDeployment=True)

def _action_scale(cluster: str, service: str, desired: int):
    log.info(f"scale_service {cluster} {service} -> desiredCount={desired}")
    ecs.update_service(cluster=cluster, service=service, desiredCount=desired)

def _action_restart_one_task(cluster: str, service: str):
    tasks = ecs.list_tasks(cluster=cluster, serviceName=service, desiredStatus="RUNNING")["taskArns"]
    if not tasks:
        log.info("No running tasks to restart.")
        return
    task_arn = tasks[0]
    log.info(f"Stopping task {task_arn}")
    ecs.stop_task(cluster=cluster, task=task_arn, reason="Agentic restart_unhealthy_task")

def _ensure_capacity_then_run(cluster: str, service: str):
    """
    Heals 'RESOURCE:INSTANCE' (capacity shortage):
      - Set ASG Min/Desired >= 1
      - Ensure service desiredCount >= 1
    """
    try:
        g = asg.describe_auto_scaling_groups(
            AutoScalingGroupNames=[ASG_NAME]
        )["AutoScalingGroups"][0]
    except Exception as e:
        log.exception(f"ASG describe failed for {ASG_NAME}")
        raise

    min_sz, des_sz, max_sz = g["MinSize"], g["DesiredCapacity"], g["MaxSize"]
    changed_asg = False

    if des_sz < 1 or min_sz < 1:
        new_max = max(1, max_sz)
        log.info(f"Scaling ASG {ASG_NAME}: Min=1 Desired=1 Max={new_max}")
        asg.update_auto_scaling_group(
            AutoScalingGroupName=ASG_NAME,
            MinSize=1,
            DesiredCapacity=1,
            MaxSize=new_max
        )
        changed_asg = True

    # Ensure service wants at least 1 task
    svc = ecs.describe_services(cluster=cluster, services=[service])["services"][0]
    if svc.get("desiredCount", 0) < 1:
        log.info(f"Setting desiredCount=1 for service {service}")
        ecs.update_service(cluster=cluster, service=service, desiredCount=1)

    # If we just scaled ASG, give the container instance a little time to register
    if changed_asg:
        log.info("Waiting ~10s for container instance to register to ECS...")
        time.sleep(10)

# ---------- Handler ----------
def handler(event, context):
    log.info(f"Agentic trigger: {json.dumps(event)}")

    # Bedrock off?
    if not BEDROCK_ENABLED or not ASSUME_ROLE_ARN:
        return {"statusCode": 200, "body": json.dumps({"ok": True, "text": "Bedrock disabled"})}

    # Model reasoning
    try:
        bedrock = _assume(ASSUME_ROLE_ARN, BEDROCK_REGION)
        prompt  = _to_prompt(event)
        text    = _invoke_bedrock(bedrock, BEDROCK_MODEL, prompt)
        log.info(f"Bedrock text:\n{text}")
    except Exception as e:
        log.exception("Bedrock call failed")
        return {"statusCode": 500, "body": json.dumps({"ok": False, "error": str(e)})}

    # Parse remediation plan (best effort)
    plan = {"action": "note", "confidence": 0.0, "diagnosis": "missing"}
    try:
        plan = _extract_json_blob(text)
    except Exception:
        pass

    # ECS target
    detail = event.get("detail", {})
    cluster, service = _discover_cluster_service(detail)

    acted = False
    try:
        if SELF_HEAL_MODE.lower() == "active" and cluster and service:
            # Heuristic: if placement failure due to capacity, prefer capacity fix,
            # even if the model suggested "restart_unhealthy_task".
            if (event.get("detail-type") == "ECS Service Action"
                and detail.get("eventName") == "SERVICE_TASK_PLACEMENT_FAILURE"
                and "RESOURCE:INSTANCE" in (detail.get("reason") or "")):
                _ensure_capacity_then_run(cluster, service)
                acted = True
            else:
                # Follow model plan
                action = plan.get("action", "note")
                if action == "force_redeploy":
                    _action_force_redeploy(cluster, service); acted = True
                elif action == "scale_service":
                    desired = int(plan.get("desiredCount", 1))
                    _action_scale(cluster, service, desired); acted = True
                elif action == "restart_unhealthy_task":
                    _action_restart_one_task(cluster, service); acted = True
                elif action == "scale_capacity":
                    _ensure_capacity_then_run(cluster, service); acted = True
    except Exception as e:
        log.exception(f"Heal action failed: {plan}")
        return {"statusCode": 500, "body": json.dumps({"ok": False, "plan": plan, "error": str(e)})}

    return {
        "statusCode": 200,
        "body": json.dumps({
            "ok": True,
            "mode": SELF_HEAL_MODE,
            "plan": plan,
            "acted": acted
        })
    }
