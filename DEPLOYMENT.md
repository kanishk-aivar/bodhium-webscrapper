# Deployment Guide

This guide provides step-by-step instructions for deploying the AWS Batch Web Scraper to your AWS environment.

## Prerequisites

- AWS CLI configured with appropriate permissions
- Docker installed and running
- Python 3.8+ (for local testing)
- Access to AWS services: Batch, S3, DynamoDB, Secrets Manager, ECR, Lambda, IAM

## Step 1: AWS Infrastructure Setup

### 1.1 Create S3 Bucket

```bash
# Create bucket for crawl results
aws s3 mb s3://your-crawl-results-bucket

# Enable versioning (optional but recommended)
aws s3api put-bucket-versioning \
    --bucket your-crawl-results-bucket \
    --versioning-configuration Status=Enabled

# Enable encryption (recommended)
aws s3api put-bucket-encryption \
    --bucket your-crawl-results-bucket \
    --server-side-encryption-configuration '{
        "Rules": [
            {
                "ApplyServerSideEncryptionByDefault": {
                    "SSEAlgorithm": "AES256"
                }
            }
        ]
    }'
```

### 1.2 Create DynamoDB Table

```bash
aws dynamodb create-table \
    --table-name OrchestrationLogs \
    --attribute-definitions \
        AttributeName=pk,AttributeType=S \
        AttributeName=sk,AttributeType=S \
    --key-schema \
        AttributeName=pk,KeyType=HASH \
        AttributeName=sk,KeyType=RANGE \
    --billing-mode PAY_PER_REQUEST \
    --tags Key=Project,Value=WebScraper
```

### 1.3 Store Gemini API Key

```bash
# Create secret in Secrets Manager
aws secretsmanager create-secret \
    --name "Gemini-API-ChatGPT" \
    --description "Google Gemini API Key for web scraper" \
    --secret-string '{"GEMINI_API_KEY":"your-actual-api-key-here"}' \
    --tags Key=Project,Value=WebScraper
```

## Step 2: IAM Setup

### 2.1 Create IAM Role for Batch Jobs

Create a file `batch-job-role-policy.json`:

```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "s3:GetObject",
                "s3:PutObject",
                "s3:ListBucket"
            ],
            "Resource": [
                "arn:aws:s3:::your-crawl-results-bucket",
                "arn:aws:s3:::your-crawl-results-bucket/*"
            ]
        },
        {
            "Effect": "Allow",
            "Action": [
                "dynamodb:GetItem",
                "dynamodb:PutItem",
                "dynamodb:Query",
                "dynamodb:Scan"
            ],
            "Resource": "arn:aws:dynamodb:*:*:table/OrchestrationLogs"
        },
        {
            "Effect": "Allow",
            "Action": [
                "secretsmanager:GetSecretValue"
            ],
            "Resource": "arn:aws:secretsmanager:*:*:secret:Gemini-API-ChatGPT*"
        },
        {
            "Effect": "Allow",
            "Action": [
                "logs:CreateLogGroup",
                "logs:CreateLogStream",
                "logs:PutLogEvents"
            ],
            "Resource": "arn:aws:logs:*:*:*"
        }
    ]
}
```

Create the policy and role:

```bash
# Create policy
aws iam create-policy \
    --policy-name WebScraperBatchPolicy \
    --policy-document file://batch-job-role-policy.json

# Create role
aws iam create-role \
    --role-name WebScraperBatchRole \
    --assume-role-policy-document '{
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {
                    "Service": "ecs-tasks.amazonaws.com"
                },
                "Action": "sts:AssumeRole"
            }
        ]
    }'

# Attach policy to role
aws iam attach-role-policy \
    --role-name WebScraperBatchRole \
    --policy-arn arn:aws:iam::<ACCOUNT-ID>:policy/WebScraperBatchPolicy
```

### 2.2 Create IAM Role for Lambda (Optional)

If using Lambda for job submission, create `lambda-role-policy.json`:

```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "batch:SubmitJob",
                "batch:DescribeJobs"
            ],
            "Resource": "*"
        },
        {
            "Effect": "Allow",
            "Action": [
                "dynamodb:PutItem"
            ],
            "Resource": "arn:aws:dynamodb:*:*:table/OrchestrationLogs"
        },
        {
            "Effect": "Allow",
            "Action": [
                "logs:CreateLogGroup",
                "logs:CreateLogStream",
                "logs:PutLogEvents"
            ],
            "Resource": "arn:aws:logs:*:*:*"
        }
    ]
}
```

