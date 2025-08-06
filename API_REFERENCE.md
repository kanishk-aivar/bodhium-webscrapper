# API Reference

This document provides detailed information about the APIs and interfaces used in the AWS Batch Web Scraper.

## Core Classes

### EnhancedWebCrawler

The main crawler class that orchestrates the entire web scraping process.

#### Constructor

```python
EnhancedWebCrawler(model_name: str, output_dir: str)
```

**Parameters:**
- `model_name` (str): The Gemini model to use (e.g., 'gemini-1.5-flash')
- `output_dir` (str): Base directory for storing crawl results

#### Methods

##### `setup_domain_directories(domain: str)`

Creates the directory structure for a specific domain.

**Parameters:**
- `domain` (str): The domain name to create directories for

**Creates:**
- `{output_dir}/{domain}/markdown/` - Markdown files
- `{output_dir}/{domain}/images/` - Screenshots
- `{output_dir}/{domain}/csv/` - CSV files
- `{output_dir}/{domain}/json/` - JSON files

##### `discover_all_urls(root_url: str) -> List[Dict[str, Any]]`

Discovers URLs from the target website using multiple strategies.

**Parameters:**
- `root_url` (str): The root URL to start discovery from

**Returns:**
- `List[Dict[str, Any]]`: List of discovered URLs with metadata

**Discovery Strategies:**
1. **Sitemap**: Attempts to find and parse sitemap.xml
2. **Common Crawl**: Uses Common Crawl data as backup
3. **Manual Discovery**: Falls back to common e-commerce URL patterns

##### `crawl_single_url(url: str, domain: str) -> Dict[str, Any]`

Crawls a single URL and extracts content.

**Parameters:**
- `url` (str): The URL to crawl
- `domain` (str): The domain name for file organization

**Returns:**
- `Dict[str, Any]`: Crawl results including success status, content, and metadata

**Features:**
- Takes screenshots of pages
- Extracts markdown content
- Handles timeouts and errors gracefully
- Saves content locally and to S3

##### `extract_products_from_content(content: str, url: str) -> List[Dict[str, Any]]`

Extracts product information from crawled content using Gemini AI.

**Parameters:**
- `content` (str): The markdown content to analyze
- `url` (str): The source URL for attribution

**Returns:**
- `List[Dict[str, Any]]`: List of extracted products

**Product Fields:**
- `productname`: Product name
- `description`: Product description
- `current_price`: Current price with currency
- `original_price`: Original price (if different)
- `rating`: Product rating
- `review`: Review information
- `image_url`: Product image URL
- `source_url`: Source URL

##### `deduplicate_products(products: List[Dict[str, Any]]) -> List[Dict[str, Any]]`

Removes duplicate products based on name and price.

**Parameters:**
- `products` (List[Dict[str, Any]]): List of products to deduplicate

**Returns:**
- `List[Dict[str, Any]]`: Deduplicated product list

##### `run(root_url: str) -> Dict[str, Any]`

Main execution method that orchestrates the entire crawling process.

**Parameters:**
- `root_url` (str): The root URL to crawl

**Returns:**
- `Dict[str, Any]`: Comprehensive results including statistics and file locations

## Data Models

### ProductInfo

```python
class ProductInfo(BaseModel):
    productname: str
    description: str
    current_price: str
    original_price: str
    rating: str
    review: str
    image_url: str
    source_url: str
```

### TokenUsage

```python
class TokenUsage(BaseModel):
    input_tokens: int
    output_tokens: int
    total_cost: float
    model_name: str
    timestamp: str
    pricing_tier: str
```

### CrawlMetrics

```python
class CrawlMetrics(BaseModel):
    url: str
    crawl_start_time: str
    crawl_end_time: str
    crawl_duration_ms: int
    content_length: int
    success: bool
    error_message: Optional[str] = None
    token_usage: Optional[TokenUsage] = None
```

## AWS Service Classes

### AWSService

Handles all AWS service interactions including S3 and DynamoDB operations.

#### Methods

##### `upload_to_s3(file_path: str, s3_key: str, content_type: str = None) -> str`

Uploads a file to S3.

**Parameters:**
- `file_path` (str): Local path to the file
- `s3_key` (str): S3 key for the file
- `content_type` (str, optional): MIME type of the file

**Returns:**
- `str`: S3 URL of the uploaded file

##### `upload_string_to_s3(content: str, s3_key: str, content_type: str = "text/plain") -> str`

Uploads string content directly to S3.

**Parameters:**
- `content` (str): String content to upload
- `s3_key` (str): S3 key for the content
- `content_type` (str): MIME type of the content

**Returns:**
- `str`: S3 URL of the uploaded content

##### `upload_json_to_s3(data: dict, s3_key: str) -> str`

Uploads JSON data to S3.

**Parameters:**
- `data` (dict): JSON data to upload
- `s3_key` (str): S3 key for the JSON file

**Returns:**
- `str`: S3 URL of the uploaded JSON

## Lambda Function API

### lambda_handler(event, context)

Main Lambda function handler for job submission.

#### Event Format

**Direct Invocation:**
```json
{
    "url": "https://example.com"
}
```

**API Gateway POST:**
```json
{
    "body": "{\"url\": \"https://example.com\"}"
}
```

**API Gateway GET:**
```json
{
    "queryStringParameters": {
        "url": "https://example.com"
    }
}
```

#### Response Format

