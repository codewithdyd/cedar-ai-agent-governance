import os
import json
import boto3
from urllib.parse import unquote_plus

s3 = boto3.client("s3")
vp = boto3.client("verifiedpermissions")
bedrock = boto3.client("bedrock-runtime")

POLICY_STORE_ID = os.environ["POLICY_STORE_ID"]
MODEL_ID = os.environ["MODEL_ID"]
GUARDRAIL_ID = os.environ["GUARDRAIL_ID"]
GUARDRAIL_VERSION = os.environ["GUARDRAIL_VERSION"]

# The only tool the model can ask for
TOOLS = {"tools": [{
    "toolSpec": {
        "name": "read_file",
        "description": "Read the text of a document in the bucket.",
        "inputSchema": {"json": {
            "type": "object",
            "properties": {"key": {"type": "string", "description": "Full file path in the bucket"}},
            "required": ["key"]}}}}]}


def is_allowed(bucket, key, ip):
    resp = vp.is_authorized(
        policyStoreId=POLICY_STORE_ID,
        principal={"entityType": "AgentApp::Agent", "entityId": "summarizer-agent"},
        action={"actionType": "AgentApp::Action", "actionId": "ReadFile"},
        resource={"entityType": "AgentApp::S3Object", "entityId": key},
        context={"contextMap": {"ip_address": {"string": ip}}},
        entities={"entityList": [{
            "identifier": {"entityType": "AgentApp::S3Object", "entityId": key},
            "attributes": {
                "bucket": {"string": bucket},
                "folder": {"string": key.split("/")[0]},
            },
        }]},
    )
    return resp["decision"] == "ALLOW"


def run_tool(name, tool_input, bucket, ip):
    """Cedar checks the model's request BEFORE the tool runs."""
    if name != "read_file":
        return "Unknown tool.", "error"

    key = tool_input.get("key", "")
    allowed = is_allowed(bucket, key, ip)
    print(json.dumps({
        "agent": "summarizer-agent",
        "action": "ReadFile",
        "bucket": bucket,
        "key": key,
        "ip": ip,
        "decision": "ALLOW" if allowed else "DENY"
    }))

    if not allowed:
        return "Denied by policy. You are not permitted to read this file.", "error"

    body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    return body.decode("utf-8", errors="ignore")[:20000], "success"


def run_agent(bucket, key, ip):
    messages = [{"role": "user", "content": [{"text":
        f"A new document was uploaded: {key}. Read it using your tool and write a short summary."}]}]

    for _ in range(5):  # safety limit on steps
        resp = bedrock.converse(modelId=MODEL_ID, messages=messages,
                        toolConfig=TOOLS,
                        guardrailConfig={"guardrailIdentifier": GUARDRAIL_ID, "guardrailVersion": GUARDRAIL_VERSION},
                        inferenceConfig={"maxTokens": 800})
        message = resp["output"]["message"]
        messages.append(message)

        if resp["stopReason"] != "tool_use":  # model is done
            return "".join(b.get("text", "") for b in message["content"])

        results = []
        for block in message["content"]:
            if "toolUse" in block:
                t = block["toolUse"]
                text, status = run_tool(t["name"], t["input"], bucket, ip)

             
                if status == "error":
                    return None

                results.append({"toolResult": {
                    "toolUseId": t["toolUseId"],
                    "content": [{"text": text}],
                    "status": status}})
        messages.append({"role": "user", "content": results})

    return "Stopped: too many steps."


def lambda_handler(event, context):
    for record in event["Records"]:
        bucket = record["s3"]["bucket"]["name"]
        key = unquote_plus(record["s3"]["object"]["key"])
        ip = record["requestParameters"]["sourceIPAddress"]

        summary = run_agent(bucket, key, ip)

        # CORRECTION 2: don't save anything if there is no summary
        if summary is None:
            print(f"No summary saved for {key}")
            continue

        name = os.path.splitext(key.split("/")[-1])[0]
        out_key = f"summaries/{name}.summary.txt"
        s3.put_object(Bucket=bucket, Key=out_key, Body=summary.encode("utf-8"))
        print(f"Summary saved to {out_key}")