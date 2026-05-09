"""
Snowflake Semantic View -> Tableau Semantics (Data Cloud) Sync

Reads Snowflake Semantic View definitions (DIMENSION / FACT / METRIC) and creates
corresponding Semantic Models in Tableau Semantics via the Authoring API.

Environment Variables:
  Snowflake:
    SNOWFLAKE_ACCOUNT       : e.g. "myorg.us-east-1.aws"
    SNOWFLAKE_USER          : Service account username
    SNOWFLAKE_DATABASE      : Database name
    SNOWFLAKE_SCHEMA        : Schema name
    SNOWFLAKE_ROLE          : Role with access to semantic views
    SNOWFLAKE_WAREHOUSE     : Warehouse name
    SNOWFLAKE_SECRET_ID     : AWS Secrets Manager secret name
    SNOWFLAKE_SECRET_KEY    : Key inside the secret JSON (default: "snowflake_password")
    SNOWFLAKE_REGION        : AWS region for Secrets Manager (default: "us-east-1")

  Salesforce:
    SF_CLIENT_ID            : Connected App Consumer Key
    SF_USERNAME             : Salesforce username for JWT auth
    SF_PRIVATE_KEY          : PKCS#8 private key (newlines escaped as \\n)
    SF_DOMAIN               : login.salesforce.com (default) or test.salesforce.com
"""

import json
import os
import time
import urllib.request
import urllib.parse
import urllib.error
import base64

import boto3
import snowflake.connector


# ==========================================================================
# Type Mapping: Snowflake -> Tableau Semantics
# ==========================================================================

SF_TYPE_MAP = {
    "VARCHAR":       ("Text",     "Discrete"),
    "STRING":        ("Text",     "Discrete"),
    "TEXT":          ("Text",     "Discrete"),
    "CHAR":          ("Text",     "Discrete"),
    "BOOLEAN":       ("Text",     "Discrete"),
    "DATE":          ("Date",     "Continuous"),
    "TIMESTAMP":     ("DateTime", "Discrete"),
    "TIMESTAMP_NTZ": ("DateTime", "Discrete"),
    "TIMESTAMP_LTZ": ("DateTime", "Discrete"),
    "TIMESTAMP_TZ":  ("DateTime", "Discrete"),
    "NUMBER":        ("Number",   "Continuous"),
    "FLOAT":         ("Number",   "Continuous"),
    "DOUBLE":        ("Number",   "Continuous"),
    "DECIMAL":       ("Number",   "Continuous"),
    "INTEGER":       ("Number",   "Continuous"),
    "BIGINT":        ("Number",   "Continuous"),
    "REAL":          ("Number",   "Continuous"),
}


def map_sf_type(snowflake_type: str) -> tuple[str, str]:
    """Map a Snowflake data type to (Tableau Semantics dataType, displayCategory)."""
    base = snowflake_type.split("(")[0].upper().strip()
    return SF_TYPE_MAP.get(base, ("Text", "Discrete"))


# ==========================================================================
# Snowflake Connection & Semantic View Parsing
# ==========================================================================

def get_snowflake_connection():
    """Connect to Snowflake using credentials from AWS Secrets Manager."""
    region = os.environ.get("SNOWFLAKE_REGION", "us-east-1")
    secret_id = os.environ["SNOWFLAKE_SECRET_ID"]
    secret_key = os.environ.get("SNOWFLAKE_SECRET_KEY", "snowflake_password")

    secret = boto3.client("secretsmanager", region_name=region) \
        .get_secret_value(SecretId=secret_id)
    password = json.loads(secret["SecretString"])[secret_key]

    return snowflake.connector.connect(
        user=os.environ["SNOWFLAKE_USER"],
        password=password,
        account=os.environ["SNOWFLAKE_ACCOUNT"],
        database=os.environ["SNOWFLAKE_DATABASE"],
        schema=os.environ["SNOWFLAKE_SCHEMA"],
        role=os.environ["SNOWFLAKE_ROLE"],
        warehouse=os.environ["SNOWFLAKE_WAREHOUSE"],
    )


