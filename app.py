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
    def __init__(self, model_name: str, output_dir: str):
        logger.info(f"🤖 Initializing EnhancedWebCrawler with model: {model_name}")
        self.model_name = model_name
        self.base_output_dir = Path(output_dir)
        logger.info(f"📁 Base output directory: {self.base_output_dir}")

        logger.info("🔄 Setting up AWS services")
        self.aws_service = AWSService()

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
        timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
        s3_prefix = os.getenv("S3_PREFIX", "crawl-data")
        self.s3_base_path = f"{s3_prefix}/{domain}/{timestamp}"
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
        logger.debug(f"💰 Token usage: {input_tokens} input, {output_tokens} output, tier: {tier}, cost: \\${total_cost:.4f}")
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

    def save_url_tree(self, domain: str, root_url: str):
        logger.info(f"📝 Saving URL tree for domain: {domain}")
        tree_file = self.output_dir / "tree.md"
        try:
            tree_content = self._generate_tree_content(domain, root_url)
            with open(tree_file, 'w', encoding='utf-8') as f:
                f.write(tree_content)
            logger.info(f"✅ Saved URL tree locally to: {tree_file}")
            s3_key = f"{self.s3_base_path}/tree.md"
            s3_url = self.aws_service.upload_string_to_s3(tree_content, s3_key, "text/markdown")
            if s3_url:
                self.s3_urls['tree_md'] = s3_url
            logger.info(f"✅ URL tree saved successfully")
        except Exception as e:
            logger.error(f"❌ Error saving URL tree: {e}")

    def _generate_tree_content(self, domain: str, root_url: str) -> str:
        logger.info(f"📄 Generating tree content for {domain}")
        content = [
            f"# URL Tree for {domain}\n",
            f"**Root URL:** [{root_url}]({root_url})\n",
            f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n",
            f"**Total URLs:** {len(self.discovered_urls_data)}\n\n",
            "---\n\n"
        ]
        if not self.discovered_urls_data:
            content.append("No URLs discovered\n")
            return ''.join(content)
        by_source = {}
        for url_info in self.discovered_urls_data:
            source = url_info.get('source', 'unknown')
            if source not in by_source:
                by_source[source] = []
            by_source[source].append(url_info)
        for source, source_urls in by_source.items():
            content.append(f"## {source.upper().replace('_', ' ')} ({len(source_urls)} URLs)\n\n")
            for i, url_info in enumerate(source_urls, 1):
                url = url_info['url']
                title = url_info.get('title', '').strip()
                meta_desc = url_info.get('meta_description', '').strip()
                status = url_info.get('status', 'unknown')
                status_icon = "✅" if status in ['valid', 200, 'manual'] else "❓"
                content.extend([
                    f"### {i:2d}. {status_icon} [{url}]({url})\n\n",
                    f"**Title:** {title}\n\n" if title else "",
                    f"**Description:** {meta_desc}\n\n" if meta_desc else "",
                    f"**Status:** {status}\n",
                    f"**Source:** {source}\n\n",
                    "---\n\n"
                ])
        content.extend([
            "## Summary\n\n",
            f"- **Total URLs discovered:** {len(self.discovered_urls_data)}\n"
        ])
        for source, source_urls in by_source.items():
            content.append(f"- **{source.replace('_', ' ').title()}:** {len(source_urls)} URLs\n")
        content.append(f"\n**Discovery completed:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        logger.info(f"✅ Tree content generated with {len(self.discovered_urls_data)} URLs")
        return ''.join(content)

    async def crawl_single_url(self, url: str, domain: str) -> Dict[str, Any]:
        logger.info(f"🕸️ Crawling URL: {url}")
        filename = self.url_to_filename(url, domain)
        crawl_start_time = datetime.now()
        try:
            page_timeout = int(os.getenv("PAGE_TIMEOUT", "60000"))
            logger.info(f"⏱️ Page timeout set to {page_timeout}ms (60 seconds)")
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
                remove_overlay_elements=True
            )
            logger.info(f"🔄 Starting crawler for URL: {url}")
            async with AsyncWebCrawler(verbose=False) as crawler:
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
  "current_price": "Current price (with currency symbol, e.g. $12, ₹250)",
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
                    json_match = re.search(r'\[.*\]', response_text, re.DOTALL)
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

    def save_products_csv(self, products: List[Dict[str, Any]], domain: str, filename_suffix: str = ""):
        csv_filename = f"{domain}{filename_suffix}.csv"
        csv_file = self.csv_dir / csv_filename
        logger.info(f"📊 Saving {len(products)} products to CSV: {csv_filename}")
        try:
            csv_content = io.StringIO()
            if products:
                fieldnames = list(products[0].keys())
                writer = csv.DictWriter(csv_content, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(products)
            else:
                fieldnames = list(ProductInfo.model_fields.keys())
                writer = csv.DictWriter(csv_content, fieldnames=fieldnames)
                writer.writeheader()
            csv_string = csv_content.getvalue()
            with open(csv_file, 'w', encoding='utf-8') as f:
                f.write(csv_string)
            logger.info(f"✅ Saved CSV locally to: {csv_file}")
            s3_key = f"{self.s3_base_path}/csv/{csv_filename}"
            s3_url = self.aws_service.upload_string_to_s3(csv_string, s3_key, "text/csv")
            if s3_url:
                self.s3_urls[f'csv_{filename_suffix}'] = s3_url
            logger.info(f"✅ Saved {len(products)} products to CSV locally and S3")
        except Exception as e:
            logger.error(f"❌ Error saving CSV: {e}")

    def save_json_output(self, domain: str, crawl_results: List[Dict[str, Any]], all_products: List[Dict[str, Any]], unique_products: List[Dict[str, Any]]):
        json_filename = f"{domain}_crawl_results.json"
        json_file = self.json_dir / json_filename
        logger.info(f"📊 Preparing comprehensive JSON output: {json_filename}")

        standard_cost = sum(
            metric.token_usage.total_cost for metric in self.crawl_metrics
            if metric.token_usage and metric.token_usage.pricing_tier == "standard"
        )
        large_context_cost = sum(
            metric.token_usage.total_cost for metric in self.crawl_metrics
            if metric.token_usage and metric.token_usage.pricing_tier == "large_context"
        )
        output_data = {
            "metadata": {
                "domain": domain,
                "timestamp": datetime.now().isoformat(),
                "model_name": self.model_name,
                "s3_base_path": self.s3_base_path,
                "s3_bucket": self.aws_service.s3_bucket,
                "total_urls_processed": len(self.crawl_metrics),
                "successful_crawls": sum(1 for m in self.crawl_metrics if m.success),
                "failed_crawls": sum(1 for m in self.crawl_metrics if not m.success),
                "total_products_found": len(all_products),
                "unique_products_after_deduplication": len(unique_products)
            },
            "s3_urls": self.s3_urls,
            "discovered_urls": self.discovered_urls_data,
            "token_usage": {
                "total_input_tokens": self.total_token_usage.input_tokens,
                "total_output_tokens": self.total_token_usage.output_tokens,
                "total_cost_usd": round(self.total_token_usage.total_cost, 4),
                "model_name": self.model_name,
                "pricing_breakdown": {
                    "standard_tier_cost": round(standard_cost, 4),
                    "large_context_tier_cost": round(large_context_cost, 4),
                    "standard_threshold": "≤128k tokens",
                    "large_context_threshold": ">128k tokens"
                },
                "pricing_tiers": self.pricing_tiers
            },
            "crawl_metrics": [
                {
                    "url": metric.url,
                    "crawl_start_time": metric.crawl_start_time,
                    "crawl_end_time": metric.crawl_end_time,
                    "crawl_duration_ms": metric.crawl_duration_ms,
                    "content_length": metric.content_length,
                    "success": metric.success,
                    "error_message": metric.error_message,
                    "token_usage": {
                        "input_tokens": metric.token_usage.input_tokens if metric.token_usage else 0,
                        "output_tokens": metric.token_usage.output_tokens if metric.token_usage else 0,
                        "cost_usd": round(metric.token_usage.total_cost, 4) if metric.token_usage else 0.0,
                        "pricing_tier": metric.token_usage.pricing_tier if metric.token_usage else "none"
                    } if metric.token_usage else None
                }
                for metric in self.crawl_metrics
            ],
            "products": {
                "all_products": all_products,
                "unique_products": unique_products,
                "deduplication_stats": {
                    "original_count": len(all_products),
                    "unique_count": len(unique_products),
                    "duplicates_removed": len(all_products) - len(unique_products)
                }
            },
            "crawl_results": crawl_results
        }
        try:
            logger.info(f"💾 Saving JSON output locally to: {json_file}")
            with open(json_file, 'w', encoding='utf-8') as f:
                json.dump(output_data, f, indent=2, ensure_ascii=False)
            logger.info(f"✅ Saved JSON locally")
            s3_key = f"{self.s3_base_path}/json/{json_filename}"
            s3_url = self.aws_service.upload_json_to_s3(output_data, s3_key)
            if s3_url:
                self.s3_urls['json_results'] = s3_url
            logger.info(f"✅ Uploaded JSON to S3: {s3_url}")
            logger.info(f"✅ Saved comprehensive JSON output locally and to S3")
        except Exception as e:
            logger.error(f"❌ Error saving JSON output: {e}")


    def log_orchestration_event(self, event_name: str, details: Dict[str, Any] = None) -> bool:
        """Log orchestration events to DynamoDB"""
        logger.info(f"📝 Logging orchestration event: {event_name}")
        try:
            # Generate job_id from environment or create one
            job_id = os.getenv("AWS_BATCH_JOB_ID", f"crawl-{int(time.time())}")
            
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

    def log_session_to_dynamodb(self, result: Dict[str, Any]) -> bool:
        """Log the entire crawl session to DynamoDB as orchestration event"""
        event_name = "CrawlSessionCompleted" if result["status"] == "success" else "CrawlSessionFailed"
        logger.info(f"📝 Logging crawl session to DynamoDB as {event_name}")
        
        try:
            # Log as orchestration event instead of separate crawl logs
            log_data = {
                "domain": result["domain"],
                "root_url": result["root_url"],
                "status": result["status"],
                "discovered_urls": result.get("discovered_urls", 0),
                "successful_crawls": result.get("successful_crawls", 0),
                "failed_crawls": result.get("failed_crawls", 0),
                "total_products_found": result.get("total_products_found", 0),
                "unique_products": result.get("unique_products", 0),
                "processing_time_seconds": result.get("processing_time_seconds", 0),
                "token_usage": result.get("token_usage", {}),
                "s3_urls": self.s3_urls,
                "s3_base_path": self.s3_base_path,
                "model_name": self.model_name,
                "crawl_summary": {
                    "total_content_length": sum(m.content_length for m in self.crawl_metrics),
                    "avg_crawl_duration_ms": sum(m.crawl_duration_ms for m in self.crawl_metrics) / max(len(self.crawl_metrics), 1),
                    "success_rate": result.get("successful_crawls", 0) / max(result.get("discovered_urls", 1), 1)
                }
            }
            
            success = self.log_orchestration_event(event_name, log_data)
            if success:
                logger.info(f"✅ Successfully logged crawl session to DynamoDB")
            return success
            
        except Exception as e:
            logger.error(f"❌ Error preparing DynamoDB log data: {e}")
            return False

    async def run(self, root_url: str) -> Dict[str, Any]:
        """Main execution pipeline with AWS integration"""
        logger.info(f"🚀 STARTING CRAWL PIPELINE FOR: {root_url}")
        overall_start = time.time()
        domain = self.get_domain_name(root_url)
        
        # Setup domain-specific directories
        self.setup_domain_directories(domain)
        
        logger.info(f"📋 Crawl configuration:")
        logger.info(f"   Root URL: {root_url}")
        logger.info(f"   Domain: {domain}")
        logger.info(f"   Local output directory: {self.output_dir}")
        logger.info(f"   S3 base path: {self.s3_base_path}")
        logger.info(f"   DynamoDB table: {self.aws_service.dynamodb_table}")
        logger.info(f"   Model: {self.model_name}")
        
        # Log scraping started event
        logger.info(f"📝 Logging scraping started event to DynamoDB")
        self.log_orchestration_event("ScrapingStarted", {
            "root_url": root_url,
            "domain": domain,
            "model_name": self.model_name,
            "output_dir": str(self.output_dir),
            "s3_base_path": self.s3_base_path
        })
        
        try:
            # Step 1: Discover all URLs
            logger.info(f"🔍 STEP 1: Discovering URLs from {root_url}")
            discovered_urls = await self.discover_all_urls(root_url)
            
            if not discovered_urls:
                logger.error("❌ No URLs discovered. Exiting.")
                error_result = {
                    "root_url": root_url,
                    "domain": domain,
                    "error": "No URLs could be discovered",
                    "status": "failed",
                    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
                }
                self.log_session_to_dynamodb(error_result)
                return error_result
            
            # Step 2: Save URL tree
            logger.info(f"📋 STEP 2: Saving URL tree")
            self.save_url_tree(domain, root_url)
            
            # Step 3: Display URL tree
            logger.info(f"📊 STEP 3: Displaying URL tree")
            self.display_url_tree(discovered_urls, domain)
            
            # Step 4: Crawl each URL
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
            
            # Step 6: Save all outputs to local and S3
            logger.info(f"\n💾 STEP 6: Saving outputs to local and S3")
            logger.info(f"   Saving all products CSV ({len(self.all_products)} products)")
            self.save_products_csv(self.all_products, domain, "_all_products")
            
            logger.info(f"   Saving unique products CSV ({len(unique_products)} products)")
            self.save_products_csv(unique_products, domain, "_unique_products")
            
            logger.info(f"   Saving comprehensive JSON output")
            self.save_json_output(domain, crawl_results, self.all_products, unique_products)
            
            # Prepare final result
            total_time = round(time.time() - overall_start, 3)
            successful_crawls = sum(1 for r in crawl_results if r.get('success'))
            
            result = {
                "root_url": root_url,
                "domain": domain,
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
                "token_usage": {
                    "total_input_tokens": self.total_token_usage.input_tokens,
                    "total_output_tokens": self.total_token_usage.output_tokens,
                    "total_cost_usd": round(self.total_token_usage.total_cost, 4),
                    "model_name": self.model_name,
                    "average_cost_per_url": round(self.total_token_usage.total_cost / max(successful_crawls, 1), 4)
                },
                "crawl_results": crawl_results
            }
            
            # Step 7: Log to DynamoDB
            logger.info(f"\n📝 STEP 7: Logging session to DynamoDB")
            dynamo_success = self.log_session_to_dynamodb(result)
            
            # Log scraping completed event
            logger.info(f"📝 Logging scraping completed event")
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
                "dynamodb_logged": dynamo_success
            })
            
            # Final summary
            logger.info(f"\n✅ CRAWLING COMPLETE!")
            logger.info(f"📊 SUMMARY:")
            logger.info(f"   URLs discovered: {len(discovered_urls)}")
            logger.info(f"   Successfully crawled: {successful_crawls}")
            logger.info(f"   Failed crawls: {len(discovered_urls) - successful_crawls}")
            logger.info(f"   Total products found: {len(self.all_products)}")
            logger.info(f"   Unique products: {len(unique_products)}")
            logger.info(f"   Total time: {total_time}s")
            logger.info(f"   Total token cost: \\${self.total_token_usage.total_cost:.4f}")
            logger.info(f"   Local backup: {self.output_dir}")
            logger.info(f"   S3 location: {self.s3_base_path}")
            logger.info(f"   DynamoDB logged: {'Yes' if dynamo_success else 'No'}")
            
            return result
            
        except Exception as e:
            logger.error(f"❌ Pipeline failed: {str(e)}")
            error_result = {
                "root_url": root_url,
                "domain": domain,
                "error": str(e),
                "status": "failed",
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
            }
            
            # Log scraping failed event
            logger.error(f"📝 Logging scraping failed event")
            self.log_orchestration_event("ScrapingFailed", {
                "root_url": root_url,
                "domain": domain,
                "error": str(e),
                "processing_time_seconds": round(time.time() - overall_start, 3)
            })
            
            self.log_session_to_dynamodb(error_result)
            return error_result

