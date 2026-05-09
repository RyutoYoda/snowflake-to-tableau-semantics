# snowflake-to-tableau-semantics

Sync **Snowflake Semantic View** definitions to **Tableau Semantics** (Salesforce Data Cloud) — dimensions, measures, and metrics — with a single Lambda invocation.

Define your semantic layer once in Snowflake. This tool creates the corresponding Tableau Semantics model automatically.

## Architecture

```mermaid
flowchart LR
    SF_DIM(("Dimensions")) --> SV(("❄️ Semantic\nView"))
    SF_FACT(("Facts")) --> SV
    SF_MET(("Metrics")) --> SV

    SV -- "DESCRIBE\nSEMANTIC VIEW" --> Lambda(("Lambda"))
    Lambda -- "Authoring API\n(JWT)" --> TS(("☁️ Tableau\nSemantics"))

    TS --> DIM(("Dimensions"))
    TS --> MEA(("Measures"))
    TS --> MET(("Metrics"))

    Secrets(("Secrets\nManager")) -.-> Lambda

    style SF_DIM fill:#29B5E8,color:#fff,stroke:none
    style SF_FACT fill:#29B5E8,color:#fff,stroke:none
    style SF_MET fill:#29B5E8,color:#fff,stroke:none
    style SV fill:#1a8cdb,color:#fff,stroke:none
    style Lambda fill:#FF9900,color:#fff,stroke:none
    style Secrets fill:#dd6b20,color:#fff,stroke:none
    style TS fill:#032D60,color:#fff,stroke:none
    style DIM fill:#1a73e8,color:#fff,stroke:none
    style MEA fill:#1a73e8,color:#fff,stroke:none
    style MET fill:#1a73e8,color:#fff,stroke:none
```

## Prerequisites

Your data must already be flowing into Salesforce Data Cloud before this tool can create a semantic model on top of it.

```mermaid
flowchart LR
    A(("❄️ Snowflake\nTable")) -- "Data Stream /\nZero-Copy" --> B(("☁️ Data Cloud\nDMO"))
    B -- "This tool" --> C(("☁️ Tableau\nSemantics"))
    C -- "Use in" --> D(("Tableau Next\nViz"))

    style A fill:#29B5E8,color:#fff,stroke:none
    style B fill:#032D60,color:#fff,stroke:none
    style C fill:#1a73e8,color:#fff,stroke:none
    style D fill:#34a853,color:#fff,stroke:none

    linkStyle 1 stroke:#1a73e8,stroke-width:3px
```

### 1. Snowflake

