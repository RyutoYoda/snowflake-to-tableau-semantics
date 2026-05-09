# snowflake-to-tableau-semantics

Sync **Snowflake Semantic View** definitions to **Tableau Semantics** (Salesforce Data Cloud) — dimensions, measures, and metrics — with a single Lambda invocation.

Define your semantic layer once in Snowflake. This tool creates the corresponding Tableau Semantics model automatically.

## Architecture

```mermaid
flowchart LR
    subgraph Snowflake
        SV["Semantic View<br/><small>DIMENSION · FACT · METRIC</small>"]
    end

    subgraph AWS
        Lambda["Lambda Function"]
        SM_AWS["Secrets Manager"]
    end

    subgraph Salesforce
        TS["Tableau Semantics Model"]
        D["Dimensions"]
        M["Measures"]
        MT["Metrics"]
    end

    SV -- "DESCRIBE<br/>SEMANTIC VIEW" --> Lambda
    SM_AWS -. credentials .-> Lambda
    Lambda -- "Authoring API<br/>(JWT Auth)" --> TS
    TS --> D & M & MT
```

## Prerequisites

Before using this tool, ensure the following are in place:

```mermaid
flowchart LR
    A["Snowflake Table"] -- "Data Stream /<br/>Zero-Copy" --> B["Data Cloud<br/>DMO"]
    B -- "This tool" --> C["Tableau Semantics<br/>Model"]
    C --> D["Tableau Next<br/>Visualizations"]

    style A fill:#29B5E8,color:#fff
    style B fill:#032D60,color:#fff
    style C fill:#1a73e8,color:#fff
    style D fill:#34a853,color:#fff
```

### 1. Snowflake

- Semantic Views must be enabled on your account
- Create a Semantic View on your source table (see [Usage](#1-create-a-snowflake-semantic-view))

### 2. Salesforce Data Cloud (Data 360)

- **Data must already be connected** to Data Cloud via Data Stream or Zero-Copy Partner Network
- A **DMO (Data Model Object)** must exist for the target table. The DMO is created automatically when you map your data stream in Data Cloud. This tool creates the *semantic model on top of* the DMO — it does not create the data pipeline itself
- The DMO naming convention is `{TABLE_NAME}__dlm` (e.g., `FCT_SALES_DAILY__dlm`)

### 3. Salesforce Connected App

- A Connected App configured for **JWT Bearer Flow**
- A **PKCS#8 private key** registered with the Connected App
- The user specified in `SF_USERNAME` must be pre-authorized

### 4. AWS

- A Lambda execution role with **Secrets Manager read access**
- VPC configuration may be required if Snowflake uses AWS PrivateLink

## What Gets Created

```mermaid
graph TD
    Model["Semantic Model<br/><i>MY_MODEL_SEMANTIC</i>"]
    LV["Logical View<br/><i>MY_TABLE_USER_ID_lv</i>"]
    DO["Data Object<br/><i>MY_TABLE1</i><br/>→ DMO: MY_TABLE__dlm"]
    D1["Dimension: CUSTOMER_ID<br/><small>Text · Discrete</small>"]
    D2["Dimension: ORDER_DATE<br/><small>Date · Continuous</small>"]
    M1["Measure: REVENUE<br/><small>Number · Sum</small>"]
    MT1["Metric: REVENUE_mtc<br/><small>Sum · TimeGrains · Insights</small>"]

    Model --> LV & MT1
    LV --> DO
    DO --> D1 & D2 & M1
    MT1 -.->|references| M1
    MT1 -.->|time dim| D2

    style Model fill:#1a73e8,color:#fff
    style LV fill:#34a853,color:#fff
    style DO fill:#fbbc04,color:#000
    style MT1 fill:#ea4335,color:#fff
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

### 3. Run Sync

```bash
aws lambda invoke \
  --function-name sf-semantic-sync \
  --cli-binary-format raw-in-base64-out \
  --payload '{
    "action": "sync",
    "semantic_view": "MY_DB.MY_SCHEMA.SV_FCT_SALES",
    "sf_model_name": "FCT_SALES_SEMANTIC",
    "sf_workspace": "Sales"
  }' response.json

cat response.json
```

```json
{
  "statusCode": 200,
  "stats": {
    "model": "created",
    "logical_view": "created",
    "dims_synced": 2,
    "measures_synced": 2,
    "metrics_synced": 2,
    "errors": []
  }
}
```

## Actions

| Action | Description | Required Fields |
|---|---|---|
| `sync` | Create/sync Tableau Semantics model from Snowflake SV | `semantic_view`, `sf_model_name`, `sf_workspace` |
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
| Data Object | `{TABLE}1` | `FCT_SALES_DAILY1` |
| Dimension/Measure | `{TABLE}1_{COLUMN}` | `FCT_SALES_DAILY1_REVENUE` |
| Logical View | `{TABLE}_USER_ID_lv` | `FCT_SALES_DAILY_USER_ID_lv` |
| DMO reference | `{TABLE}__dlm` | `FCT_SALES_DAILY__dlm` |
| Metric | `{COLUMN}_mtc` | `REVENUE_mtc` |
| DMO field | `{column}__c` | `revenue__c` |

## Limitations

- **Single-table only**: Multi-table joins in Snowflake Semantic Views are not synced
- **No updates**: If the model already exists with a logical view, sync is skipped. Delete the model to re-sync
- **Sum aggregation**: All metrics default to `Sum`. Custom aggregation types are not yet mapped
- **DMO must exist**: The target DMO must be present in Data Cloud before running sync

## License

MIT
