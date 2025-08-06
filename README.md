# AWS Batch Web Scraper

A sophisticated, production-ready web scraping solution built on AWS Batch that crawls e-commerce websites, extracts product information using Google's Gemini AI, and stores results in S3 and DynamoDB.

## 🚀 Features

- **Intelligent URL Discovery**: Automatically discovers URLs using sitemaps, Common Crawl, and manual fallback
- **AI-Powered Product Extraction**: Uses Google Gemini 1.5 Flash to extract structured product data
- **Comprehensive Data Storage**: Saves results to S3 (CSV, JSON, Markdown, screenshots) and DynamoDB
- **Cost Optimization**: Implements tiered pricing for token usage tracking
- **Robust Error Handling**: Graceful failure handling with detailed logging
- **Scalable Architecture**: Built on AWS Batch for high-performance processing
- **Multi-Format Output**: Generates CSV, JSON, Markdown, and visual screenshots

## 📋 Prerequisites

### AWS Services Required
- **AWS Batch**: For job execution
- **S3 Bucket**: For storing crawl results
- **DynamoDB Table**: For orchestration logging
- **AWS Secrets Manager**: For storing Gemini API key
- **Lambda Function**: For job submission (optional)
- **ECR Repository**: For Docker image storage

### API Keys
- **Google Gemini API Key**: Required for AI-powered product extraction

## 🏗️ Architecture

```
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│   API Gateway   │───▶│   Lambda        │───▶│   AWS Batch     │
│   (Optional)    │    │   (Job Submit)  │    │   (Execution)   │
└─────────────────┘    └─────────────────┘    └─────────────────┘
                                                       │
                                                       ▼
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│   DynamoDB      │◀───│   S3 Bucket     │◀───│   Web Scraper   │
│   (Logs)        │    │   (Results)     │    │   (Docker)      │
└─────────────────┘    └─────────────────┘    └─────────────────┘
```

## 🛠️ Installation & Setup

### 1. Clone the Repository
```bash
git clone <your-repo-url>
cd "Batch Without RDS"
```

### 2. Set Up AWS Infrastructure

#### Create S3 Bucket
```bash
aws s3 mb s3://your-crawl-results-bucket
```

#### Create DynamoDB Table
```bash
aws dynamodb create-table \
    --table-name OrchestrationLogs \
    --attribute-definitions \
        AttributeName=pk,AttributeType=S \
        AttributeName=sk,AttributeType=S \
    --key-schema \
        AttributeName=pk,KeyType=HASH \
        AttributeName=sk,KeyType=RANGE \
    --billing-mode PAY_PER_REQUEST
```

#### Store Gemini API Key in Secrets Manager
```bash
aws secretsmanager create-secret \
    --name "Gemini-API-ChatGPT" \
    --description "Google Gemini API Key for web scraper" \
    --secret-string '{"GEMINI_API_KEY":"your-api-key-here"}'
```

### 3. Build and Push Docker Image

#### Create ECR Repository
```bash
aws ecr create-repository --repository-name web-scraper-batch
```

#### Build and Push Image
```bash
# Get ECR login token
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin <account-id>.dkr.ecr.us-east-1.amazonaws.com

# Build image
docker build -t web-scraper-batch .

# Tag image
docker tag web-scraper-batch:latest <account-id>.dkr.ecr.us-east-1.amazonaws.com/web-scraper-batch:latest

# Push image
docker push <account-id>.dkr.ecr.us-east-1.amazonaws.com/web-scraper-batch:latest
```

### 4. Create AWS Batch Job Definition

```json
{
    "jobDefinitionName": "web-scraper-job",
    "type": "container",
    "containerProperties": {
        "image": "<account-id>.dkr.ecr.us-east-1.amazonaws.com/web-scraper-batch:latest",
        "vcpus": 2,
        "memory": 4096,
        "jobRoleArn": "arn:aws:iam::<account-id>:role/BatchJobRole",
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
            }
        ]
    }
}
```

### 5. Create IAM Role for Batch Jobs

Create an IAM role with the following permissions:
- S3 read/write access to your bucket
- DynamoDB read/write access to OrchestrationLogs table
- Secrets Manager read access for Gemini API key
- CloudWatch Logs write access

## 🚀 Usage

### Method 1: Direct AWS Batch Job Submission

```bash
aws batch submit-job \
    --job-name "scrape-example-com" \
    --job-queue "your-job-queue" \
    --job-definition "web-scraper-job" \
    --parameters '{"url":"https://example.com"}'
```

### Method 2: Using Lambda Function (Recommended)

Deploy the `lambda_function.py` as a Lambda function and invoke it:

