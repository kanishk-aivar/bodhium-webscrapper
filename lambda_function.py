import json
import os
import boto3
import uuid
import logging
from datetime import datetime
from botocore.exceptions import ClientError

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

def log_orchestration_event(event_name: str, details: dict = None, job_id: str = None):
    """Log orchestration events to DynamoDB"""
    try:
        # Use the DynamoDB table name from environment
        dynamodb_table_name = os.environ.get('DYNAMODB_TABLE_NAME', 'OrchestrationLogs')
        
        # Create DynamoDB client
        dynamodb = boto3.resource('dynamodb')
        table = dynamodb.Table(dynamodb_table_name)
        
        # Generate job_id if not provided
        if not job_id:
            job_id = f"lambda-{int(datetime.now().timestamp())}"
        
        # Create timestamp with unique ID
        timestamp = datetime.now().isoformat()
        unique_id = str(uuid.uuid4())[:8]
        event_timestamp_id = f"{timestamp}#{unique_id}"
        
        # Prepare log data
        log_data = {
            "pk": job_id,  # Partition key
            "sk": event_timestamp_id,  # Sort key
            "job_id": job_id,
            "event_timestamp_id": event_timestamp_id,
            "eventName": event_name,
            "details": details or {}
        }
        
        # Convert float values to Decimal for DynamoDB
        import decimal
        def convert_floats(obj):
            if isinstance(obj, float):
                return decimal.Decimal(str(obj))
            elif isinstance(obj, dict):
                return {k: convert_floats(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [convert_floats(v) for v in obj]
            return obj
        
        log_data = convert_floats(log_data)
        
        # Put item in DynamoDB
        response = table.put_item(Item=log_data)
        logger.info(f"Logged orchestration event: {event_name} for job {job_id}")
        return True
        
    except Exception as e:
        logger.error(f"Failed to log orchestration event: {e}")
        return False

def lambda_handler(event, context):
    """
    Lambda handler that accepts a URL and submits an AWS Batch job
    
    Expected input format:
    - API Gateway POST with JSON body: {"url": "https://example.com"}
    - Direct invocation with event: {"url": "https://example.com"}
    - Query parameters: ?url=https://example.com
    """
    try:
        # Generate a unique job ID for this request
        job_id = f"lambda-{int(datetime.now().timestamp())}-{str(uuid.uuid4())[:8]}"
        
        # Log job initiated event
        log_orchestration_event("JobInitiated", {
            "lambda_function": context.function_name if context else "unknown",
            "request_id": context.aws_request_id if context else "unknown",
            "timestamp": datetime.now().isoformat()
        }, job_id)
        
        # Extract URL from various possible sources
        url = None
        
        # Check if event is from API Gateway
        if 'body' in event:
            try:
                # Handle API Gateway POST request with JSON body
                body = event['body']
                if isinstance(body, str):
                    body = json.loads(body)
                url = body.get('url')
            except (json.JSONDecodeError, AttributeError):
                pass
        
        # Check query parameters (API Gateway GET)
        if not url and 'queryStringParameters' in event and event['queryStringParameters']:
            url = event['queryStringParameters'].get('url')
        
        # Direct invocation
        if not url and 'url' in event:
            url = event['url']
        
        # Validate URL
        if not url:
            return {
                'statusCode': 400,
                'headers': {'Content-Type': 'application/json'},
                'body': json.dumps({
                    'status': 'error',
                    'message': 'URL parameter is required'
                })
            }
        
        # Ensure URL has protocol
        if not url.startswith('http'):
            url = 'https://' + url
        
        # Get configuration from environment variables
        batch_job_queue = os.environ.get('BATCH_JOB_QUEUE')
        batch_job_definition = os.environ.get('BATCH_JOB_DEFINITION')
        batch_job_name_prefix = os.environ.get('BATCH_JOB_NAME_PREFIX', 'bodhium-scrapper')
        
        if not batch_job_queue or not batch_job_definition:
            return {
                'statusCode': 500,
                'headers': {'Content-Type': 'application/json'},
                'body': json.dumps({
                    'status': 'error',
                    'message': 'Batch configuration is missing'
                })
            }
        
        # Generate a unique job name
        timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
        unique_id = str(uuid.uuid4())[:8]
        job_name = f"{batch_job_name_prefix}-{timestamp}-{unique_id}"
        
        # Collect mandatory environment variables to pass to the batch job
        batch_env_vars = [
            # Required environment variables
            {'name': 'CRAWL_URL', 'value': url},
            
            # AWS configuration
            {'name': 'AWS_REGION', 'value': os.environ.get('AWS_REGION', 'us-east-1')},
            {'name': 'S3_BUCKET_NAME', 'value': os.environ.get('S3_BUCKET_NAME')},
            {'name': 'DYNAMODB_TABLE_NAME', 'value': os.environ.get('DYNAMODB_TABLE_NAME', 'OrchestrationLogs')},
            
            # Gemini API configuration
            {'name': 'GEMINI_SECRET_NAME', 'value': os.environ.get('GEMINI_SECRET_NAME', 'Gemini-API-ChatGPT')},
            {'name': 'GEMINI_SECRET_REGION', 'value': os.environ.get('GEMINI_SECRET_REGION', 'us-east-1')}
        ]
        
        # Filter out None values
        batch_env_vars = [env for env in batch_env_vars if env['value'] is not None]
        
        # Submit the batch job - USING URL DIRECTLY AS COMMAND LINE ARGUMENT
        batch_client = boto3.client('batch')
        response = batch_client.submit_job(
            jobName=job_name,
            jobQueue=batch_job_queue,
            jobDefinition=batch_job_definition,
            containerOverrides={
                'environment': batch_env_vars,
                'command': ['python', 'app.py', '--url', url]
            }
        )
        
        job_id = response['jobId']
        logger.info(f"Submitted batch job {job_name} with ID {job_id} for URL: {url}")
        
        # Log job successful event
        log_orchestration_event("JobSuccessful", {
            "batch_job_id": job_id,
            "batch_job_name": job_name,
            "url": url,
            "batch_job_queue": batch_job_queue,
            "batch_job_definition": batch_job_definition,
            "timestamp": datetime.now().isoformat()
        }, job_id)
        
        # Return success response
        return {
            'statusCode': 200,
            'headers': {'Content-Type': 'application/json'},
            'body': json.dumps({
                'status': 'success',
                'message': 'Batch job submitted successfully',
                'data': {
                    'job_id': job_id,
                    'job_name': job_name,
                    'url': url,
                    'batch_job_queue': batch_job_queue,
                    'batch_job_definition': batch_job_definition,
                    'timestamp': datetime.now().isoformat()
                }
            })
        }
        
    except Exception as e:
        logger.error(f"Error submitting batch job: {str(e)}")
        
        # Log job failed event
        log_orchestration_event("JobFailed", {
            "error": str(e),
            "timestamp": datetime.now().isoformat()
        }, job_id if 'job_id' in locals() else None)
        
        return {
            'statusCode': 500,
            'headers': {'Content-Type': 'application/json'},
            'body': json.dumps({
                'status': 'error',
                'message': f'Failed to submit batch job: {str(e)}'
            })
        }