**Success Response:**
```json
{
    "statusCode": 200,
    "headers": {
        "Content-Type": "application/json"
    },
    "body": {
        "status": "success",
        "message": "Job submitted successfully",
        "data": {
            "job_id": "batch-job-123",
            "job_name": "scrape-example-com",
            "submitted_at": "2024-01-15T14:30:25Z"
        }
    }
}
```

**Error Response:**
```json
{
    "statusCode": 400,
    "headers": {
        "Content-Type": "application/json"
    },
    "body": {
        "status": "error",
        "message": "Invalid URL provided"
    }
}
```

## Environment Variables

### Required Variables

| Variable | Description | Example |
|----------|-------------|---------|
| `S3_BUCKET_NAME` | S3 bucket for storing results | `my-crawl-results` |
| `GEMINI_SECRET_NAME` | Secrets Manager secret name | `Gemini-API-ChatGPT` |

### Optional Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `AWS_REGION` | AWS region | `us-east-1` |
| `DYNAMODB_TABLE_NAME` | DynamoDB table name | `OrchestrationLogs` |
| `MAX_URLS` | Maximum URLs to discover | `100` |
| `PAGE_TIMEOUT` | Page timeout in milliseconds | `60000` |
| `OUTPUT_DIR` | Local output directory | `/tmp/crawl_output` |
| `LOG_LEVEL` | Logging level | `INFO` |
| `S3_PREFIX` | S3 key prefix | `crawl-data` |

## Configuration Options

### Crawler Configuration

```python
config = CrawlerRunConfig(
    cache_mode=CacheMode.BYPASS,
    screenshot=True,
    word_count_threshold=10,
    only_text=False,
    process_iframes=True,
    wait_for_images=True,
    page_timeout=60000,
    exclude_external_links=True,
    excluded_tags=['script', 'style', 'nav', 'footer', 'header'],
    remove_overlay_elements=True
)
```

### Pricing Tiers

```python
pricing_tiers = {
    "standard": {
        "input": 0.075,      # $0.075 per 1M input tokens
        "output": 0.30,      # $0.30 per 1M output tokens
        "threshold": 128000  # ≤128k tokens
    },
    "large_context": {
        "input": 0.15,       # $0.15 per 1M input tokens
        "output": 0.60,      # $0.60 per 1M output tokens
        "threshold": float('inf')  # >128k tokens
    }
}
```

## Error Handling

### Common Error Types

1. **URL Discovery Errors**
   - Sitemap not found or invalid
   - Common Crawl data unavailable
   - Network connectivity issues

2. **Crawling Errors**
   - Page timeout
   - JavaScript rendering issues
   - Anti-bot protection

3. **AI Extraction Errors**
   - Invalid API key
   - Rate limiting
   - Malformed responses

4. **AWS Service Errors**
   - S3 upload failures
   - DynamoDB write errors
   - Secrets Manager access issues

### Error Response Format

```python
{
    "status": "failed",
    "error": "Error description",
    "error_type": "ERROR_CATEGORY",
    "timestamp": "2024-01-15T14:30:25Z",
    "job_id": "batch-job-123"
}
```

## Logging

### Log Levels

- **DEBUG**: Detailed debugging information
- **INFO**: General information about progress
- **WARNING**: Non-critical issues
- **ERROR**: Critical errors that may affect functionality

### Log Format

```
2024-01-15 14:30:25,123 - INFO - 🚀 Starting crawl for: https://example.com
2024-01-15 14:30:25,456 - INFO - 🔍 Discovered 15 URLs from domain example.com
2024-01-15 14:30:26,789 - INFO - ✅ Extracted 8 products from https://example.com/products
```

### CloudWatch Integration

All logs are automatically sent to CloudWatch with the following log group:
- `/aws/batch/web-scraper`

## Performance Metrics

### Key Metrics Tracked

1. **Discovery Metrics**
   - URLs discovered per source
   - Discovery time per strategy
   - Success rate by discovery method

2. **Crawling Metrics**
   - Pages crawled successfully
   - Average crawl time per page
   - Content length distribution

3. **AI Extraction Metrics**
   - Products extracted per page
   - Token usage and costs
   - Extraction accuracy

4. **Storage Metrics**
   - Files uploaded to S3
   - Storage usage
   - Upload times

### Monitoring Queries

```bash
# Check job status
aws batch describe-jobs --jobs <JOB-ID>

# Monitor CloudWatch logs
aws logs filter-log-events \
    --log-group-name "/aws/batch/web-scraper" \
    --start-time $(date -d '1 hour ago' +%s)000

# Check DynamoDB metrics
aws dynamodb scan \
    --table-name OrchestrationLogs \
    --filter-expression "pk = :pk" \
    --expression-attribute-values '{":pk":{"S":"<JOB-ID>"}}'
```

## Security Considerations

### IAM Permissions

Minimum required permissions for Batch job role:

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
                "arn:aws:s3:::your-bucket",
                "arn:aws:s3:::your-bucket/*"
            ]
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
                "secretsmanager:GetSecretValue"
            ],
            "Resource": "arn:aws:secretsmanager:*:*:secret:Gemini-API-ChatGPT*"
        }
    ]
}
```

### Data Encryption

- S3: Server-side encryption (AES256)
- DynamoDB: Encryption at rest
- Secrets Manager: Automatic encryption
- In-transit: TLS 1.2+ for all communications 