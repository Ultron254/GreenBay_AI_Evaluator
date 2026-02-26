"""Shopify webhook handlers for product and collection management."""

import os
import json
import hmac
import hashlib
from datetime import datetime, timezone
from typing import Dict, Any
from fastapi import APIRouter, Request, HTTPException, Header
from loguru import logger

from app.config import get_settings

settings = get_settings()

shopify_router = APIRouter(prefix="/shopify/webhooks")

# Shopify webhook secret for verification (set in .env)
SHOPIFY_WEBHOOK_SECRET = os.getenv("SHOPIFY_WEBHOOK_SECRET", "")


def verify_shopify_webhook(data: bytes, hmac_header: str) -> bool:
    """
    Verify Shopify webhook signature.
    
    Args:
        data: Raw request body bytes
        hmac_header: X-Shopify-Hmac-Sha256 header value
        
    Returns:
        True if signature is valid, False otherwise
    """
    if not SHOPIFY_WEBHOOK_SECRET:
        logger.warning("SHOPIFY_WEBHOOK_SECRET not set, skipping webhook verification")
        return True  # Allow webhooks in development
    
    try:
        # Calculate HMAC
        calculated_hmac = hmac.new(
            SHOPIFY_WEBHOOK_SECRET.encode('utf-8'),
            data,
            hashlib.sha256
        ).digest()
        
        # Encode to base64
        calculated_hmac_b64 = hashlib.sha256(calculated_hmac).hexdigest()
        
        # Compare with header
        return hmac.compare_digest(calculated_hmac_b64, hmac_header)
    except Exception as e:
        logger.error(f"Error verifying webhook signature: {e}")
        return False