def describe_semantic_view(conn, view_name: str) -> dict:
    """Parse DESCRIBE SEMANTIC VIEW output into a structured dict."""
    cur = conn.cursor(snowflake.connector.DictCursor)
    try:
        cur.execute(f"DESCRIBE SEMANTIC VIEW {view_name}")
        rows = cur.fetchall()
    finally:
        cur.close()

    result = {"comment": None, "tables": {}, "dimensions": [], "facts": [], "metrics": []}

    for row in rows:
        kind   = row.get("object_kind")
        name   = row.get("object_name")
        parent = row.get("parent_entity")
        prop   = row.get("property")
        value  = row.get("property_value")

        if kind is None and prop == "COMMENT":
            result["comment"] = value
            continue
        if kind == "TABLE":
            result["tables"].setdefault(name, {})[prop] = value
            continue
        if kind in ("DIMENSION", "FACT", "METRIC"):
            lst = {"DIMENSION": result["dimensions"],
                   "FACT":      result["facts"],
                   "METRIC":    result["metrics"]}[kind]
            fld = next((f for f in lst if f["name"] == name), None)
            if not fld:
                fld = {"name": name, "parent": parent, "kind": kind}
                lst.append(fld)
            fld[prop] = value

    return result


# ==========================================================================
# Salesforce JWT Bearer Flow
# ==========================================================================

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _make_jwt(client_id: str, username: str, private_key_pem: str, audience: str) -> str:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    header  = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    now     = int(time.time())
    payload = _b64url(json.dumps({
        "iss": client_id, "sub": username, "aud": audience, "exp": now + 300
    }).encode())
    signing_input = f"{header}.{payload}".encode()

    private_key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
    signature   = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{payload}.{_b64url(signature)}"