```bash
aws lambda invoke \
    --function-name web-scraper-lambda \
    --payload '{"url":"https://example.com"}' \
    response.json
```

### Method 3: API Gateway Integration

Set up API Gateway to trigger the Lambda function:

```bash
# POST request to API Gateway
curl -X POST https://your-api-gateway-url/prod/scrape \
    -H "Content-Type: application/json" \
    -d '{"url":"https://example.com"}'
```

## 📊 Output Structure

### S3 Bucket Organization
```
your-crawl-results-bucket/
├── crawl-data/
│   └── example.com/
│       └── 2024-01-15_14-30-25/
│           ├── markdown/
│           │   ├── index.md
│           │   └── products.md
│           ├── images/
│           │   ├── index.png
│           │   └── products.png
│           ├── csv/
│           │   ├── example.com_all_products.csv
│           │   └── example.com_unique_products.csv
│           ├── json/
│           │   └── example.com_crawl_results.json
│           └── tree.md
```

### DynamoDB Logs
The system logs orchestration events to DynamoDB with the following structure:
- **Partition Key (pk)**: Job ID
- **Sort Key (sk)**: Timestamp with unique ID
- **Event Types**: JobInitiated, ScrapingStarted, ScrapingCompleted, ScrapingFailed

## ⚙️ Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `S3_BUCKET_NAME` | S3 bucket for storing results | Required |
| `DYNAMODB_TABLE_NAME` | DynamoDB table for logs | OrchestrationLogs |
| `GEMINI_SECRET_NAME` | Secrets Manager secret name | Gemini-API-ChatGPT |
| `AWS_REGION` | AWS region | us-east-1 |
| `MAX_URLS` | Maximum URLs to discover | 100 |
| `PAGE_TIMEOUT` | Page timeout in milliseconds | 60000 |
| `OUTPUT_DIR` | Local output directory | /tmp/crawl_output |
| `LOG_LEVEL` | Logging level | INFO |

### Pricing Configuration

The system uses tiered pricing for Gemini API:
- **Standard Tier**: ≤128k tokens ($0.075/1M input, $0.30/1M output)
- **Large Context Tier**: >128k tokens ($0.15/1M input, $0.60/1M output)

## 🔧 Customization

### Adding New URL Discovery Methods

Edit the `discover_all_urls` method in `app.py`:

```python
# Add custom URL patterns
custom_paths = [
    "/custom-path-1",
    "/custom-path-2"
]
```

### Modifying Product Extraction

Update the prompt in `extract_products_from_content`:

```python
prompt = f"""Your custom extraction prompt here..."""
```

### Adding New Output Formats

Extend the `save_*` methods in the `EnhancedWebCrawler` class.

## 📈 Monitoring & Logging

### CloudWatch Logs
All application logs are sent to CloudWatch with detailed information about:
- URL discovery process
- Crawling progress
- Product extraction results
- Token usage and costs
- Error details

### DynamoDB Metrics
Track orchestration events and job status in DynamoDB:
- Job initiation and completion
- Success/failure rates
- Processing times
- Resource usage

### S3 Analytics
Monitor storage usage and access patterns in S3.

## 🚨 Troubleshooting

### Common Issues

1. **Docker Build Failures**
   - Ensure you have sufficient disk space
   - Check internet connectivity for package downloads
   - Verify Docker daemon is running

2. **AWS Batch Job Failures**
   - Check IAM permissions
   - Verify ECR image exists and is accessible
   - Review CloudWatch logs for detailed error messages

3. **Token Usage Issues**
   - Verify Gemini API key is valid
   - Check Secrets Manager permissions
   - Monitor API rate limits

4. **S3 Upload Failures**
   - Verify bucket permissions
   - Check bucket exists and is accessible
   - Ensure sufficient storage space

### Debug Mode

Enable debug logging by setting:
```bash
export LOG_LEVEL=DEBUG
```

## 🔒 Security Considerations

- **API Key Management**: Store Gemini API key in AWS Secrets Manager
- **IAM Roles**: Use least-privilege IAM roles for Batch jobs
- **Network Security**: Consider VPC configuration for enhanced security
- **Data Encryption**: Enable S3 encryption and DynamoDB encryption at rest

## 📝 License

[Your License Here]

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Add tests if applicable
5. Submit a pull request

## 📞 Support

For issues and questions:
- Create an issue in the GitHub repository
- Check CloudWatch logs for detailed error information
- Review the troubleshooting section above

## 🔄 Version History

- **v1.0.0**: Initial release with basic web scraping functionality
- **v1.1.0**: Added AI-powered product extraction
- **v1.2.0**: Enhanced error handling and logging
- **v1.3.0**: Added DynamoDB orchestration logging
- **v1.4.0**: Improved token usage tracking and cost optimization 