```bash
# Create Lambda policy and role
aws iam create-policy \
    --policy-name WebScraperLambdaPolicy \
    --policy-document file://lambda-role-policy.json

aws iam create-role \
    --role-name WebScraperLambdaRole \
    --assume-role-policy-document '{
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {
                    "Service": "lambda.amazonaws.com"
                },
                "Action": "sts:AssumeRole"
            }
        ]
    }'

aws iam attach-role-policy \
    --role-name WebScraperLambdaRole \
    --policy-arn arn:aws:iam::<ACCOUNT-ID>:policy/WebScraperLambdaPolicy
```

## Step 3: Docker Image Setup

### 3.1 Create ECR Repository

```bash
aws ecr create-repository \
    --repository-name web-scraper-batch \
    --image-scanning-configuration scanOnPush=true \
    --tags Key=Project,Value=WebScraper
```

### 3.2 Build and Push Image

```bash
# Get your account ID
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REGION=us-east-1

# Login to ECR
aws ecr get-login-password --region $REGION | \
docker login --username AWS --password-stdin $ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com

# Build image
docker build -t web-scraper-batch .

# Tag image
docker tag web-scraper-batch:latest $ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com/web-scraper-batch:latest

# Push image
docker push $ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com/web-scraper-batch:latest
```

## Step 4: AWS Batch Setup

### 4.1 Create Compute Environment

Create `compute-environment.json`:

```json
{
    "computeEnvironmentName": "web-scraper-compute-env",
    "type": "MANAGED",
    "state": "ENABLED",
    "computeResources": {
        "type": "SPOT",
        "maxvCpus": 256,
        "minvCpus": 0,
        "desiredvCpus": 0,
        "instanceTypes": ["optimal"],
        "subnets": ["subnet-xxxxxxxxx", "subnet-yyyyyyyyy"],
        "securityGroupIds": ["sg-xxxxxxxxx"],
        "instanceRole": "arn:aws:iam::<ACCOUNT-ID>:instance-profile/ecsInstanceRole",
        "tags": {
            "Project": "WebScraper"
        }
    },
    "serviceRole": "arn:aws:iam::<ACCOUNT-ID>:role/AWSBatchServiceRole"
}
```

```bash
aws batch create-compute-environment --cli-input-json file://compute-environment.json
```

### 4.2 Create Job Queue

```bash
aws batch create-job-queue \
    --job-queue-name web-scraper-job-queue \
    --state ENABLED \
    --priority 1 \
    --compute-environment-order order=1,computeEnvironment=arn:aws:batch:<REGION>:<ACCOUNT-ID>:compute-environment/web-scraper-compute-env
```

### 4.3 Create Job Definition

Create `job-definition.json`:

```json
{
    "jobDefinitionName": "web-scraper-job",
    "type": "container",
    "containerProperties": {
        "image": "<ACCOUNT-ID>.dkr.ecr.<REGION>.amazonaws.com/web-scraper-batch:latest",
        "vcpus": 2,
        "memory": 4096,
        "jobRoleArn": "arn:aws:iam::<ACCOUNT-ID>:role/WebScraperBatchRole",
        "environment": [
            {
                "name": "S3_BUCKET_NAME",
                "value": "your-crawl-results-bucket"
            },
            {
                "name": "DYNAMODB_TABLE_NAME",
                "value": "OrchestrationLogs"
            },
            {
                "name": "GEMINI_SECRET_NAME",
                "value": "Gemini-API-ChatGPT"
            },
            {
                "name": "AWS_REGION",
                "value": "us-east-1"
            },
            {
                "name": "MAX_URLS",
                "value": "100"
            },
            {
                "name": "PAGE_TIMEOUT",
                "value": "60000"
            }
        ],
        "logConfiguration": {
            "logDriver": "awslogs",
            "options": {
                "awslogs-group": "/aws/batch/web-scraper",
                "awslogs-region": "us-east-1",
                "awslogs-stream-prefix": "ecs"
            }
        }
    }
}
```

```bash
aws batch register-job-definition --cli-input-json file://job-definition.json
```

## Step 5: Lambda Setup (Optional)

### 5.1 Create Lambda Function

```bash
# Create deployment package
zip -r lambda-deployment.zip lambda_function.py

# Create Lambda function
aws lambda create-function \
    --function-name web-scraper-lambda \
    --runtime python3.9 \
    --role arn:aws:iam::<ACCOUNT-ID>:role/WebScraperLambdaRole \
    --handler lambda_function.lambda_handler \
    --zip-file fileb://lambda-deployment.zip \
    --timeout 30 \
    --memory-size 128 \
    --environment Variables='{
        "BATCH_JOB_QUEUE": "web-scraper-job-queue",
        "BATCH_JOB_DEFINITION": "web-scraper-job",
        "DYNAMODB_TABLE_NAME": "OrchestrationLogs"
    }'
```

### 5.2 Create API Gateway (Optional)