def get_access_token() -> dict:
    """Authenticate to Salesforce via JWT Bearer Flow."""
    client_id       = os.environ["SF_CLIENT_ID"]
    username        = os.environ["SF_USERNAME"]
    private_key_pem = os.environ["SF_PRIVATE_KEY"].replace("\\n", "\n")
    domain          = os.environ.get("SF_DOMAIN", "login.salesforce.com")

    jwt  = _make_jwt(client_id, username, private_key_pem, f"https://{domain}")
    data = urllib.parse.urlencode({
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": jwt,
    }).encode()

    req = urllib.request.Request(
        f"https://{domain}/services/oauth2/token", data=data, method="POST"
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Salesforce OAuth error {e.code}: {e.read().decode()}")


# ==========================================================================
# Tableau Semantics Authoring API
# ==========================================================================

API_BASE = "/services/data/v66.0/ssot/semantic/models"


def sf_request(method: str, path: str, token_info: dict, body: dict = None) -> dict:
    """Call the Salesforce REST API."""
    url = f"{token_info['instance_url']}{path}"
    req = urllib.request.Request(
        url, data=json.dumps(body).encode() if body else None, method=method
    )
    req.add_header("Authorization", f"Bearer {token_info['access_token']}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            content = resp.read()
            return json.loads(content) if content else {}
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Salesforce API error {e.code}: {e.read().decode()}")


def get_existing_model(model_name: str, token_info: dict) -> dict | None:
    """Fetch an existing Tableau Semantics model, or None if not found."""
    try:
        return sf_request("GET", f"{API_BASE}/{model_name}", token_info)
    except RuntimeError as e:
        if "404" in str(e) or "NOT_FOUND" in str(e):
            return None
        raise


def get_existing_fields(model_name: str, token_info: dict) -> dict:
    """Get sets of existing dimension/measure apiNames to avoid duplicates."""
    existing = {"dimensions": set(), "measures": set()}
    try:
        model = sf_request(
            "GET", f"{API_BASE}/{model_name}?includeModelContent=true", token_info
        )
        for lv in model.get("semanticLogicalViews", []):
            for do in lv.get("semanticDataObjects", []):
                for dim in do.get("semanticDimensions", []):
                    existing["dimensions"].add(dim["apiName"])
                for meas in do.get("semanticMeasurements", []):
                    existing["measures"].add(meas["apiName"])
    except Exception:
        pass
    return existing


# ==========================================================================
# Sync: Snowflake Semantic View -> Tableau Semantics
# ==========================================================================

def build_field_api_name(base_table: str, column_name: str) -> str:
    """Generate Tableau Semantics field name: {table}1_{column}."""
    return f"{base_table}1_{column_name}"


def sync_to_tableau_semantics(sv_parsed, model_name, workspace, token_info):
    """Sync a parsed Snowflake Semantic View to a Tableau Semantics model."""
    stats = {
        "model": None, "logical_view": None,
        "dims_synced": 0, "measures_synced": 0, "metrics_synced": 0,
        "skipped": 0, "errors": [],
    }

    # Derive naming from Snowflake table metadata
    table_aliases = list(sv_parsed["tables"].keys())
    if not table_aliases:
        raise RuntimeError("No tables defined in the Semantic View")

    base_table       = sv_parsed["tables"][table_aliases[0]].get("BASE_TABLE_NAME", table_aliases[0])
    data_object_api  = f"{base_table}1"
    logical_view_api = f"{base_table}_USER_ID_lv"
    dmo_name         = f"{base_table}__dlm"

    # --- Step 1: Create or verify model ---
    existing = get_existing_model(model_name, token_info)
    if existing:
        print(f"Model '{model_name}' exists (id={existing.get('id')})")
        stats["model"] = "exists"
        if existing.get("semanticLogicalViews"):
            fields = get_existing_fields(model_name, token_info)
            stats["logical_view"] = "exists"
            stats["skipped"] = len(fields["dimensions"]) + len(fields["measures"])
            return stats
    else:
        sf_request("POST", API_BASE, token_info, {
            "apiName": model_name, "label": model_name,
            "dataspace": "default", "queryUnrelatedDataObjects": "Union",
        })
        print(f"Created model: {model_name}")
        stats["model"] = "created"

    # --- Step 2: Build dimensions ---
    sf_dimensions = []
    for dim in sv_parsed["dimensions"]:
        dt, dc = map_sf_type(dim.get("DATA_TYPE", "VARCHAR"))
        api_name = build_field_api_name(base_table, dim["name"])
        sf_dimensions.append({
            "apiName": api_name,
            "label": dim.get("COMMENT", dim["name"]),
            "dataType": dt, "displayCategory": dc,
            "dataObjectFieldName": f"{dim['name'].lower()}__c",
            "isVisible": True, "sortOrder": "None",
        })
        stats["dims_synced"] += 1
        print(f"  DIM: {dim['name']} -> {api_name}")

    # --- Step 3: Build measures ---
    sf_measures = []
    for fact in sv_parsed["facts"]:
        dt, _ = map_sf_type(fact.get("DATA_TYPE", "NUMBER"))
        api_name = build_field_api_name(base_table, fact["name"])
        sf_measures.append({
            "apiName": api_name,
            "label": fact.get("COMMENT", fact["name"]),
            "dataType": dt, "displayCategory": "Continuous",
            "dataObjectFieldName": f"{fact['name'].lower()}__c",
            "aggregationType": "Sum", "isVisible": True, "isAggregatable": True,
            "sortOrder": "None", "decimalPlace": 2, "directionality": "Up",
        })
        stats["measures_synced"] += 1
        print(f"  MEASURE: {fact['name']} -> {api_name}")

    # --- Step 4: POST logical view + data object + fields ---
    lv_payload = {
        "apiName": logical_view_api,
        "label": base_table.replace("_", " "),
        "semanticDataObjects": [{
            "apiName": data_object_api, "label": base_table,
            "dataObjectName": dmo_name, "dataObjectType": "Dmo",
            "semanticDimensions": sf_dimensions,
            "semanticMeasurements": sf_measures,
        }],
    }

    try:
        sf_request("POST", f"{API_BASE}/{model_name}/logical-views", token_info, lv_payload)
        print(f"Created logical view: {logical_view_api}")
        stats["logical_view"] = "created"
    except RuntimeError as e:
        stats["errors"].append(str(e))
        stats["logical_view"] = "error"
        return stats

    # --- Step 5: Create metrics for each measure ---
    time_dim_api = None
    for dim in sv_parsed["dimensions"]:
        if dim.get("DATA_TYPE", "").upper().startswith("DATE"):
            time_dim_api = build_field_api_name(base_table, dim["name"])
            break

    text_dim_api = None
    for dim in sv_parsed["dimensions"]:
        if dim.get("DATA_TYPE", "").split("(")[0].upper() in ("VARCHAR", "STRING", "TEXT"):
            text_dim_api = build_field_api_name(base_table, dim["name"])
            break

    metrics_url = f"{API_BASE}/{model_name}/metrics"
    stats["metrics_synced"] = 0

    for fact in sv_parsed["facts"]:
        measure_api = build_field_api_name(base_table, fact["name"])
        metric_api  = f"{fact['name']}_mtc"

        metric_body = {
            "apiName": metric_api,
            "label": fact.get("COMMENT", fact["name"]),
            "description": fact.get("COMMENT", fact["name"]),
            "aggregationType": "Sum",
            "isCumulative": True, "isGoalEditingBlocked": False,
            "timeGrains": ["Day", "Week", "Month", "Quarter", "Year"],
            "measurementReference": {
                "tableFieldReference": {
                    "fieldApiName": measure_api, "tableApiName": logical_view_api,
                }
            },
            "insightsSettings": {
                "sentiment": "SentimentTypeUpIsGood",
                "singularNoun": "", "pluralNoun": "",
                "insightTypes": [
                    {"enabled": True, "type": "TopContributors"},
                    {"enabled": True, "type": "ComparisonToExpectedRangeAlert"},
                    {"enabled": True, "type": "TrendChangeAlert"},
                    {"enabled": True, "type": "BottomContributors"},
                    {"enabled": True, "type": "CurrentTrend"},
                ],
            },
        }

        if time_dim_api:
            metric_body["timeDimensionReference"] = {
                "tableFieldReference": {
                    "fieldApiName": time_dim_api, "tableApiName": logical_view_api,
                }
            }

        if text_dim_api:
            metric_body["additionalDimensions"] = [{
                "tableFieldReference": {
                    "fieldApiName": text_dim_api, "tableApiName": logical_view_api,
                }
            }]
            metric_body["insightsSettings"]["insightsDimensionsReferences"] = [{
                "tableFieldReference": {
                    "fieldApiName": text_dim_api, "tableApiName": logical_view_api,
                }
            }]

        try:
            sf_request("POST", metrics_url, token_info, metric_body)
            print(f"  METRIC: {metric_api}")
            stats["metrics_synced"] += 1
        except RuntimeError as e:
            print(f"  METRIC error ({metric_api}): {e}")
            stats["errors"].append(f"metric {metric_api}: {e}")

    return stats


# ==========================================================================
# Lambda Handler
# ==========================================================================

def lambda_handler(event, context):
    """
    Actions:
      {"action": "sync", "semantic_view": "DB.SCHEMA.SV_NAME",
       "sf_model_name": "MODEL_NAME", "sf_workspace": "WORKSPACE"}
      {"action": "describe", "semantic_view": "DB.SCHEMA.SV_NAME"}
      {"action": "list_sf_models"}
    """
    print(f"Event: {json.dumps(event)}")
    action = event.get("action", "sync")

    if action == "describe":
        conn = get_snowflake_connection()
        try:
            parsed = describe_semantic_view(conn, event["semantic_view"])
        finally:
            conn.close()
        return {
            "statusCode": 200,
            "comment": parsed["comment"],
            "tables": parsed["tables"],
            "dimensions": [{"name": d["name"], "type": d.get("DATA_TYPE"), "comment": d.get("COMMENT")} for d in parsed["dimensions"]],
            "facts":      [{"name": f["name"], "type": f.get("DATA_TYPE"), "comment": f.get("COMMENT")} for f in parsed["facts"]],
            "metrics":    [{"name": m["name"], "expression": m.get("EXPRESSION"), "comment": m.get("COMMENT")} for m in parsed["metrics"]],
        }

    elif action == "list_sf_models":
        token_info = get_access_token()
        result = sf_request("GET", API_BASE, token_info)
        return {
            "statusCode": 200,
            "models": [{"apiName": m["apiName"], "label": m["label"], "id": m["id"]} for m in result.get("items", [])],
        }

    elif action == "sync":
        conn = get_snowflake_connection()
        try:
            parsed = describe_semantic_view(conn, event["semantic_view"])
        finally:
            conn.close()
        print(f"Parsed: {len(parsed['dimensions'])} dims, {len(parsed['facts'])} facts, {len(parsed['metrics'])} metrics")

        token_info = get_access_token()
        stats = sync_to_tableau_semantics(parsed, event["sf_model_name"], event.get("sf_workspace", "default"), token_info)
        print(f"=== Sync complete ===\n{json.dumps(stats, indent=2)}")
        return {"statusCode": 200, "stats": stats}

    else:
        return {"statusCode": 400, "error": f"Unknown action: {action}"}
