"""S3 service for handling image uploads to AWS S3."""

import os
import uuid
import time
import asyncio
import boto3
from botocore.exceptions import ClientError
from loguru import logger

from app.config import get_settings

settings = get_settings()

# Initialize S3 client
s3_client = None
if settings.aws_s3_bucket:
    try:
        s3_client = boto3.client(
            "s3",
            region_name=settings.aws_region,
            aws_access_key_id=settings.aws_access_key_id,
            aws_secret_access_key=settings.aws_secret_access_key,
            endpoint_url=f"https://s3.{settings.aws_region}.amazonaws.com"
        )
        logger.info(f"S3 client initialized for bucket: {settings.aws_s3_bucket}")
    except Exception as e:
        logger.error(f"Failed to initialize S3 client: {e}")
        s3_client = None
else:
    logger.warning("S3 bucket not configured. Image uploads will not work.")


def generate_s3_key(user_id: str, filename: str) -> str:
    """
    Generate a unique S3 key for a file.
    
    Args:
        user_id: User identifier (phone number)
        filename: Original filename
        
    Returns:
        S3 key path
    """
    ext = os.path.splitext(filename)[1] or ".jpg"
    ts = int(time.time())
    unique_id = uuid.uuid4().hex
    return f"uploads/{user_id}/{ts}_{unique_id}{ext}"


async def upload_bytes_to_s3(
    bytes_data: bytes,
    key: str,
    content_type: str = "image/jpeg"
) -> dict:
    """
    Upload bytes to S3 (async-safe via asyncio.to_thread).
    
    Args:
        bytes_data: File bytes
        key: S3 key (path)
        content_type: MIME type
        
    Returns:
        Dictionary with key and presigned URL
    """
    if not s3_client or not settings.aws_s3_bucket:
        raise RuntimeError("S3 not configured. Set S3_BUCKET environment variable.")
    
    try:
        # Upload file (blocking call in thread)
        await asyncio.to_thread(
            s3_client.put_object,
            Bucket=settings.aws_s3_bucket,
            Key=key,
            Body=bytes_data,
            ContentType=content_type
        )
        
        logger.info(f"File uploaded to S3: {key}")
        
        # Generate presigned GET URL (1 hour expiry)
        url = await asyncio.to_thread(
            s3_client.generate_presigned_url,
            "get_object",
            Params={"Bucket": settings.aws_s3_bucket, "Key": key},
            ExpiresIn=3600
        )
        
        # Ensure region-specific endpoint
        if f".s3.{settings.aws_region}.amazonaws.com" not in url:
            url = url.replace(".s3.amazonaws.com", f".s3.{settings.aws_region}.amazonaws.com")
        
        logger.info(f"Generated presigned URL for {key}")
        
        return {
            "key": key,
            "url": url,
            "bucket": settings.aws_s3_bucket
        }
        
    except ClientError as e:
        logger.error(f"S3 upload error: {e}")
        raise
    except Exception as e:
        logger.error(f"Error uploading to S3: {e}")
        raise


async def download_whatsapp_image(image_id: str, access_token: str) -> bytes:
    """
    Download image from WhatsApp Media API.
    
    Args:
        image_id: WhatsApp media ID
        access_token: WhatsApp API access token
        
    Returns:
        Image bytes
    """
    import requests
    
    try:
        # Step 1: Get media URL
        media_url_endpoint = f"https://graph.facebook.com/v21.0/{image_id}"
        headers = {"Authorization": f"Bearer {access_token}"}
        
        response = requests.get(media_url_endpoint, headers=headers)
        response.raise_for_status()
        
        media_data = response.json()
        media_url = media_data.get("url")
        
        if not media_url:
            raise ValueError("No media URL in response")
        
        logger.info(f"Retrieved WhatsApp media URL for {image_id}")
        
        # Step 2: Download the actual image
        image_response = requests.get(media_url, headers=headers)
        image_response.raise_for_status()
        
        logger.info(f"Downloaded image from WhatsApp: {len(image_response.content)} bytes")
        
        return image_response.content
        
    except Exception as e:
        logger.error(f"Error downloading WhatsApp image {image_id}: {e}")
        raise


async def upload_whatsapp_image_to_s3(
    image_id: str,
    user_phone: str,
    access_token: str
) -> dict:
    """
    Download image from WhatsApp and upload to S3.
    
    Args:
        image_id: WhatsApp media ID
        user_phone: User's phone number
        access_token: WhatsApp API access token
        
    Returns:
        Dictionary with S3 key and URL
    """
    try:
        # Download image from WhatsApp
        image_bytes = await download_whatsapp_image(image_id, access_token)
        
        # Generate S3 key
        key = generate_s3_key(user_phone, f"whatsapp_{image_id}.jpg")
        
        # Upload to S3
        result = await upload_bytes_to_s3(
            bytes_data=image_bytes,
            key=key,
            content_type="image/jpeg"
        )
        
        logger.info(f"WhatsApp image {image_id} uploaded to S3: {result['key']}")
        
        return result
        
    except Exception as e:
        logger.error(f"Error processing WhatsApp image {image_id}: {e}")
        raise


async def generate_presigned_post(
    key: str,
    content_type_prefix: str = "image/"
) -> dict:
    """
    Return presigned POST data for direct browser/client uploads.
    
    Args:
        key: S3 key (path)
        content_type_prefix: Content type prefix for validation
        
    Returns:
        Presigned POST data
    """
    if not s3_client or not settings.aws_s3_bucket:
        raise RuntimeError("S3 not configured. Set S3_BUCKET environment variable.")
    
    try:
        conditions = [
            ["starts-with", "$Content-Type", content_type_prefix],
            ["content-length-range", 0, 10 * 1024 * 1024],  # up to 10MB
        ]
        
        presigned = await asyncio.to_thread(
            s3_client.generate_presigned_post,
            Bucket=settings.aws_s3_bucket,
            Key=key,
            Fields={"Content-Type": ""},
            Conditions=conditions,
            ExpiresIn=3600
        )
        
        return presigned
        
    except Exception as e:
        logger.error(f"Error generating presigned POST: {e}")
        raise