```bash
# Create REST API
aws apigateway create-rest-api \
    --name "Web Scraper API" \
    --description "API for submitting web scraping jobs"

# Get API ID and root resource ID
API_ID=$(aws apigateway get-rest-apis --query 'items[?name==`Web Scraper API`].id' --output text)
ROOT_ID=$(aws apigateway get-resources --rest-api-id $API_ID --query 'items[?path==`/`].id' --output text)

# Create resource
aws apigateway create-resource \
    --rest-api-id $API_ID \
    --parent-id $ROOT_ID \
    --path-part "scrape"

# Create POST method
aws apigateway put-method \
    --rest-api-id $API_ID \
    --resource-id <RESOURCE-ID> \
    --http-method POST \
    --authorization-type NONE

# Set up Lambda integration
aws apigateway put-integration \
    --rest-api-id $API_ID \
    --resource-id <RESOURCE-ID> \
    --http-method POST \
    --type AWS_PROXY \
    --integration-http-method POST \
    --uri arn:aws:apigateway:<REGION>:lambda:path/2015-03-31/functions/arn:aws:lambda:<REGION>:<ACCOUNT-ID>:function:web-scraper-lambda/invocations

# Deploy API
aws apigateway create-deployment \
    --rest-api-id $API_ID \
    --stage-name prod
```

## Step 6: Testing

### 6.1 Test Direct Batch Job

```bash
aws batch submit-job \
    --job-name "test-scrape" \
    --job-queue "web-scraper-job-queue" \
    --job-definition "web-scraper-job" \
    --parameters '{"url":"https://example.com"}'
```

### 6.2 Test Lambda Function

```bash
aws lambda invoke \
    --function-name web-scraper-lambda \
    --payload '{"url":"https://example.com"}' \
    response.json

cat response.json
```

### 6.3 Monitor Progress

```bash
# Check job status
aws batch describe-jobs --jobs <JOB-ID>

# Check CloudWatch logs
aws logs describe-log-groups --log-group-name-prefix "/aws/batch/web-scraper"

# Check DynamoDB logs
aws dynamodb scan --table-name OrchestrationLogs --filter-expression "pk = :pk" --expression-attribute-values '{":pk":{"S":"<JOB-ID>"}}'
```

## Step 7: Cleanup (Optional)

```bash
# Delete resources in reverse order
aws batch deregister-job-definition --job-definition web-scraper-job
aws batch delete-job-queue --job-queue web-scraper-job-queue
aws batch delete-compute-environment --compute-environment web-scraper-compute-env

# Delete Lambda and API Gateway
aws lambda delete-function --function-name web-scraper-lambda
aws apigateway delete-rest-api --rest-api-id $API_ID

# Delete ECR repository
aws ecr delete-repository --repository-name web-scraper-batch --force

# Delete IAM roles and policies
aws iam detach-role-policy --role-name WebScraperBatchRole --policy-arn arn:aws:iam::<ACCOUNT-ID>:policy/WebScraperBatchPolicy
aws iam delete-role --role-name WebScraperBatchRole
aws iam delete-policy --policy-arn arn:aws:iam::<ACCOUNT-ID>:policy/WebScraperBatchPolicy

# Delete S3 bucket (empty first)
aws s3 rm s3://your-crawl-results-bucket --recursive
aws s3 rb s3://your-crawl-results-bucket

# Delete DynamoDB table
aws dynamodb delete-table --table-name OrchestrationLogs

# Delete Secrets Manager secret
aws secretsmanager delete-secret --secret-id Gemini-API-ChatGPT --force-delete-without-recovery
```

## Troubleshooting

### Common Issues

1. **ECR Login Issues**
   ```bash
   # Ensure AWS CLI is configured correctly
   aws configure list
   
   # Check ECR permissions
   aws ecr describe-repositories
   ```

2. **Batch Job Failures**
   ```bash
   # Check job status
   aws batch describe-jobs --jobs <JOB-ID>
   
   # Check CloudWatch logs
   aws logs filter-log-events --log-group-name "/aws/batch/web-scraper" --start-time $(date -d '1 hour ago' +%s)000
   ```

3. **IAM Permission Issues**
   ```bash
   # Test IAM permissions
   aws sts get-caller-identity
   aws iam get-role --role-name WebScraperBatchRole
   ```

### Performance Optimization

1. **Adjust Batch Resources**
   - Increase `vcpus` and `memory` for larger websites
   - Use `SPOT` instances for cost optimization
   - Consider `FARGATE` for serverless execution

2. **Optimize Docker Image**
   - Use multi-stage builds
   - Minimize layer size
   - Cache dependencies

3. **Monitor Costs**
   - Set up CloudWatch alarms for Batch costs
   - Monitor S3 storage usage
   - Track DynamoDB read/write units 