- Semantic Views must be enabled on your account
- Create a Semantic View on your source table (see [Usage](#1-create-a-snowflake-semantic-view))

### 2. Salesforce Data Cloud (Data 360)

- **Data must already be connected** to Data Cloud via Data Stream or Zero-Copy Partner Network
- A **DMO (Data Model Object)** must exist for the target table — this is created automatically when you map a data stream in Data Cloud
- This tool creates the *semantic model on top of* the DMO. It does not create the data pipeline itself
- DMO naming convention: `{TABLE_NAME}__dlm` (e.g., `FCT_SALES_DAILY__dlm`)

### 3. Salesforce Connected App

- Configured for **JWT Bearer Flow**
- **PKCS#8 private key** registered with the app
- `SF_USERNAME` must be pre-authorized

### 4. AWS

- Lambda execution role with **Secrets Manager read access**
- VPC may be required for Snowflake PrivateLink

## What Gets Created

```mermaid
flowchart LR
    LV(("Logical View\nMY_TABLE_lv"))
    DO(("Data Object\nMY_TABLE1"))
    DIM(("CUSTOMER_ID\nText · Discrete"))
    DATE(("ORDER_DATE\nDate · Continuous"))
    REV(("REVENUE\nNumber · Sum"))
    MTC(("REVENUE_mtc\nSum · TimeGrains"))

    LV --> DO
    DO --> DIM & DATE & REV
    REV -.- MTC
    DATE -.- MTC

    style LV fill:#34a853,color:#fff,stroke:none
    style DO fill:#fbbc04,color:#000,stroke:none
    style DIM fill:#e3f2fd,color:#000,stroke:#90caf9
    style DATE fill:#e3f2fd,color:#000,stroke:#90caf9
    style REV fill:#e8f5e9,color:#000,stroke:#a5d6a7
    style MTC fill:#ea4335,color:#fff,stroke:none
```

Each Snowflake `FACT` gets a corresponding **Measure** and **Metric** with:
- Time dimension reference (auto-detected from `DATE` columns)
- Additional dimensions (auto-detected from `VARCHAR` columns)
- Insights settings (sentiment, top contributors, trend alerts)

## Usage

### 1. Create a Snowflake Semantic View

```sql
CREATE OR REPLACE SEMANTIC VIEW my_db.my_schema.SV_FCT_SALES
  tables (
    FCT_SALES AS my_db.my_schema.FCT_SALES_DAILY
      primary key (CUSTOMER_ID, SALE_DATE)
  )
  facts (
    FCT_SALES.REVENUE AS REVENUE comment='Total revenue in USD',
    FCT_SALES.QUANTITY AS QUANTITY comment='Units sold'
  )
  dimensions (
    FCT_SALES.CUSTOMER_ID AS CUSTOMER_ID comment='Customer identifier',
    FCT_SALES.SALE_DATE AS SALE_DATE comment='Date of sale'
  )
  metrics (
    FCT_SALES.TOTAL_REVENUE AS SUM(FCT_SALES.REVENUE) comment='Sum of revenue'
  )
  comment='Daily sales fact table';
```

### 2. Deploy the Lambda

```bash
# Install dependencies for Lambda (Linux x86_64)
pip install snowflake-connector-python cryptography \
  -t package --platform manylinux2014_x86_64 \
  --only-binary=:all: --python-version 3.12

# Package
cd package && zip -r9 ../function.zip . && cd ..
zip -g function.zip lambda_function.py

# Deploy
aws lambda create-function \
  --function-name sf-semantic-sync \
  --runtime python3.12 \
  --role arn:aws:iam::ACCOUNT_ID:role/YOUR_ROLE \
  --handler lambda_function.lambda_handler \
  --timeout 900 --memory-size 256 \
  --zip-file fileb://function.zip \
  --environment "Variables={
    SNOWFLAKE_ACCOUNT=myorg.us-east-1.aws,
    SNOWFLAKE_USER=SVC_USER,
    SNOWFLAKE_DATABASE=MY_DB,
    SNOWFLAKE_SCHEMA=MY_SCHEMA,
    SNOWFLAKE_ROLE=MY_ROLE,
    SNOWFLAKE_WAREHOUSE=MY_WH,
    SNOWFLAKE_SECRET_ID=my_snowflake_secret,
    SNOWFLAKE_REGION=us-east-1,
    SF_CLIENT_ID=3MVG9...,
    SF_USERNAME=user@example.com,
    SF_PRIVATE_KEY=-----BEGIN PRIVATE KEY-----...,
    SF_DOMAIN=login.salesforce.com
  }"
```

### 3. Configure Sync Targets

Set the `SYNC_TARGETS` environment variable with a JSON array. Each entry only needs the Semantic View name and workspace — the model name is auto-derived.

```json
[
  {"semantic_view": "MY_DB.MY_SCHEMA.SV_FCT_SALES", "sf_workspace": "Sales"},
  {"semantic_view": "MY_DB.MY_SCHEMA.SV_FCT_USAGE", "sf_workspace": "Analytics"}
]
```

> **Naming rule**: `SV_FCT_SALES` → model name `FCT_SALES_SEMANTIC` (strips `SV_` prefix, adds `_SEMANTIC` suffix)

### 4. Run Sync

```bash
# Sync all targets defined in SYNC_TARGETS
aws lambda invoke --function-name sf-semantic-sync \
  --cli-binary-format raw-in-base64-out \
  --payload '{"action": "sync_all"}' response.json
```

```json
{
  "statusCode": 200,
  "results": [
    {"model": "FCT_SALES_SEMANTIC", "status": "ok",
     "stats": {"model": "created", "logical_view": "created",
               "dims_synced": 2, "measures_synced": 2, "metrics_synced": 2}},
    {"model": "FCT_USAGE_SEMANTIC", "status": "ok",
     "stats": {"model": "created", "logical_view": "created",
               "dims_synced": 3, "measures_synced": 5, "metrics_synced": 5}}
  ]
}
```

Or sync a single view:

```bash
aws lambda invoke --function-name sf-semantic-sync \
  --cli-binary-format raw-in-base64-out \
  --payload '{
    "action": "sync",
    "semantic_view": "MY_DB.MY_SCHEMA.SV_FCT_SALES",
    "sf_workspace": "Sales"
  }' response.json
```

## Actions

| Action | Description | Required Fields |
|---|---|---|
| `sync_all` | Sync all targets in `SYNC_TARGETS` env var **(default)** | _(none — reads env)_ |
| `sync` | Sync a single Semantic View | `semantic_view`, `sf_workspace` |
| `describe` | Inspect a Snowflake Semantic View definition | `semantic_view` |
| `list_sf_models` | List all Tableau Semantics models in the org | _(none)_ |

## Environment Variables

### Snowflake

| Variable | Description | Default |
|---|---|---|
| `SNOWFLAKE_ACCOUNT` | Snowflake account identifier | _(required)_ |
| `SNOWFLAKE_USER` | Service account username | _(required)_ |
| `SNOWFLAKE_DATABASE` | Database containing semantic views | _(required)_ |
| `SNOWFLAKE_SCHEMA` | Schema containing semantic views | _(required)_ |
| `SNOWFLAKE_ROLE` | Role with `SELECT` on semantic views | _(required)_ |
| `SNOWFLAKE_WAREHOUSE` | Warehouse for queries | _(required)_ |
| `SNOWFLAKE_SECRET_ID` | AWS Secrets Manager secret name | _(required)_ |
| `SNOWFLAKE_SECRET_KEY` | Key inside the secret JSON | `snowflake_password` |
| `SNOWFLAKE_REGION` | AWS region for Secrets Manager | `us-east-1` |

### Salesforce

| Variable | Description | Default |
|---|---|---|
| `SF_CLIENT_ID` | Connected App Consumer Key | _(required)_ |
| `SF_USERNAME` | Salesforce username (JWT sub) | _(required)_ |
| `SF_PRIVATE_KEY` | PKCS#8 private key (`\n` escaped) | _(required)_ |
| `SF_DOMAIN` | Salesforce login domain | `login.salesforce.com` |

### Sync Configuration

| Variable | Description |
|---|---|
| `SYNC_TARGETS` | JSON array of sync targets (see [Configure Sync Targets](#3-configure-sync-targets)) |

## Type Mapping

| Snowflake | Tableau Semantics | Display |
|---|---|---|
| `VARCHAR` `STRING` `TEXT` | Text | Discrete |
| `DATE` | Date | Continuous |
| `TIMESTAMP` | DateTime | Discrete |
| `NUMBER` `INTEGER` `BIGINT` | Number | Continuous |
| `FLOAT` `DOUBLE` `DECIMAL` | Number | Continuous |

## Naming Conventions (auto-generated)

| Concept | Pattern | Example |
|---|---|---|
| Model name | `{SV_NAME minus SV_}_SEMANTIC` | `FCT_SALES_SEMANTIC` |
| Data Object | `{TABLE}1` | `FCT_SALES_DAILY1` |
| Dimension / Measure | `{TABLE}1_{COLUMN}` | `FCT_SALES_DAILY1_REVENUE` |
| Logical View | `{TABLE}_lv` | `FCT_SALES_DAILY_lv` |
| DMO reference | `{TABLE}__dlm` | `FCT_SALES_DAILY__dlm` |
| Metric | `{COLUMN}_mtc` | `REVENUE_mtc` |
| DMO field | `{column}__c` | `revenue__c` |

## Limitations

- **Single-table only** — multi-table joins in Snowflake Semantic Views are not synced
- **No updates** — if the model already exists with a logical view, sync is skipped. Delete the model to re-sync
- **Sum aggregation** — all metrics default to `Sum`. Custom aggregation types are not yet mapped
- **DMO must exist** — the target DMO must be present in Data Cloud before running sync

## License

MIT
