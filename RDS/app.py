import asyncio
import json
import os
import time
import re
import sys
import argparse
from typing import List, Dict, Any, Set, Optional
from pydantic import BaseModel
from dotenv import load_dotenv
import google.generativeai as genai
from crawl4ai import AsyncWebCrawler, CrawlerRunConfig, CacheMode, AsyncUrlSeeder, SeedingConfig

import logging
from urllib.parse import urlparse, urljoin
from base64 import b64decode
import csv
from pathlib import Path
import hashlib
from datetime import datetime
import boto3
from botocore.exceptions import ClientError, NoCredentialsError
import io
import uuid
import psycopg2
from psycopg2.pool import ThreadedConnectionPool
from psycopg2.extras import RealDictCursor
import threading
from contextlib import contextmanager

# Set up logging
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()
logger.info("🚀 Starting application - Environment variables loaded")


def get_secret(secret_name="Gemini-API-ChatGPT", region_name="us-east-1"):
    """Retrieve a secret from AWS Secrets Manager"""
    logger.info(f"Retrieving secret '{secret_name}' from AWS Secrets Manager in {region_name}")
    try:
        session = boto3.session.Session()
        client = session.client(service_name='secretsmanager', region_name=region_name)
        get_secret_value_response = client.get_secret_value(SecretId=secret_name)
        secret_string = get_secret_value_response['SecretString']
        try:
            secret_dict = json.loads(secret_string)
            logger.info(f"Successfully retrieved secret as JSON: {list(secret_dict.keys())}")
            if "GEMINI_API_KEY" in secret_dict:
                logger.info("Found GEMINI_API_KEY in secret JSON")
                return secret_dict["GEMINI_API_KEY"]
            else:
                logger.warning("Secret found but missing GEMINI_API_KEY field")
                return None
        except json.JSONDecodeError:
            logger.info("Secret is not in JSON format, returning as plain text")
            return secret_string
    except ClientError as e:
        logger.error(f"Failed to retrieve secret: {e}")
        return None


def get_rds_secret():
    """Retrieve RDS credentials from AWS Secrets Manager"""
    secret_name = os.getenv("rds_secret", "dev/rds")
    region_name = os.getenv("AWS_REGION", "us-east-1")
    logger.info(f"🔑 Retrieving RDS credentials from AWS Secrets Manager: {secret_name}")
    try:
        # Create a Secrets Manager client
        session = boto3.session.Session()
        client = session.client(
            service_name='secretsmanager',
            region_name=region_name
        )
        get_secret_value_response = client.get_secret_value(
            SecretId=secret_name
        )
        secret_string = get_secret_value_response['SecretString']
        secret_dict = json.loads(secret_string)
        logger.info(f"✅ Successfully retrieved RDS secret with keys: {list(secret_dict.keys())}")

        # Validate required keys
        required_keys = ['DB_HOST', 'DB_NAME', 'DB_USER', 'DB_PASSWORD', 'DB_PORT']
        missing_keys = [key for key in required_keys if key not in secret_dict]
        if missing_keys:
            logger.error(f"❌ Missing required keys in RDS secret: {missing_keys}")
            raise ValueError(f"Missing required keys in RDS secret: {missing_keys}")

        return secret_dict
    except ClientError as e:
        logger.error(f"❌ Failed to retrieve RDS secret from Secrets Manager: {e}")
        raise
    except json.JSONDecodeError as e:
        logger.error(f"❌ Failed to parse RDS secret as JSON: {e}")
        raise
    except Exception as e:
        logger.error(f"❌ Unexpected error retrieving RDS secret: {e}")
        raise


class ProductInfo(BaseModel):
    productname: str
    description: str
    current_price: str
    original_price: str
    rating: str
    review: str
    image_url: str
    source_url: str

class TokenUsage(BaseModel):
    input_tokens: int
    output_tokens: int
    total_cost: float
    model_name: str
    timestamp: str
    pricing_tier: str

class CrawlMetrics(BaseModel):
    url: str
    crawl_start_time: str
    crawl_end_time: str
    crawl_duration_ms: int
    content_length: int
    success: bool
    error_message: Optional[str] = None
    token_usage: Optional[TokenUsage] = None