@shopify_router.post("/products/create")
async def product_create_webhook(
    request: Request,
    x_shopify_hmac_sha256: str = Header(None),
    x_shopify_topic: str = Header(None),
    x_shopify_shop_domain: str = Header(None)
):
    """
    Handle Shopify product creation webhook.
    
    Shopify API Version: 2025-10
    
    Webhook triggered when:
    - A new product is created in Shopify admin
    - A product is imported
    - A product is created via API
    
    Payload includes:
    - Full product details
    - Variants (price, SKU, inventory)
    - Images (URLs, dimensions)
    - Inventory information
    - Metafields (if webhook is configured to include them)
    
    Note: To include metafields in webhooks, configure in Shopify:
    Settings → Notifications → Webhooks → Edit webhook → 
    Check "Include metafields in webhook payload"
    """
    try:
        # Get raw body for signature verification
        body = await request.body()
        
        # Verify webhook signature
        if x_shopify_hmac_sha256 and not verify_shopify_webhook(body, x_shopify_hmac_sha256):
            logger.warning("Invalid Shopify webhook signature")
            raise HTTPException(status_code=401, detail="Invalid webhook signature")
        
        # Parse product data
        product_data = json.loads(body)
        
        logger.info(f"📦 Received product create webhook from {x_shopify_shop_domain}")
        logger.info(f"   Topic: {x_shopify_topic}")
        logger.info(f"   Product ID: {product_data.get('id')}")
        logger.info(f"   Product Title: {product_data.get('title')}")
        logger.info(f"   Vendor: {product_data.get('vendor')}")
        logger.info(f"   Status: {product_data.get('status')}")
        
        # Extract key information
        product_id = product_data.get("id")
        title = product_data.get("title")
        variants = product_data.get("variants", [])
        images = product_data.get("images", [])
        
        # Check if product has in-stock variants
        in_stock_variants = [
            v for v in variants 
            if v.get("inventory_quantity", 0) > 0
        ]
        
        # Get metafields if present
        metafields = product_data.get("metafields", [])
        
        logger.info(f"   Variants: {len(variants)} total, {len(in_stock_variants)} in stock")
        logger.info(f"   Images: {len(images)}")
        logger.info(f"   Metafields: {len(metafields)}")
        
        # Save to JSON file for tracking
        save_webhook_event({
            "event_type": "product_create",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "shop_domain": x_shopify_shop_domain,
            "product_id": product_id,
            "title": title,
            "variants_count": len(variants),
            "in_stock_variants": len(in_stock_variants),
            "images_count": len(images),
            "metafields_count": len(metafields),
            "product_data": product_data
        })
        
        # TODO: Sync to Qdrant vector database
        # This should be done asynchronously
        logger.info(f"✅ Product create webhook processed: {title}")
        
        return {
            "status": "success",
            "message": "Product create webhook received",
            "product_id": product_id,
            "title": title
        }
        
    except json.JSONDecodeError as e:
        logger.error(f"Error parsing webhook JSON: {e}")
        raise HTTPException(status_code=400, detail="Invalid JSON")
    except Exception as e:
        logger.error(f"Error processing product create webhook: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@shopify_router.post("/products/delete")
async def product_delete_webhook(
    request: Request,
    x_shopify_hmac_sha256: str = Header(None),
    x_shopify_topic: str = Header(None),
    x_shopify_shop_domain: str = Header(None)
):
    """
    Handle Shopify product deletion webhook.
    
    Shopify API Version: 2025-10
    
    Webhook triggered when:
    - A product is deleted from Shopify admin
    - A product is deleted via API
    - A product is archived (depending on settings)
    
    Payload includes:
    - Product ID only (minimal data)
    """
    try:
        # Get raw body for signature verification
        body = await request.body()
        
        # Verify webhook signature
        if x_shopify_hmac_sha256 and not verify_shopify_webhook(body, x_shopify_hmac_sha256):
            logger.warning("Invalid Shopify webhook signature")
            raise HTTPException(status_code=401, detail="Invalid webhook signature")
        
        # Parse deletion data
        delete_data = json.loads(body)
        
        logger.info(f"🗑️  Received product delete webhook from {x_shopify_shop_domain}")
        logger.info(f"   Topic: {x_shopify_topic}")
        logger.info(f"   Product ID: {delete_data.get('id')}")
        
        product_id = delete_data.get("id")
        
        # Save to JSON file for tracking
        save_webhook_event({
            "event_type": "product_delete",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "shop_domain": x_shopify_shop_domain,
            "product_id": product_id,
            "delete_data": delete_data
        })
        
        # TODO: Remove from Qdrant vector database
        # This should be done asynchronously
        logger.info(f"✅ Product delete webhook processed: ID {product_id}")
        
        return {
            "status": "success",
            "message": "Product delete webhook received",
            "product_id": product_id
        }
        
    except json.JSONDecodeError as e:
        logger.error(f"Error parsing webhook JSON: {e}")
        raise HTTPException(status_code=400, detail="Invalid JSON")
    except Exception as e:
        logger.error(f"Error processing product delete webhook: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@shopify_router.post("/collections/update")
async def collection_update_webhook(
    request: Request,
    x_shopify_hmac_sha256: str = Header(None),
    x_shopify_topic: str = Header(None),
    x_shopify_shop_domain: str = Header(None)
):
    """
    Handle Shopify collection update webhook.
    
    Shopify API Version: 2025-10
    
    Webhook triggered when:
    - A collection is created
    - A collection is updated (title, rules, products)
    - Products are added/removed from collection
    - Collection rules are modified
    
    Payload includes:
    - Collection details
    - Collection rules
    - Product count
    - Collection type (manual, automated)
    - Metafields (if webhook is configured to include them)
    
    Note: To include metafields in webhooks, configure in Shopify:
    Settings → Notifications → Webhooks → Edit webhook → 
    Check "Include metafields in webhook payload"
    """
    try:
        # Get raw body for signature verification
        body = await request.body()
        
        # Verify webhook signature
        if x_shopify_hmac_sha256 and not verify_shopify_webhook(body, x_shopify_hmac_sha256):
            logger.warning("Invalid Shopify webhook signature")
            raise HTTPException(status_code=401, detail="Invalid webhook signature")
        
        # Parse collection data
        collection_data = json.loads(body)
        
        logger.info(f"📚 Received collection update webhook from {x_shopify_shop_domain}")
        logger.info(f"   Topic: {x_shopify_topic}")
        logger.info(f"   Collection ID: {collection_data.get('id')}")
        logger.info(f"   Collection Title: {collection_data.get('title')}")
        logger.info(f"   Collection Type: {collection_data.get('collection_type')}")
        
        # Extract key information
        collection_id = collection_data.get("id")
        title = collection_data.get("title")
        handle = collection_data.get("handle")
        collection_type = collection_data.get("collection_type")  # "smart" or "custom"
        published = collection_data.get("published")
        
        # Get metafields if present
        metafields = collection_data.get("metafields", [])
        
        logger.info(f"   Handle: {handle}")
        logger.info(f"   Published: {published}")
        logger.info(f"   Metafields: {len(metafields)}")
        
        # Save to JSON file for tracking
        save_webhook_event({
            "event_type": "collection_update",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "shop_domain": x_shopify_shop_domain,
            "collection_id": collection_id,
            "title": title,
            "handle": handle,
            "collection_type": collection_type,
            "published": published,
            "metafields_count": len(metafields),
            "collection_data": collection_data
        })
        
        # TODO: Update collection metadata in database
        # TODO: Re-sync products in this collection if needed
        logger.info(f"✅ Collection update webhook processed: {title}")
        
        return {
            "status": "success",
            "message": "Collection update webhook received",
            "collection_id": collection_id,
            "title": title,
            "collection_type": collection_type
        }
        
    except json.JSONDecodeError as e:
        logger.error(f"Error parsing webhook JSON: {e}")
        raise HTTPException(status_code=400, detail="Invalid JSON")
    except Exception as e:
        logger.error(f"Error processing collection update webhook: {e}")
        raise HTTPException(status_code=500, detail=str(e))


def save_webhook_event(event_data: Dict[str, Any], filename="shopify_webhook_events.json"):
    """
    Save webhook event to JSON file for tracking and debugging.
    
    Args:
        event_data: Dictionary with event data
        filename: Name of the JSON file
    """
    try:
        from pathlib import Path
        
        # Save to scripts directory
        filepath = Path(__file__).parent.parent.parent / "scripts" / filename
        
        # Load existing events
        events = []
        if filepath.exists():
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    events = data.get("events", [])
            except Exception as e:
                logger.warning(f"Could not load existing webhook events: {e}")
        
        # Add new event
        events.append(event_data)
        
        # Keep only last 1000 events
        if len(events) > 1000:
            events = events[-1000:]
        
        # Save back to file
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump({
                "last_updated": datetime.now(timezone.utc).isoformat(),
                "total_events": len(events),
                "events": events
            }, f, indent=2, ensure_ascii=False)
        
        logger.debug(f"Saved webhook event to {filepath}")
        
    except Exception as e:
        logger.error(f"Error saving webhook event: {e}")


@shopify_router.get("/health")
async def shopify_webhooks_health():
    """Health check endpoint for Shopify webhooks."""
    return {
        "status": "healthy",
        "service": "Shopify Webhooks",
        "endpoints": [
            "/shopify/webhooks/products/create",
            "/shopify/webhooks/products/delete",
            "/shopify/webhooks/collections/update"
        ],
        "api_version": "2025-10",
        "webhook_secret_configured": bool(SHOPIFY_WEBHOOK_SECRET)
    }