async def main():
    """Main function for batch processing"""
    logger.info("🚀 Starting batch processing")
    parser = argparse.ArgumentParser(description='E-commerce website crawler')
    parser.add_argument('--url', type=str, help='URL to crawl')
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
                output_dir = args.output_dir
                logger.info(f"📋 Parsed arguments - URL: {url}, Output dir: {output_dir}")
            except SystemExit:
                # If argument parsing fails, use default values
                logger.warning("⚠️ Argument parsing failed, using default values")
                url = None
                output_dir = os.getenv('OUTPUT_DIR', '/tmp/crawl_output')
        else:
            # No meaningful arguments provided, use default values
            logger.info("📋 No meaningful arguments provided, using default values")
            url = None
            output_dir = os.getenv('OUTPUT_DIR', '/tmp/crawl_output')
    else:
        # Extract parameters from event data
        # Check various locations where the URL might be
        url = None
        logger.info("🔍 Extracting URL from event data")
        
        # Check in body (POST request)
        if 'body' in event_data:
            try:
                body = event_data['body']
                if isinstance(body, str):
                    body = json.loads(body)
                url = body.get('url')
                if url:
                    logger.info(f"✅ Found URL in request body: {url}")
            except (json.JSONDecodeError, AttributeError):
                logger.warning("⚠️ Failed to parse body as JSON")
                pass
        
        # Check in queryStringParameters (GET request)
        if not url and 'queryStringParameters' in event_data and event_data['queryStringParameters']:
            url = event_data['queryStringParameters'].get('url')
            if url:
                logger.info(f"✅ Found URL in query parameters: {url}")
        
        # Check directly in the event
        if not url:
            url = event_data.get('url')
            if url:
                logger.info(f"✅ Found URL directly in event: {url}")
        
        # Get output directory from event or use default
        output_dir = event_data.get('output_dir', os.getenv('OUTPUT_DIR', '/tmp/crawl_output'))
        logger.info(f"📁 Output directory: {output_dir}")
    
    # Debug logging
    logger.info(f"📋 Final parameters - URL: {url}, Output dir: {output_dir}")
    
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
        
        # Initialize crawler with fixed model
        logger.info(f"🔄 Initializing EnhancedWebCrawler")
        crawler = EnhancedWebCrawler(
            model_name='gemini-1.5-flash',
            output_dir=output_dir
        )
        
        # Run the full pipeline
        logger.info(f"🚀 Running crawler pipeline")
        result = await crawler.run(url)
        
        # Output final status for batch job
        if result["status"] == "success":
            logger.info(f"✅ JOB SUCCESSFUL: Processed {result['discovered_urls']} URLs")
            logger.info(f"📊 Results: {result['successful_crawls']} successful crawls, {result['total_products_found']} products found")
            logger.info(f"💰 Token cost: \\${result['token_usage']['total_cost_usd']}")
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
                            "domain": result["domain"],
                            "discovered_urls": result["discovered_urls"],
                            "successful_crawls": result["successful_crawls"],
                            "total_products": result["total_products_found"],
                            "unique_products": result["unique_products"],
                            "processing_time": result["processing_time_seconds"],
                            "s3_base_path": result["s3_base_path"],
                            "s3_urls": result["s3_urls"]
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
                        "message": f"Crawl failed: {error_msg}"
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
                    "message": f"Exception occurred: {error_msg}"
                })
            }
            print(json.dumps(api_response))
        
        logger.info("👋 Exiting with error status")
        sys.exit(1)

if __name__ == "__main__":
    logger.info("🏁 Application entry point")
    asyncio.run(main())