class DatabaseManager:
    """PostgreSQL RDS database manager with connection pooling"""

    def __init__(self):
        logger.info("🗄️ Initializing PostgreSQL Database Manager")
        self.pool = None
        self.lock = threading.Lock()
        self.rds_credentials = None
        self._get_rds_credentials()
        self._initialize_connection_pool()
        self._ensure_tables_exist()
        logger.info("✅ Database Manager initialized successfully")

    def _get_rds_credentials(self):
        """Get RDS credentials from AWS Secrets Manager"""
        logger.info("🔑 Retrieving RDS credentials from AWS Secrets Manager")
        try:
            self.rds_credentials = get_rds_secret()
            logger.info("✅ Successfully retrieved RDS credentials from Secrets Manager")
        except Exception as e:
            logger.error(f"❌ Failed to get RDS credentials: {e}")
            raise EnvironmentError(f"Failed to get RDS credentials: {e}")

    def _initialize_connection_pool(self):
        """Initialize PostgreSQL connection pool"""
        try:
            db_config = {
                'host': self.rds_credentials['DB_HOST'],
                'database': self.rds_credentials['DB_NAME'],
                'user': self.rds_credentials['DB_USER'],
                'password': self.rds_credentials['DB_PASSWORD'],
                'port': int(self.rds_credentials['DB_PORT']),
                'sslmode': os.getenv("DB_SSLMODE", "require"),
                'connect_timeout': int(os.getenv("DB_CONNECTION_TIMEOUT", "30"))
            }
            logger.info(f"🔗 Connecting to PostgreSQL RDS:")
            logger.info(f" Host: {db_config['host']}")
            logger.info(f" Database: {db_config['database']}")
            logger.info(f" User: {db_config['user']}")
            logger.info(f" Port: {db_config['port']}")
            logger.info(f" SSL Mode: {db_config['sslmode']}")
            min_conn = int(os.getenv("DB_MIN_CONNECTIONS", "2"))
            max_conn = int(os.getenv("DB_MAX_CONNECTIONS", "20"))
            self.pool = ThreadedConnectionPool(
                minconn=min_conn,
                maxconn=max_conn,
                **db_config
            )
            # Test connection
            with self.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT version();")
                    version = cur.fetchone()[0]
                    logger.info(f"✅ Connected to PostgreSQL: {version}")
        except Exception as e:
            logger.error(f"❌ Failed to initialize database connection pool: {e}")
            raise

    @contextmanager
    def get_connection(self):
        """Get a database connection from the pool"""
        conn = None
        try:
            with self.lock:
                conn = self.pool.getconn()
            yield conn
            conn.commit()
        except Exception as e:
            if conn:
                conn.rollback()
            logger.error(f"❌ Database operation failed: {e}")
            raise
        finally:
            if conn:
                with self.lock:
                    self.pool.putconn(conn)

    def _ensure_tables_exist(self):
        """Check if tables exist and create them if they don't"""
        logger.info("📋 Checking if database tables exist")
        try:
            with self.get_connection() as conn:
                with conn.cursor() as cur:
                    # Check if scrapejobs table exists
                    cur.execute("""
                        SELECT EXISTS (
                            SELECT FROM information_schema.tables
                            WHERE table_schema = 'public'
                              AND table_name = 'scrapejobs'
                        );
                    """)
                    scrapejobs_exists = cur.fetchone()[0]

                    # Check if products table exists
                    cur.execute("""
                        SELECT EXISTS (
                            SELECT FROM information_schema.tables
                            WHERE table_schema = 'public'
                              AND table_name = 'products'
                        );
                    """)
                    products_exists = cur.fetchone()[0]

                    # Check if jobselectedproducts table exists
                    cur.execute("""
                        SELECT EXISTS (
                            SELECT FROM information_schema.tables
                            WHERE table_schema = 'public'
                              AND table_name = 'jobselectedproducts'
                        );
                    """)
                    jobselectedproducts_exists = cur.fetchone()[0]

                    if scrapejobs_exists and products_exists and jobselectedproducts_exists:
                        logger.info("✅ All required tables already exist, skipping table creation")
                        return
                    else:
                        logger.info("📋 Some tables are missing, creating them...")
                        self._create_tables()
        except Exception as e:
            logger.error(f"❌ Error checking table existence: {e}")
            # If we can't check, try to create tables anyway
            logger.info("🔄 Attempting to create tables anyway...")
            self._create_tables()

    def _create_tables(self):
        """Create necessary database tables"""
        logger.info("📋 Creating database tables if they don't exist")
        create_tables_sql = """
        -- Create scrapejobs table
        CREATE TABLE IF NOT EXISTS scrapejobs (
            job_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            source_url TEXT,
            status VARCHAR(50),
            created_at TIMESTAMP WITH TIME ZONE,
            updated_at TIMESTAMP WITH TIME ZONE,
            brand_name VARCHAR(255),
            completed_at TIMESTAMP WITH TIME ZONE,
            error_message TEXT,
            metadata JSONB DEFAULT '{}'::jsonb
        );
        -- Create products table (same as before)
        CREATE TABLE IF NOT EXISTS products (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            product_name VARCHAR(500) NOT NULL,
            description TEXT,
            current_price VARCHAR(100),
            original_price VARCHAR(100),
            rating VARCHAR(50),
            review VARCHAR(200),
            image_url TEXT,
            source_url TEXT NOT NULL,
            domain VARCHAR(255) NOT NULL,
            brand VARCHAR(255),
            product_hash VARCHAR(64) UNIQUE NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            metadata JSONB DEFAULT '{}'::jsonb
        );
        -- Create jobselectedproducts table
        CREATE TABLE IF NOT EXISTS jobselectedproducts (
            job_id UUID REFERENCES scrapejobs(job_id) ON DELETE CASCADE,
            product_id UUID REFERENCES products(id) ON DELETE CASCADE,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (job_id, product_id)
        );
        -- Create indexes for better performance
        CREATE INDEX IF NOT EXISTS idx_scrapejobs_status ON scrapejobs(status);
        CREATE INDEX IF NOT EXISTS idx_scrapejobs_brand_name ON scrapejobs(brand_name);
        CREATE INDEX IF NOT EXISTS idx_products_domain ON products(domain);
        CREATE INDEX IF NOT EXISTS idx_products_source_url ON products(source_url);
        CREATE INDEX IF NOT EXISTS idx_products_product_hash ON products(product_hash);
        CREATE INDEX IF NOT EXISTS idx_jobselectedproducts_job_id ON jobselectedproducts(job_id);
        CREATE INDEX IF NOT EXISTS idx_jobselectedproducts_product_id ON jobselectedproducts(product_id);
        """


        # Create the trigger function separately
        trigger_function_sql = r"""
        CREATE OR REPLACE FUNCTION update_updated_at_column() RETURNS TRIGGER AS $update_timestamp$
        BEGIN
            NEW.updated_at = CURRENT_TIMESTAMP;
            RETURN NEW;
        END;
        $update_timestamp$ language 'plpgsql';
        """

        # Create triggers separately
        triggers_sql = """
        DROP TRIGGER IF EXISTS update_scrapejobs_updated_at ON scrapejobs;
        CREATE TRIGGER update_scrapejobs_updated_at
            BEFORE UPDATE ON scrapejobs
            FOR EACH ROW
            EXECUTE FUNCTION update_updated_at_column();
        DROP TRIGGER IF EXISTS update_products_updated_at ON products;
        CREATE TRIGGER update_products_updated_at
            BEFORE UPDATE ON products
            FOR EACH ROW
            EXECUTE FUNCTION update_updated_at_column();
        """

        try:
            with self.get_connection() as conn:
                with conn.cursor() as cur:
                    # Execute table creation
                    logger.info("📋 Creating tables...")
                    cur.execute(create_tables_sql)
                    # Execute trigger function creation
                    logger.info("📋 Creating trigger function...")
                    cur.execute(trigger_function_sql)
                    # Execute trigger creation
                    logger.info("📋 Creating triggers...")
                    cur.execute(triggers_sql)
                    logger.info("✅ Database tables created/verified successfully")
        except Exception as e:
            logger.error(f"❌ Failed to create database tables: {e}")
            # Don't raise the exception - continue without triggers if needed
            logger.warning("⚠️ Continuing without triggers - tables should still work")

    def normalize_job_id(self, job_id: str) -> str:
        """Convert string job_id to UUID format"""
        if not job_id:
            raise ValueError("Job ID cannot be empty")
        # Check if it's already a valid UUID
        try:
            uuid.UUID(job_id)
            logger.info(f"✅ Job ID is already valid UUID: {job_id}")
            return job_id
        except ValueError:
            pass
        # Convert string to UUID using namespace UUID
        logger.info(f"🔄 Converting non-UUID job_id to UUID: {job_id}")
        namespace = uuid.NAMESPACE_DNS
        generated_uuid = str(uuid.uuid5(namespace, job_id))
        logger.info(f"🔄 Generated UUID {generated_uuid} from string: {job_id}")
        return generated_uuid

    def job_exists(self, job_id: str) -> bool:
        """Check if a job exists in the database"""
        normalized_job_id = self.normalize_job_id(job_id)
        try:
            with self.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT EXISTS(SELECT 1 FROM scrapejobs WHERE job_id = %s)",
                        (normalized_job_id,)
                    )
                    exists = cur.fetchone()[0]
                    logger.info(f"🔍 Job {job_id} (normalized: {normalized_job_id}) exists: {exists}")
                    return exists
        except Exception as e:
            logger.error(f"❌ Error checking job existence: {e}")
            return False

    def create_scrape_job(self, job_id: str, url: str, domain: str, brand: str = None) -> str:
        """Create a new scrape job"""
        normalized_job_id = self.normalize_job_id(job_id)
        logger.info(f"📝 Creating scrape job: {normalized_job_id} for URL: {url}")
        now = datetime.now()
        try:
            with self.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO scrapejobs (job_id, source_url, status, created_at, updated_at, brand_name)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (job_id) DO UPDATE
                        SET source_url = EXCLUDED.source_url,
                            brand_name = EXCLUDED.brand_name,
                            updated_at = CURRENT_TIMESTAMP
                        RETURNING job_id
                    """, (
                        normalized_job_id,
                        url,
                        'PENDING',
                        now,
                        now,
                        brand
                    ))
                    result = cur.fetchone()
                    created_job_id = result[0] if result else normalized_job_id
                    logger.info(f"✅ Created scrape job: {created_job_id} with brand: {brand}")
                    return str(created_job_id)
        except Exception as e:
            logger.error(f"❌ Failed to create scrape job: {e}")
            raise

    def update_job_status(self, job_id: str, status: str, error_message: str = None):
        """Update job status"""
        normalized_job_id = self.normalize_job_id(job_id)
        logger.info(f"🔄 Updating job {job_id} (normalized: {normalized_job_id}) status to: {status}")
        try:
            with self.get_connection() as conn:
                with conn.cursor() as cur:
                    if status in ['JOB_SUCCESS', 'JOB_COMPLETED']:
                        cur.execute("""
                            UPDATE scrapejobs
                            SET status = %s, error_message = %s, completed_at = CURRENT_TIMESTAMP
                            WHERE job_id = %s
                        """, (status, error_message, normalized_job_id))
                    else:
                        cur.execute("""
                            UPDATE scrapejobs
                            SET status = %s, error_message = %s
                            WHERE job_id = %s
                        """, (status, error_message, normalized_job_id))
                    rows_updated = cur.rowcount
                    logger.info(f"✅ Updated {rows_updated} job(s) with status: {status}")
                    if rows_updated == 0:
                        logger.warning(f"⚠️ No jobs found to update with ID: {job_id}")
                    else:
                        logger.info(f"✅ Updated job {job_id} status to: {status}")
        except Exception as e:
            logger.error(f"❌ Failed to update job status: {e}")
            raise

    def ingest_products(self, job_id: str, products: List[Dict[str, Any]]) -> Dict[str, int]:
        """Ingest products into database with deduplication"""
        normalized_job_id = self.normalize_job_id(job_id)
        logger.info(f"📦 Ingesting {len(products)} products for job: {job_id} (normalized: {normalized_job_id})")
        if not products:
            logger.info("ℹ️ No products to ingest")
            return {"new_products": 0, "existing_products": 0, "jobselectedproducts_linked": 0}
        stats = {"new_products": 0, "existing_products": 0, "jobselectedproducts_linked": 0}
        try:
            with self.get_connection() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    for product in products:
                        # Create product hash for deduplication
                        product_key = f"{product.get('productname', '')}{product.get('source_url', '')}{product.get('current_price', '')}"
                        product_hash = hashlib.sha256(product_key.encode()).hexdigest()
                        # Extract domain from source URL
                        domain = urlparse(product.get('source_url', '')).netloc.replace('www.', '') if product.get('source_url') else ''
                        # Check if product already exists
                        cur.execute("SELECT id FROM products WHERE product_hash = %s", (product_hash,))
                        existing_product = cur.fetchone()
                        if existing_product:
                            product_id = existing_product['id']
                            stats["existing_products"] += 1
                            logger.debug(f"🔄 Product already exists: {product.get('productname', 'Unknown')}")
                        else:
                            # Insert new product
                            cur.execute("""
                                INSERT INTO products (
                                    product_name, description, current_price, original_price, rating, review, image_url, source_url, domain, product_hash
                                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                                RETURNING id
                            """, (
                                product.get('productname', ''),
                                product.get('description', ''),
                                product.get('current_price', ''),
                                product.get('original_price', ''),
                                product.get('rating', ''),
                                product.get('review', ''),
                                product.get('image_url', ''),
                                product.get('source_url', ''),
                                domain,
                                product_hash
                            ))
                            result = cur.fetchone()
                            product_id = result['id']
                            stats["new_products"] += 1
                            logger.debug(f"✅ Inserted new product: {product.get('productname', 'Unknown')}")
                        # Link product to job (handle duplicates gracefully)
                        cur.execute("""
                            INSERT INTO jobselectedproducts (job_id, product_id)
                            VALUES (%s, %s)
                            ON CONFLICT (job_id, product_id) DO NOTHING
                        """, (normalized_job_id, product_id))
                        if cur.rowcount > 0:
                            stats["jobselectedproducts_linked"] += 1
                    logger.info(f"✅ Product ingestion completed:")
                    logger.info(f" New products: {stats['new_products']}")
                    logger.info(f" Existing products: {stats['existing_products']}")
                    logger.info(f" Job-product links: {stats['jobselectedproducts_linked']}")
                    return stats
        except Exception as e:
            logger.error(f"❌ Failed to ingest products: {e}")
            raise

    def close(self):
        """Close all database connections"""
        if self.pool:
            logger.info("🔄 Closing database connection pool")
            self.pool.closeall()
            logger.info("✅ Database connection pool closed")

# ... (rest of your unchanged code continues here, unchanged)
# (No SQL referencing the job_id/id bug outside DatabaseManager - but if you use raw SQL elsewhere referencing scrapejobs.id, change to job_id!)
# 
# Also, be sure to fix all `logger.debug()` statements with `\${}` to just use `${}`, and fix f-string/dollar signs in all logs and doc/prompts as discussed above.

# ... 
# Add similar fixes wherever f-string logs or doc prompts use \$, make them ${...} and/or use raw triple-quote strings as needed.
# ... 

class AWSService:
    """AWS service handler for S3 and DynamoDB operations"""
    def __init__(self):
        logger.info("🔧 Initializing AWS Service")
        self.aws_region = os.getenv("AWS_REGION", "us-east-1")
        self.s3_bucket = os.getenv("S3_BUCKET_NAME")
        self.dynamodb_table = os.getenv("DYNAMODB_TABLE_NAME", "OrchestrationLogs")
        logger.info(f"📋 Environment variables:")
        logger.info(f" AWS_REGION: {self.aws_region}")
        logger.info(f" S3_BUCKET_NAME: {self.s3_bucket}")
        logger.info(f" DYNAMODB_TABLE_NAME: {self.dynamodb_table}")
        if not self.s3_bucket:
            logger.error("❌ S3_BUCKET_NAME environment variable is not set")
            raise EnvironmentError("S3_BUCKET_NAME must be set in environment variables")

        try:
            logger.info("🔑 Initializing AWS clients with default credential provider chain")
            try:
                sts_client = boto3.client('sts', region_name=self.aws_region)
                identity = sts_client.get_caller_identity()
                logger.info(f"✅ AWS Identity verified: Account={identity['Account']}, ARN={identity['Arn']}")
            except Exception as e:
                logger.error(f"❌ Failed to get caller identity: {e}")

            logger.info("📦 Initializing S3 client")
            self.s3_client = boto3.client('s3', region_name=self.aws_region)
            try:
                self.s3_client.head_bucket(Bucket=self.s3_bucket)
                logger.info(f"✅ Successfully verified S3 bucket access: {self.s3_bucket}")
            except Exception as e:
                logger.error(f"❌ Failed to access S3 bucket: {e}")

            logger.info("🗄️ Initializing DynamoDB client")
            self.dynamodb = boto3.resource('dynamodb', region_name=self.aws_region)
            self.table = self.dynamodb.Table(self.dynamodb_table)
            try:
                self.table.get_item(Key={'pk': 'test', 'sk': 'test'})
                logger.info(f"✅ Successfully verified DynamoDB table access: {self.dynamodb_table}")
            except Exception as e:
                logger.error(f"❌ Failed to access DynamoDB table: {e}")
            logger.info(f"✅ AWS services initialized successfully")
        except Exception as e:
            logger.error(f"❌ Failed to initialize AWS services: {e}")
            raise EnvironmentError(f"Failed to initialize AWS services: {e}")

    def upload_to_s3(self, file_path: str, s3_key: str, content_type: str = None) -> str:
        logger.info(f"📤 Uploading file to S3: {s3_key}")
        try:
            extra_args = {}
            if content_type:
                extra_args['ContentType'] = content_type
            self.s3_client.upload_file(file_path, self.s3_bucket, s3_key, ExtraArgs=extra_args)
            s3_url = f"https://{self.s3_bucket}.s3.{self.aws_region}.amazonaws.com/{s3_key}"
            logger.info(f"✅ Successfully uploaded to S3: {s3_key}")
            return s3_url
        except ClientError as e:
            logger.error(f"❌ Failed to upload {s3_key} to S3: {e}")
            return ""
        except Exception as e:
            logger.error(f"❌ Unexpected error uploading to S3: {e}")
            return ""

    def upload_string_to_s3(self, content: str, s3_key: str, content_type: str = "text/plain") -> str:
        logger.info(f"📤 Uploading string content to S3: {s3_key}")
        try:
            self.s3_client.put_object(Bucket=self.s3_bucket, Key=s3_key, Body=content.encode('utf-8'), ContentType=content_type)
            s3_url = f"https://{self.s3_bucket}.s3.{self.aws_region}.amazonaws.com/{s3_key}"
            logger.info(f"✅ Successfully uploaded string content to S3: {s3_key}")
            return s3_url
        except ClientError as e:
            logger.error(f"❌ Failed to upload string to S3: {e}")
            return ""

    def upload_json_to_s3(self, data: dict, s3_key: str) -> str:
        logger.info(f"📤 Uploading JSON data to S3: {s3_key}")
        try:
            json_content = json.dumps(data, indent=2, ensure_ascii=False)
            return self.upload_string_to_s3(json_content, s3_key, "application/json")
        except Exception as e:
            logger.error(f"❌ Failed to upload JSON to S3: {e}")
            return ""

class EnhancedWebCrawler:
    def __init__(self, model_name: str, output_dir: str, job_id: str = None):
        logger.info(f"🤖 Initializing EnhancedWebCrawler with model: {model_name}")
        self.model_name = model_name
        self.base_output_dir = Path(output_dir)
        self.job_id = job_id or f"crawl-{int(time.time())}"
        logger.info(f"📁 Base output directory: {self.base_output_dir}")
        logger.info(f"🆔 Job ID: {self.job_id}")

        logger.info("🔄 Setting up AWS services")
        self.aws_service = AWSService()
        
        logger.info("🗄️ Setting up database manager")
        self.db_manager = DatabaseManager()

        logger.info("💰 Configuring pricing tiers for token usage")
        self.pricing_tiers = {
            "standard": {
                "input": float(os.getenv("STANDARD_INPUT_PRICE", "0.075")),
                "output": float(os.getenv("STANDARD_OUTPUT_PRICE", "0.30")),
                "threshold": int(os.getenv("STANDARD_THRESHOLD", "128000"))
            },
            "large_context": {
                "input": float(os.getenv("LARGE_CONTEXT_INPUT_PRICE", "0.15")),
                "output": float(os.getenv("LARGE_CONTEXT_OUTPUT_PRICE", "0.60")),
                "threshold": float('inf')
            }
        }
        logger.info(f"📊 Pricing tiers configured: Standard threshold={self.pricing_tiers['standard']['threshold']} tokens")

        self.output_dir = None
        self.markdown_dir = None
        self.image_dir = None
        self.csv_dir = None
        self.json_dir = None
        self.s3_base_path = None

        logger.info("🔑 Retrieving Gemini API key")
        secret_name = os.getenv("GEMINI_SECRET_NAME", "Gemini-API-ChatGPT")
        secret_region = os.getenv("GEMINI_SECRET_REGION", "us-east-1")
        key = get_secret(secret_name, secret_region)
        if not key:
            logger.info("🔍 API key not found in Secrets Manager, checking environment variables")
            key = os.getenv("GEMINI_API_KEY")
            if key:
                logger.info("✅ Using Gemini API key from environment variable")
            else:
                logger.error("❌ GEMINI_API_KEY not found in Secrets Manager or environment variables")
                raise EnvironmentError("GEMINI_API_KEY not found in Secrets Manager or environment variables")
        else:
            logger.info("✅ Using Gemini API key from AWS Secrets Manager")

        logger.info("🔄 Configuring Gemini API")
        genai.configure(api_key=key)
        self.model = genai.GenerativeModel(
            self.model_name,
            generation_config=genai.types.GenerationConfig(
                temperature=float(os.getenv("MODEL_TEMPERATURE", "0.1")),
                max_output_tokens=int(os.getenv("MAX_OUTPUT_TOKENS", "8192")),
                response_mime_type="application/json",
            )
        )
        logger.info(f"✅ Gemini model {self.model_name} initialized successfully")
        self.all_products = []
        self.processed_urls = set()
        self.crawl_metrics = []
        self.discovered_urls_data = []
        self.s3_urls = {}
        self.total_token_usage = TokenUsage(
            input_tokens=0,
            output_tokens=0,
            total_cost=0.0,
            model_name=model_name,
            timestamp=datetime.now().isoformat(),
            pricing_tier="mixed"
        )
        logger.info("✅ EnhancedWebCrawler initialization complete")

    def setup_job_in_database(self, url: str, job_id: str = None) -> str:
        """Setup scrape job in database"""
        logger.info("📝 STEP 1: Setting up scrape job in database")
        
        # Use provided job_id or generate one
        if job_id:
            self.job_id = job_id
            logger.info(f"📋 Using job ID from Lambda: {job_id}")
        else:
            self.job_id = f"crawl-{int(time.time())}"
            logger.info(f"🆔 Generated new job ID: {self.job_id}")
        
        # Extract domain and brand
        domain = self.get_domain_name(url)
        brand = domain.split('.')[0].title()  # Simple brand extraction
        
        # Check if job already exists
        if self.db_manager.job_exists(self.job_id):
            logger.info(f"🔍 Job {self.job_id} already exists, updating...")
            normalized_job_id = self.db_manager.normalize_job_id(self.job_id)
        else:
            logger.info(f"📝 Creating new job with provided job_id: {self.job_id}")
            normalized_job_id = self.db_manager.create_scrape_job(self.job_id, url, brand)
        
        logger.info(f"✅ Job setup complete: {self.job_id} -> {normalized_job_id}")
        return normalized_job_id

    def update_job_status(self, status: str, error_message: str = None):
        """Update job status in database"""
        logger.info(f"🔄 STATUS UPDATE: {status}")
        try:
            self.db_manager.update_job_status(self.job_id, status, error_message)
            logger.info(f"✅ Updated job status to {status}: {self.job_id}")
        except Exception as e:
            logger.error(f"❌ Failed to update job status: {e}")

    def setup_domain_directories(self, domain: str):
        logger.info(f"📁 Setting up directory structure for domain: {domain}")
        self.output_dir = self.base_output_dir / domain
        self.markdown_dir = self.output_dir / "markdown"
        self.image_dir = self.output_dir / "images"
        self.csv_dir = self.output_dir / "csv"
        self.json_dir = self.output_dir / "json"
        for dir_path in [self.output_dir, self.markdown_dir, self.image_dir, self.csv_dir, self.json_dir]:
            dir_path.mkdir(parents=True, exist_ok=True)
            logger.info(f"📂 Created directory: {dir_path}")
        
        # Use job_id for S3 path instead of timestamp
        s3_prefix = os.getenv("S3_PREFIX", "crawl-data/raw-scrapes")
        normalized_job_id = self.db_manager.normalize_job_id(self.job_id)
        self.s3_base_path = f"{s3_prefix}/{normalized_job_id}"
        
        logger.info(f"✅ Created local directory structure at: {self.output_dir}")
        logger.info(f"☁️ S3 base path: {self.s3_base_path}")

    def calculate_pricing_tier_and_cost(self, input_tokens: int, output_tokens: int) -> Dict[str, Any]:
        total_tokens = input_tokens + output_tokens
        if total_tokens <= self.pricing_tiers["standard"]["threshold"]:
            tier = "standard"
        else:
            tier = "large_context"
        input_cost = (input_tokens / 1000000) * self.pricing_tiers[tier]["input"]
        output_cost = (output_tokens / 1000000) * self.pricing_tiers[tier]["output"]
        total_cost = input_cost + output_cost
        logger.debug(f"💰 Token usage: {input_tokens} input, {output_tokens} output, tier: {tier}, cost: ${total_cost:.4f}")
        return {
            "tier": tier,
            "input_cost": input_cost,
            "output_cost": output_cost,
            "total_cost": total_cost
        }

    def get_domain_name(self, url: str) -> str:
        parsed_url = urlparse(url)
        domain = parsed_url.netloc
        if domain.startswith('www.'):
            domain = domain[4:]
        if ':' in domain:
            domain = domain.split(':')[0]
        logger.debug(f"🔍 Extracted domain '{domain}' from URL: {url}")
        return domain

    def url_to_filename(self, url: str, domain: str) -> str:
        parsed = urlparse(url)
        path = parsed.path
        if path == '/':
            return 'index'
        path = path.strip('/')
        path = path.replace('/', '_')
        if parsed.query:
            query_hash = hashlib.md5(parsed.query.encode()).hexdigest()[:8]
            path = f"{path}_{query_hash}"
        if len(path) > 100:
            path = path[:90] + hashlib.md5(path.encode()).hexdigest()[:10]
        logger.debug(f"📄 Converted URL to filename: {path}")
        return path

    async def discover_all_urls(self, root_url: str) -> List[Dict[str, Any]]:
        logger.info(f"🔍 STARTING URL DISCOVERY: {root_url}")
        logger.info(f"📊 Domain extracted: {self.get_domain_name(root_url)}")
        domain = self.get_domain_name(root_url)
        discovered_urls = []
        try:
            max_urls = int(os.getenv("MAX_URLS", "100"))
            logger.info(f"🔧 Configuring URL seeder with max_urls={max_urls}")
            logger.info("🔄 Trying URL Seeding with sitemap...")
            async with AsyncUrlSeeder() as seeder:
                sitemap_config = SeedingConfig(
                    source="sitemap",
                    extract_head=True,
                    live_check=False,
                    max_urls=max_urls,
                    verbose=False,
                    force=True
                )
                try:
                    sitemap_urls = await seeder.urls(domain, sitemap_config)
                    if sitemap_urls:
                        logger.info(f"✅ Found {len(sitemap_urls)} URLs via sitemap")
                        for url_info in sitemap_urls:
                            discovered_urls.append({
                                'url': url_info['url'],
                                'status': url_info.get('status', 'unknown'),
                                'title': url_info.get('head_data', {}).get('title', ''),
                                'meta_description': url_info.get('head_data', {}).get('meta', {}).get('description', ''),
                                'source': 'sitemap'
                            })
                    else:
                        logger.warning("⚠️ No URLs found via sitemap")
                except Exception as e:
                    logger.warning(f"⚠️ URL seeding with sitemap failed: {e}")

            if not discovered_urls:
                logger.info("🔄 Trying Common Crawl as backup...")
                async with AsyncUrlSeeder() as seeder:
                    cc_config = SeedingConfig(
                        source="cc",
                        extract_head=False,
                        max_urls=max_urls,
                        verbose=False
                    )
                    try:
                        cc_urls = await seeder.urls(domain, cc_config)
                        if cc_urls:
                            logger.info(f"✅ Found {len(cc_urls)} URLs via Common Crawl")
                            for url_info in cc_urls:
                                discovered_urls.append({
                                    'url': url_info['url'],
                                    'status': url_info.get('status', 'unknown'),
                                    'title': '',
                                    'meta_description': '',
                                    'source': 'common_crawl'
                                })
                        else:
                            logger.warning("⚠️ No URLs found via Common Crawl")
                    except Exception as e:
                        logger.warning(f"⚠️ Common Crawl failed: {e}")

            if not discovered_urls:
                logger.info("🔄 Using manual URL discovery as fallback...")
                common_paths = [
                    "", "/collections/all", "/collections/skincare", "/collections/haircare", "/collections/bundles",
                    "/pages/about", "/pages/ingredients", "/blogs/news", "/search", "/products", "/shop", "/categories",
                    "/new-arrivals", "/sale"
                ]
                for path in common_paths:
                    full_url = urljoin(root_url, path)
                    discovered_urls.append({
                        'url': full_url,
                        'status': 'manual',
                        'title': '',
                        'meta_description': '',
                        'source': 'manual_discovery'
                    })
                logger.info(f"✅ Added {len(discovered_urls)} URLs via manual discovery")

            seen_urls = set()
            unique_urls = []
            base_domain = domain
            for url_info in discovered_urls:
                url = url_info['url']
                if not url.startswith('http'):
                    url = f"https://{url}"
                url_info['url'] = url
                try:
                    url_domain = urlparse(url).netloc.replace('www.', '')
                    if base_domain in url_domain and url not in seen_urls:
                        seen_urls.add(url)
                        unique_urls.append(url_info)
                except Exception:
                    continue
            self.discovered_urls_data = unique_urls
            logger.info(f"🎯 Discovered {len(unique_urls)} URLs from domain {domain}")
            return unique_urls
        except Exception as e:
            logger.error(f"❌ Error discovering URLs: {str(e)}")
            return []

    def display_url_tree(self, urls: List[Dict[str, Any]], domain: str):
        if not urls:
            logger.info("⚠️ No URLs to display")
            return
        logger.info(f"\n📋 URL Tree for {domain} ({len(urls)} URLs):")
        by_source = {}
        for url_info in urls:
            source = url_info.get('source', 'unknown')
            if source not in by_source:
                by_source[source] = []
            by_source[source].append(url_info)
        for source, source_urls in by_source.items():
            logger.info(f"\n📂 {source.upper().replace('_', ' ')} ({len(source_urls)} URLs)")
            for i, url_info in enumerate(source_urls[:10], 1):
                url = url_info['url']
                title = url_info.get('title', '').strip()
                status = url_info.get('status', 'unknown')
                status_icon = "✅" if status in ['valid', 200, 'manual'] else "❓"
                logger.info(f" {i:2d}. {status_icon} {url}")
                if title:
                    logger.info(f" 📝 Title: {title[:50]}...")
            if len(source_urls) > 10:
                logger.info(f" ... and {len(source_urls) - 10} more URLs")

    async def crawl_single_url(self, url: str, domain: str) -> Dict[str, Any]:
        logger.info(f"🕸️ Crawling URL: {url}")
        filename = self.url_to_filename(url, domain)
        crawl_start_time = datetime.now()
        try:
            page_timeout = int(os.getenv("PAGE_TIMEOUT", "60000"))
            logger.info(f"⏱️ Page timeout set to {page_timeout}ms ({page_timeout//1000} seconds)")
            
            # Enhanced crawler configuration for better reliability
            config = CrawlerRunConfig(
                cache_mode=CacheMode.BYPASS,
                screenshot=True,
                word_count_threshold=int(os.getenv("WORD_COUNT_THRESHOLD", "10")),
                only_text=False,
                process_iframes=True,
                wait_for_images=True,
                page_timeout=page_timeout,
                exclude_external_links=True,
                excluded_tags=['script', 'style', 'nav', 'footer', 'header'],
                remove_overlay_elements=True,
                # Additional browser args for better stability
                browser_type="chromium",
                headless=True,
                verbose=False
            )
            
            logger.info(f"🔄 Starting crawler for URL: {url}")
            
            # Use enhanced browser configuration
            browser_args = [
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-web-security",
                "--disable-features=VizDisplayCompositor",
                "--disable-extensions",
                "--disable-plugins",
                "--disable-images",  # Speed up loading
                "--disable-javascript",  # For basic content extraction
                "--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ]
            
            async with AsyncWebCrawler(
                verbose=False,
                browser_type="chromium",
                headless=True,
                browser_args=browser_args
            ) as crawler:
                logger.info(f"⏳ Crawler running on {url} (this may take a while)")
                result = await crawler.arun(url=url, config=config)
            
            crawl_end_time = datetime.now()
            crawl_duration = (crawl_end_time - crawl_start_time).total_seconds() * 1000

            if not result or not result.markdown:
                logger.warning(f"⚠️ No content returned from {url}")
                crawl_metric = CrawlMetrics(
                    url=url,
                    crawl_start_time=crawl_start_time.isoformat(),
                    crawl_end_time=crawl_end_time.isoformat(),
                    crawl_duration_ms=int(crawl_duration),
                    content_length=0,
                    success=False,
                    error_message="No content returned"
                )
                self.crawl_metrics.append(crawl_metric)
                return {'url': url, 'filename': filename, 'success': False, 'content_length': 0}

            # Save and log content
            markdown_content = f"# Content from {url}\n\n**URL:** [{url}]({url})\n\n---\n\n{result.markdown}"
            markdown_file = self.markdown_dir / f"{filename}.md"
            with open(markdown_file, 'w', encoding='utf-8') as f:
                f.write(markdown_content)
            logger.info(f"✅ Saved markdown to {markdown_file}")
            logger.info(f"🔎 Markdown _snippet_ for {url}: {markdown_content[:200].replace(chr(10),' ')} ...")

            s3_markdown_key = f"{self.s3_base_path}/markdown/{filename}.md"
            s3_markdown_url = self.aws_service.upload_string_to_s3(
                markdown_content, s3_markdown_key, "text/markdown"
            )
            screenshot_file = ""
            s3_screenshot_url = ""
            if result.screenshot:
                try:
                    logger.info(f"📸 Processing screenshot for {url}")
                    screenshot_file = self.image_dir / f"{filename}.png"
                    with open(screenshot_file, "wb") as f:
                        f.write(b64decode(result.screenshot))
                    logger.info(f"✅ Saved screenshot to {screenshot_file}")
                    s3_screenshot_key = f"{self.s3_base_path}/images/{filename}.png"
                    s3_screenshot_url = self.aws_service.upload_to_s3(
                        str(screenshot_file), s3_screenshot_key, "image/png"
                    )
                    logger.info(f"✅ Screenshot saved locally and to S3")
                except Exception as e:
                    logger.warning(f"⚠️ Failed to save screenshot: {e}")
            else:
                logger.info("ℹ️ No screenshot available for this URL")
            logger.info(f"✅ Saved: {filename}.md ({len(result.markdown)} chars)")

            crawl_metric = CrawlMetrics(
                url=url,
                crawl_start_time=crawl_start_time.isoformat(),
                crawl_end_time=crawl_end_time.isoformat(),
                crawl_duration_ms=int(crawl_duration),
                content_length=len(result.markdown),
                success=True
            )
            self.crawl_metrics.append(crawl_metric)
            return {
                'url': url,
                'filename': filename,
                'success': True,
                'content_length': len(result.markdown),
                'markdown_file': str(markdown_file),
                'screenshot_file': str(screenshot_file) if screenshot_file else "",
                's3_markdown_url': s3_markdown_url,
                's3_screenshot_url': s3_screenshot_url,
                'markdown_content': result.markdown,
                'crawl_duration_ms': int(crawl_duration)
            }
        except Exception as e:
            crawl_end_time = datetime.now()
            crawl_duration = (crawl_end_time - crawl_start_time).total_seconds() * 1000
            logger.error(f"❌ Failed to crawl {url}: {str(e)}")
            crawl_metric = CrawlMetrics(
                url=url,
                crawl_start_time=crawl_start_time.isoformat(),
                crawl_end_time=crawl_end_time.isoformat(),
                crawl_duration_ms=int(crawl_duration),
                content_length=0,
                success=False,
                error_message=str(e)
            )
            self.crawl_metrics.append(crawl_metric)
            return {'url': url, 'filename': filename, 'success': False, 'error': str(e)}

    async def extract_products_from_content(self, content: str, url: str) -> List[Dict[str, Any]]:
        logger.info(f"🔍 Preparing for extraction on {url}, content length: {len(content) if content else 0}")

        if not content or not content.strip():
            logger.info(f"⚠️ Skipping Gemini extraction for {url}: BLANK content.")
            return []

        logger.info(f"🔹 Markdown sample for {url}: {content[:300].replace(chr(10),' ')} ...")

        try:
            prompt = f"""Extract ALL products found in this e-commerce page content.
- Products may be in <li>, <div>, <section>, or any type of card, block, tile, grid, or repeated element.
- Include products that appear inside elements with class names like 'product', 'item', 'card', 'listing', or ANY repetitive structure typical in shops (even without a class).
- Do NOT restrict to <li> tags only—support <div> and other containers.

For every product, return ONLY a JSON array, with each object having:
{{
  "productname": "Name (no HTML)",
  "description": "Short description/benefits",
  "current_price": "Current price (with currency symbol, e.g. \$12, ₹250)",
  "original_price": "Original price or same as current if no discount",
  "rating": "Rating or N/A",
  "review": "Review count/text or N/A",
  "image_url": "Main product image URL or N/A",
  "source_url": "{url}"
}}

If no products found, return `[]`. Begin response with `[` and end with `]` JSON only.

CONTENT:
{content[:50000]}
"""
            response = await self.model.generate_content_async(prompt)
            logger.info(f"✅ Received response from Gemini API")

            # --- TOKEN USAGE EXTRACTION: FIX THIS BLOCK ---
            input_tokens = output_tokens = 0
            try:
                # Most recent google-generativeai SDK:
                # usage_metadata: prompt_token_count and candidates_token_count
                if hasattr(response, "usage_metadata"):
                    input_tokens = getattr(response.usage_metadata, "prompt_token_count", 0)
                    output_tokens = getattr(response.usage_metadata, "candidates_token_count", 0)
                # Some SDKs: candidates[0].usage
                elif hasattr(response, "candidates") and hasattr(response.candidates[0], "usage"):
                    usage = response.candidates[0].usage
                    input_tokens = getattr(usage, "input_tokens", 0)
                    output_tokens = getattr(usage, "output_tokens", 0)
            except Exception as e:
                logger.warning(f"⚠️ Could not extract token usage for {url}: {str(e)}")

            logger.info(f"💰 RAW token usage: input_tokens={input_tokens}, output_tokens={output_tokens}")

            pricing_info = self.calculate_pricing_tier_and_cost(input_tokens, output_tokens)
            token_usage = TokenUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_cost=pricing_info["total_cost"],
                model_name=self.model_name,
                timestamp=datetime.now().isoformat(),
                pricing_tier=pricing_info["tier"]
            )
            if self.crawl_metrics:
                self.crawl_metrics[-1].token_usage = token_usage
            self.total_token_usage.input_tokens += input_tokens
            self.total_token_usage.output_tokens += output_tokens
            self.total_token_usage.total_cost += pricing_info["total_cost"]

            response_text = response.text.strip()
            logger.info(f"🔎 Gemini raw text (truncated): {response_text[:200]} ...")
            products = []
            try:
                if not response_text.startswith('['):
                    json_match = re.search(r'```.*```', response_text, re.DOTALL)
                    if json_match:
                        response_text = json_match.group(0)
                    else:
                        json_objs = re.findall(r'\{[^}]+\}', response_text, re.DOTALL)
                        if json_objs:
                            response_text = '[' + ','.join(json_objs) + ']'
                products = json.loads(response_text)
                if not isinstance(products, list):
                    products = []
            except Exception as e:
                logger.warning(f"⚠️ Failed to parse Gemini response as JSON (error: {e}). First 200 chars: {response_text[:200]}")
            logger.info(f"✅ Extracted {len(products)} products from {url}")
            logger.info(f"💰 Token usage: {input_tokens} input, {output_tokens} output, ${pricing_info['total_cost']:.4f}")
            return products

        except Exception as e:
            logger.error(f"❌ Gemini extraction failed for {url}: {str(e)}")
            return []

    def deduplicate_products(self, products: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not products:
            logger.info("ℹ️ No products to deduplicate")
            return []
        logger.info(f"🔄 Deduplicating {len(products)} products...")
        unique_products = []
        seen_keys = set()
        for product in products:
            name = product.get('productname', '').strip().lower()
            price = product.get('current_price', '').strip()
            if not name or name == "n/a":
                continue
            # Normalize price for dedup, but you could do smart comparison if needed
            dedup_key = f"{name}|{price}"
            if dedup_key not in seen_keys:
                seen_keys.add(dedup_key)
                unique_products.append(product)
        logger.info(f"✅ Found {len(unique_products)} unique products after deduplication")
        logger.info(f"🗑️ Removed {len(products) - len(unique_products)} duplicate products")
        return unique_products

    def save_products_json(self, products: List[Dict[str, Any]]):
        """Save products as JSON to local and S3"""
        logger.info(f"📊 Preparing comprehensive JSON output: products.json")
        
        # Prepare comprehensive output
        output_data = {
            "metadata": {
                "job_id": self.job_id,
                "timestamp": datetime.now().isoformat(),
                "total_products": len(products),
                "model_name": self.model_name,
                "s3_base_path": self.s3_base_path
            },
            "products": products,
            "token_usage": {
                "total_input_tokens": self.total_token_usage.input_tokens,
                "total_output_tokens": self.total_token_usage.output_tokens,
                "total_cost_usd": round(self.total_token_usage.total_cost, 4),
                "model_name": self.model_name
            }
        }
        
        try:
            # Save locally
            json_file = self.output_dir / "products.json"
            logger.info(f"💾 Saving JSON output locally to: {json_file}")
            with open(json_file, 'w', encoding='utf-8') as f:
                json.dump(output_data, f, indent=2, ensure_ascii=False)
            logger.info(f"✅ Saved JSON locally")
            
            # Upload to S3
            s3_key = f"{self.s3_base_path}/products.json"
            s3_url = self.aws_service.upload_json_to_s3(output_data, s3_key)
            if s3_url:
                self.s3_urls['products_json'] = s3_url
                logger.info(f"✅ Uploaded JSON to S3: {s3_url}")
            
            logger.info(f"✅ Saved comprehensive JSON output locally and to S3")
            
        except Exception as e:
            logger.error(f"❌ Error saving JSON output: {e}")

    def log_orchestration_event(self, event_name: str, details: Dict[str, Any] = None) -> bool:
        """Log orchestration events to DynamoDB"""
        logger.info(f"📝 Logging orchestration event: {event_name}")
        try:
            # Use the job_id for consistency
            job_id = self.job_id
            
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
            
            # Use the same DynamoDB table as AWS service
            logger.info(f"🔄 Writing event to DynamoDB table: {self.aws_service.dynamodb_table}")
            response = self.aws_service.table.put_item(Item=log_data)
            logger.info(f"✅ Logged orchestration event: {event_name} for job {job_id}")
            return True
            
        except Exception as e:
            logger.error(f"❌ Failed to log orchestration event: {e}")
            return False

    async def run(self, root_url: str, job_id: str = None) -> Dict[str, Any]:
        """Main execution pipeline with RDS integration"""
        logger.info(f"🚀 STARTING CRAWL PIPELINE FOR: {root_url}")
        overall_start = time.time()
        
        try:
            # Step 1: Setup job in database
            normalized_job_id = self.setup_job_in_database(root_url, job_id)
            
            # Update job status to running
            self.update_job_status("JOB_RUNNING")
            
            # Setup domain directories
            domain = self.get_domain_name(root_url)
            self.setup_domain_directories(domain)
            
            logger.info(f"📋 Crawl configuration:")
            logger.info(f"Original Job ID: {job_id}")
            logger.info(f"Normalized Job ID: {normalized_job_id}")
            logger.info(f"Root URL: {root_url}")
            logger.info(f"Domain: {domain}")
            logger.info(f"Local output directory: {self.output_dir}")
            logger.info(f"S3 base path: {self.s3_base_path}")
            logger.info(f"DynamoDB table: {self.aws_service.dynamodb_table}")
            logger.info(f"Model: {self.model_name}")
            
            # Log job creation event
            logger.info(f"📝 Logging job creation event to DynamoDB")
            self.log_orchestration_event("JobCreated", {
                "root_url": root_url,
                "domain": domain,
                "model_name": self.model_name,
                "s3_base_path": self.s3_base_path
            })
            
            # Step 2: Discover URLs
            logger.info(f"🔍 STEP 2: Discovering URLs from {root_url}")
            discovered_urls = await self.discover_all_urls(root_url)
            
            if not discovered_urls:
                error_msg = "No URLs could be discovered"
                logger.error(f"❌ {error_msg}")
                self.update_job_status("JOB_FAILED", error_msg)
                return {
                    "root_url": root_url,
                    "domain": domain,
                    "error": error_msg,
                    "status": "failed",
                    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
                }
            
            # Step 3: Display URL tree
            logger.info(f"📊 STEP 3: Displaying URL tree")
            self.display_url_tree(discovered_urls, domain)
            
            # Step 4: Crawl URLs
            logger.info(f"\n🕸️ STEP 4: Starting to crawl {len(discovered_urls)} URLs...")
            
            crawl_results = []
            for i, url_info in enumerate(discovered_urls, 1):
                url = url_info['url']
                logger.info(f"\n[{i}/{len(discovered_urls)}] 🔄 Processing: {url}")
                
                result = await self.crawl_single_url(url, domain)
                crawl_results.append(result)
                
                # Extract products if crawl was successful
                if result.get('success') and result.get('markdown_content'):
                    logger.info(f"🔍 Extracting products from {url}")
                    products = await self.extract_products_from_content(
                        result['markdown_content'], url
                    )
                    self.all_products.extend(products)
                    logger.info(f"✅ Added {len(products)} products from {url}")
                else:
                    logger.info(f"⚠️ Skipping product extraction for {url} (crawl failed or no content)")
                
                logger.info(f"⏱️ Waiting 1 second before next URL...")
                await asyncio.sleep(1)  # Be respectful to the server
            
            # Step 5: Process products
            logger.info(f"\n🔄 STEP 5: Processing {len(self.all_products)} total products...")
            unique_products = self.deduplicate_products(self.all_products)
            
            # Step 6: Save products JSON
            logger.info(f"\n💾 STEP 6: Saving products.json to local and S3")
            self.save_products_json(unique_products)
            
            # Step 7: Ingest products into database
            logger.info(f"\n🗄️ STEP 7: Ingesting products into database")
            db_stats = self.db_manager.ingest_products(self.job_id, unique_products)
            logger.info(f"✅ Database ingestion completed: {db_stats}")
            
            # Update job status to success
            self.update_job_status("JOB_SUCCESS")
            
            # Prepare final result
            total_time = round(time.time() - overall_start, 3)
            successful_crawls = sum(1 for r in crawl_results if r.get('success'))
            
            result = {
                "root_url": root_url,
                "domain": domain,
                "job_id": self.job_id,
                "normalized_job_id": normalized_job_id,
                "discovered_urls": len(discovered_urls),
                "successful_crawls": successful_crawls,
                "failed_crawls": len(discovered_urls) - successful_crawls,
                "total_products_found": len(self.all_products),
                "unique_products": len(unique_products),
                "processing_time_seconds": total_time,
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                "status": "success",
                "s3_base_path": self.s3_base_path,
                "s3_urls": self.s3_urls,
                "local_output_directory": str(self.output_dir),
                "database_stats": db_stats,
                "token_usage": {
                    "total_input_tokens": self.total_token_usage.input_tokens,
                    "total_output_tokens": self.total_token_usage.output_tokens,
                    "total_cost_usd": round(self.total_token_usage.total_cost, 4),
                    "model_name": self.model_name,
                    "average_cost_per_url": round(self.total_token_usage.total_cost / max(successful_crawls, 1), 4)
                }
            }
            
            # Step 8: Log to DynamoDB
            logger.info(f"\n📝 STEP 8: Logging session to DynamoDB")
            self.log_orchestration_event("ScrapingCompleted", {
                "root_url": root_url,
                "domain": domain,
                "discovered_urls": len(discovered_urls),
                "successful_crawls": successful_crawls,
                "failed_crawls": len(discovered_urls) - successful_crawls,
                "total_products_found": len(self.all_products),
                "unique_products": len(unique_products),
                "processing_time_seconds": total_time,
                "token_cost_usd": round(self.total_token_usage.total_cost, 4),
                "s3_base_path": self.s3_base_path,
                "database_stats": db_stats
            })
            
            # Log scraping completed event
            logger.info(f"📝 Logging scraping completed event")
            self.log_orchestration_event("ScrapingCompleted", result)
            
            # Final summary
            logger.info(f"\n✅ CRAWLING COMPLETE!")
            logger.info(f"📊 SUMMARY:")
            logger.info(f"Original Job ID: {job_id}")
            logger.info(f"Normalized Job ID: {normalized_job_id}")
            logger.info(f"URLs discovered: {len(discovered_urls)}")
            logger.info(f"Successfully crawled: {successful_crawls}")
            logger.info(f"Failed crawls: {len(discovered_urls) - successful_crawls}")
            logger.info(f"Total products found: {len(self.all_products)}")
            logger.info(f"Unique products: {len(unique_products)}")
            logger.info(f"Database ingestion: {db_stats}")
            logger.info(f"Total time: {total_time}s")
            logger.info(f"Total token cost: ${self.total_token_usage.total_cost:.4f}")
            logger.info(f"Local backup: {self.output_dir}")
            logger.info(f"S3 location: {self.s3_base_path}")
            logger.info(f"DynamoDB logged: Yes")
            
            return result
            
        except Exception as e:
            logger.error(f"❌ Pipeline failed: {str(e)}")
            
            # Update job status to failed
            self.update_job_status("JOB_FAILED", str(e))
            
            error_result = {
                "root_url": root_url,
                "domain": self.get_domain_name(root_url),
                "job_id": self.job_id,
                "error": str(e),
                "status": "failed",
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
            }
            
            # Log scraping failed event
            logger.error(f"📝 Logging scraping failed event")
            self.log_orchestration_event("ScrapingFailed", {
                "root_url": root_url,
                "error": str(e),
                "processing_time_seconds": round(time.time() - overall_start, 3)
            })
            
            return error_result
        
        finally:
            # Clean up database connections
            logger.info("🔄 Cleaning up database connections")
            self.db_manager.close()

async def main():
    """Main function for batch processing"""
    logger.info("🚀 Starting batch processing")
    parser = argparse.ArgumentParser(description='E-commerce website crawler with RDS integration')
    parser.add_argument('--url', type=str, help='URL to crawl')
    parser.add_argument('--job-id', type=str, help='Job ID for tracking')
    parser.add_argument('--output-dir', type=str, default=os.getenv('OUTPUT_DIR', '/tmp/crawl_output'), 
                        help='Output directory for local files')
    
    # Check if we're being invoked by Lambda (API Gateway)
    event_data = {}
    if len(sys.argv) > 1 and sys.argv[1].startswith('{'):
        try:
            # Try to parse the first argument as JSON (Lambda event)
            event_data = json.loads(sys.argv[1])
            logger.info(f"📋 Detected event data: {event_data}")
        except json.JSONDecodeError:
            # Not JSON, proceed with normal argument parsing
            logger.info("📋 No JSON event data detected, using command line arguments")
            pass
    
    # Parse command line arguments if not invoked with event data
    if not event_data:
        # Check if we have any meaningful arguments (not just script name)
        # Filter out common script execution patterns
        meaningful_args = [arg for arg in sys.argv[1:] if not arg.startswith('python') and arg != 'app.py']
        logger.info(f"📋 Command line arguments: {sys.argv}")
        logger.info(f"📋 Meaningful arguments: {meaningful_args}")
        
        if meaningful_args:
            try:
                args = parser.parse_args(meaningful_args)
                url = args.url
                job_id = args.job_id
                output_dir = args.output_dir
                logger.info(f"📋 Parsed arguments - URL: {url}, Job ID: {job_id}, Output dir: {output_dir}")
            except SystemExit:
                # If argument parsing fails, use default values
                logger.warning("⚠️ Argument parsing failed, using default values")
                url = None
                job_id = None
                output_dir = os.getenv('OUTPUT_DIR', '/tmp/crawl_output')
        else:
            # No meaningful arguments provided, use default values
            logger.info("📋 No meaningful arguments provided, using default values")
            url = None
            job_id = None
            output_dir = os.getenv('OUTPUT_DIR', '/tmp/crawl_output')
    else:
        # Extract parameters from event data
        # Check various locations where the URL might be
        url = None
        job_id = None
        logger.info("🔍 Extracting parameters from event data")
        
        # Check in body (POST request)
        if 'body' in event_data:
            try:
                body = event_data['body']
                if isinstance(body, str):
                    body = json.loads(body)
                url = body.get('url')
                job_id = body.get('job_id')
                if url:
                    logger.info(f"✅ Found URL in request body: {url}")
                if job_id:
                    logger.info(f"✅ Found Job ID in request body: {job_id}")
            except (json.JSONDecodeError, AttributeError):
                logger.warning("⚠️ Failed to parse body as JSON")
                pass
        
        # Check in queryStringParameters (GET request)
        if not url and 'queryStringParameters' in event_data and event_data['queryStringParameters']:
            url = event_data['queryStringParameters'].get('url')
            job_id = event_data['queryStringParameters'].get('job_id')
            if url:
                logger.info(f"✅ Found URL in query parameters: {url}")
            if job_id:
                logger.info(f"✅ Found Job ID in query parameters: {job_id}")
        
        # Check directly in the event
        if not url:
            url = event_data.get('url')
            if url:
                logger.info(f"✅ Found URL directly in event: {url}")
        
        if not job_id:
            job_id = event_data.get('job_id') or os.getenv("AWS_BATCH_JOB_ID")
            if job_id:
                logger.info(f"✅ Found Job ID: {job_id}")
        
        # Get output directory from event or use default
        output_dir = event_data.get('output_dir', os.getenv('OUTPUT_DIR', '/tmp/crawl_output'))
        logger.info(f"📁 Output directory: {output_dir}")
    
    # Generate job_id if not provided
    if not job_id:
        job_id = f"lambda-{int(time.time())}"
        logger.info(f"🆔 Generated job ID: {job_id}")
    
    # Debug logging
    logger.info(f"📋 Final parameters - URL: {url}, Job ID: {job_id}, Output dir: {output_dir}")
    
    if not url:
        logger.error("❌ No URL provided. Exiting.")
        sys.exit(1)
    
    if not url.startswith("http"):
        logger.info(f"🔄 Adding https:// prefix to URL: {url}")
        url = "https://" + url
        
    try:
        logger.info(f"🚀 Starting crawl for: {url}")
        logger.info(f"🤖 Using model: gemini-1.5-flash")
        logger.info(f"📁 Output directory: {output_dir}")
        logger.info(f"🆔 Job ID: {job_id}")
        
        # Initialize crawler with RDS integration
        logger.info(f"🔄 Initializing EnhancedWebCrawler with RDS integration")
        crawler = EnhancedWebCrawler(
            model_name='gemini-1.5-flash',
            output_dir=output_dir,
            job_id=job_id
        )
        
        # Run the full pipeline
        logger.info(f"🚀 Running crawler pipeline")
        result = await crawler.run(url, job_id)
        
        # Output final status for batch job
        if result["status"] == "success":
            logger.info(f"✅ JOB SUCCESSFUL: Processed {result['discovered_urls']} URLs")
            logger.info(f"📊 Results: {result['successful_crawls']} successful crawls, {result['total_products_found']} products found")
            logger.info(f"🗄️ Database: {result['database_stats']}")
            logger.info(f"💰 Token cost: ${result['token_usage']['total_cost_usd']}")
            logger.info(f"☁️ S3 path: {result['s3_base_path']}")
            
            # If invoked with event data, format response
            if event_data:
                logger.info(f"🔄 Formatting API response")
                api_response = {
                    "statusCode": 200,
                    "headers": {
                        "Content-Type": "application/json"
                    },
                    "body": json.dumps({
                        "status": "success",
                        "message": f"Successfully processed {result['discovered_urls']} URLs",
                        "data": {
                            "job_id": result["job_id"],
                            "normalized_job_id": result["normalized_job_id"],
                            "domain": result["domain"],
                            "discovered_urls": result["discovered_urls"],
                            "successful_crawls": result["successful_crawls"],
                            "total_products": result["total_products_found"],
                            "unique_products": result["unique_products"],
                            "processing_time": result["processing_time_seconds"],
                            "s3_base_path": result["s3_base_path"],
                            "s3_urls": result["s3_urls"],
                            "database_stats": result["database_stats"]
                        }
                    })
                }
                print(json.dumps(api_response))
            
            logger.info("👋 Exiting with success status")
            sys.exit(0)
        else:
            error_msg = result.get('error', 'Unknown error')
            logger.error(f"❌ JOB FAILED: {error_msg}")
            
            # If invoked with event data, format error response
            if event_data:
                logger.info(f"🔄 Formatting API error response")
                api_response = {
                    "statusCode": 500,
                    "headers": {
                        "Content-Type": "application/json"
                    },
                    "body": json.dumps({
                        "status": "error",
                        "message": f"Crawl failed: {error_msg}",
                        "job_id": result.get("job_id")
                    })
                }
                print(json.dumps(api_response))
            
            logger.info("👋 Exiting with error status")
            sys.exit(1)
            
    except Exception as e:
        error_msg = str(e)
        logger.error(f"❌ Job failed with error: {error_msg}")
        
        # If invoked with event data, format error response
        if event_data:
            logger.info(f"🔄 Formatting API error response")
            api_response = {
                "statusCode": 500,
                "headers": {
                    "Content-Type": "application/json"
                },
                "body": json.dumps({
                    "status": "error",
                    "message": f"Exception occurred: {error_msg}",
                    "job_id": job_id
                })
            }
            print(json.dumps(api_response))
        
        logger.info("👋 Exiting with error status")
        sys.exit(1)

if __name__ == "__main__":
    logger.info("🏁 Application entry point")
    asyncio.run(main())