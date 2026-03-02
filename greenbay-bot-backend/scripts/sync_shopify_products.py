#!/usr/bin/env python3
"""
Sync Shopify products to Qdrant and Meta Catalog.
Based on fetch_shopify.py with full sync capabilities.
"""
import os
import re
import json
import requests
import time
from datetime import datetime, timezone
from dotenv import load_dotenv
from qdrant_client import QdrantClient
from qdrant_client.http.models import Distance, VectorParams, PointStruct
from sentence_transformers import SentenceTransformer

# Load environment variables
load_dotenv()

# Shopify config
SHOPIFY_STORE = os.getenv("SHOPIFY_SHOP_DOMAIN")  # e.g. mybrandstore.myshopify.com
SHOPIFY_API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2025-10")
SHOPIFY_ACCESS_TOKEN = os.getenv("SHOPIFY_ACCESS_TOKEN")

# # Meta config
# META_ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
# CATALOG_ID = os.getenv("CATALOG_ID")

# Qdrant config
QDRANT_URL = os.getenv("QDRANT_URL", "http://qdrant:9400")
QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "products")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY")  # Optional, None if not set

# Initialize clients
qdrant = QdrantClient(url=QDRANT_URL)
model = SentenceTransformer("all-mpnet-base-v2")

# Ensure Qdrant collection exists
if not qdrant.collection_exists(QDRANT_COLLECTION):
    qdrant.create_collection(
        collection_name=QDRANT_COLLECTION,
        vectors_config=VectorParams(size=768, distance=Distance.COSINE)
    )
    print(f"✅ Created Qdrant collection: {QDRANT_COLLECTION}")


# -------------------
# Utility Functions
# -------------------
def clean_html(raw_html: str) -> str:
    """Strip HTML tags from body_html"""
    return re.sub(r'<[^>]+>', '', raw_html or '').strip()


def price_to_kes_int(price: str) -> int:
    """Convert Shopify string price to integer (KES minor units)"""
    return int(round(float(price) * 100))


# -------------------
# Step 1: Fetch all available Shopify products
# -------------------
def fetch_metafields(resource_type, resource_id, headers):
    """
    Fetch metafields for a specific resource.
    
    Args:
        resource_type: Type of resource ('products', 'collections', etc.)
        resource_id: ID of the resource
        headers: Request headers with auth token
        
    Returns:
        List of metafields or empty list if none/error
    """
    try:
        url = f"https://{SHOPIFY_STORE}/admin/api/{SHOPIFY_API_VERSION}/{resource_type}/{resource_id}/metafields.json"
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        # Small delay to avoid rate limits (0.1s = 10 requests/second max)
        time.sleep(0.1)
        return data.get("metafields", [])
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 429:
            # Rate limit hit - wait and retry once
            # Retry-After can be '2.0' (float string) or '2' (int string)
            retry_after_str = e.response.headers.get('Retry-After', '5')
            retry_after = int(float(retry_after_str))  # Convert to float first, then int
            time.sleep(retry_after)
            try:
                resp = requests.get(url, headers=headers, timeout=10)
                resp.raise_for_status()
                data = resp.json()
                time.sleep(0.1)
                return data.get("metafields", [])
            except Exception:
                return []  # Give up after one retry
        return []  # Silently fail - metafields are optional
    except Exception as e:
        # Silently fail - metafields are optional
        return []


def fetch_available_products():
    """Fetch all in-stock products from Shopify with pagination, including metafields."""
    print(f"🔗 Connecting to Shopify: {SHOPIFY_STORE}")
    print(f"📡 API Version: {SHOPIFY_API_VERSION}")
    
    url = f"https://{SHOPIFY_STORE}/admin/api/{SHOPIFY_API_VERSION}/products.json?limit=250"
    headers = {"X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN}
    available_products = []
    page_count = 0

    while url:
        page_count += 1
        print(f"📄 Fetching page {page_count}...", end='', flush=True)
        
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            
            page_products = data.get("products", [])
            print(f" {len(page_products)} products")
            
            for product in page_products:
                # Filter to only in-stock variants
                in_stock_variants = [
                    v for v in product.get("variants", []) 
                    if v.get("inventory_quantity", 0) > 0
                ]
                if in_stock_variants:
                    product["variants"] = in_stock_variants
                    
                    # Fetch metafields for this product
                    product_id = product.get("id")
                    metafields = fetch_metafields("products", product_id, headers)
                    if metafields:
                        product["metafields"] = metafields
                    
                    available_products.append(product)
            
            # Pagination via Link header
            link_header = resp.headers.get("Link")
            if link_header and 'rel="next"' in link_header:
                parts = link_header.split(",")
                next_link = [p for p in parts if 'rel="next"' in p]
                url = next_link[0].split(";")[0].strip(" <>") if next_link else None
                
                # Rate limiting: Wait 2 seconds between pages to avoid 429 errors
                if url:
                    print("⏱️  Rate limiting: waiting 2 seconds before next page...")
                    time.sleep(2)
            else:
                url = None
                
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 429:
                # Rate limit hit - wait and retry
                # Retry-After can be '2.0' (float string) or '2' (int string)
                retry_after_str = e.response.headers.get('Retry-After', '10')
                retry_after = int(float(retry_after_str))  # Convert to float first, then int
                print(f"\n⚠️  Rate limit hit (429). Waiting {retry_after} seconds before retry...")
                time.sleep(retry_after)
                # Retry same URL without incrementing page_count
                page_count -= 1
                continue
            else:
                print(f"\n❌ HTTP Error fetching page {page_count}: {e}")
                raise
        except requests.exceptions.RequestException as e:
            print(f"\n❌ Error fetching page {page_count}: {e}")
            raise

    print(f"✅ Fetched {len(available_products)} in-stock products from Shopify ({page_count} pages)")
    
    # Count products with metafields
    products_with_metafields = sum(1 for p in available_products if p.get("metafields"))
    if products_with_metafields > 0:
        print(f"   📋 {products_with_metafields} products have metafields")
    
    return available_products


# -------------------
# Step 2: Cleanup Old Products from Qdrant
# -------------------
def cleanup_qdrant_products(current_product_ids):
    """
    Remove products from Qdrant that are no longer in stock.
    
    Args:
        current_product_ids: List of Shopify product IDs that are currently in stock
    """
    print(f"\n🧹 Cleaning up out-of-stock products from Qdrant...")
    print(f"📊 Current in-stock products: {len(current_product_ids)}")
    
    try:
        from qdrant_client import QdrantClient
        from qdrant_client.models import Filter, FieldCondition, MatchAny
        
        # Connect to Qdrant (api_key is optional for local installations)
        if QDRANT_API_KEY:
            qdrant = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY)
        else:
            qdrant = QdrantClient(url=QDRANT_URL)
        
        # Get all product IDs currently in Qdrant
        print(f"   🔍 Fetching all products from Qdrant...")
        scroll_result = qdrant.scroll(
            collection_name=QDRANT_COLLECTION,
            limit=10000,  # Get all products
            with_payload=["shopify_id"],
            with_vectors=False
        )
        
        qdrant_products = scroll_result[0]  # List of points
        qdrant_ids = {point.payload.get("shopify_id") for point in qdrant_products if point.payload.get("shopify_id")}
        
        print(f"   📊 Products in Qdrant: {len(qdrant_ids)}")
        
        # Find products to delete (in Qdrant but not in current stock)
        products_to_delete = qdrant_ids - set(current_product_ids)
        
        if products_to_delete:
            print(f"   🗑️  Removing {len(products_to_delete)} out-of-stock products...")
            
            # Delete products in batches
            deleted_count = 0
            for product_id in products_to_delete:
                try:
                    qdrant.delete(
                        collection_name=QDRANT_COLLECTION,
                        points_selector=[product_id]  # Use Shopify ID as Qdrant point ID
                    )
                    deleted_count += 1
                except Exception as e:
                    print(f"   ⚠️  Error deleting product {product_id}: {e}")
            
            print(f"   ✅ Deleted {deleted_count} out-of-stock products")
        else:
            print(f"   ✅ No cleanup needed - all Qdrant products are still in stock")
        
        print(f"   📊 Final Qdrant count: {len(qdrant_ids) - len(products_to_delete)}")
        
    except Exception as e:
        print(f"   ⚠️  Error during cleanup: {e}")
        print(f"   ⚠️  Continuing with sync anyway...")


# -------------------
# Step 3: Sync to Qdrant
# -------------------
def sync_to_qdrant(products, batch_size=100):
    """Embed and store products in Qdrant (with metafields)."""
    total = len(products)
    print(f"\n📦 Syncing {total} products to Qdrant...")
    print(f"🔗 Qdrant URL: {QDRANT_URL}")
    print(f"📁 Collection: {QDRANT_COLLECTION}")
    print(f"📦 Batch size: {batch_size}")
    print(f"🤖 Embedding model: all-mpnet-base-v2 (768 dimensions)")
    print(f"📋 Metafields: INCLUDED")
    
    import time
    start_time = time.time()
    
    for i in range(0, total, batch_size):
        batch_start = time.time()
        batch = products[i:i + batch_size]
        points = []
        
        print(f"\n🔄 Processing batch {i//batch_size + 1}/{(total + batch_size - 1)//batch_size}...")
        print(f"   Products {i+1}-{min(i+len(batch), total)} of {total}")
        
        for idx, p in enumerate(batch):
            # Create searchable text
            searchable_text = " ".join([
                p.get("title", ""),
                clean_html(p.get("body_html", "")),
                p.get("vendor", ""),
                p.get("product_type", ""),
                p.get("tags", "")
            ])
            
            # Generate embedding
            embedding = model.encode(searchable_text).tolist()
            
            # Prepare payload (includes metafields)
            payload = {
                "shopify_id": p.get("id"),
                "title": p.get("title"),
                "vendor": p.get("vendor"),
                "product_type": p.get("product_type"),
                "tags": p.get("tags"),
                "handle": p.get("handle"),
                "status": p.get("status"),
                "created_at": p.get("created_at"),
                "updated_at": p.get("updated_at"),
                "published_at": p.get("published_at"),
                "body_html": clean_html(p.get("body_html", "")),
                "variants": p.get("variants", []),
                "images": p.get("images", []),
                "metafields": p.get("metafields", []),  # ← ADD METAFIELDS
                "searchable_text": searchable_text,
                "synced_at": datetime.now(timezone.utc).isoformat()
            }
            
            points.append(
                PointStruct(
                    id=p.get("id"),
                    vector=embedding,
                    payload=payload
                )
            )
            
            if (idx + 1) % 10 == 0:
                print(f"   ⚙️  Embedded {idx + 1}/{len(batch)} products in this batch...", end='\r', flush=True)
        
        # Upsert batch to Qdrant
        print(f"   💾 Uploading {len(batch)} products to Qdrant...")
        try:
            qdrant.upsert(collection_name=QDRANT_COLLECTION, points=points)
            batch_time = time.time() - batch_start
            print(f"   ✅ Batch complete ({batch_time:.1f}s) - Total: {i + len(batch)}/{total} products")
        except Exception as e:
            print(f"   ❌ Error upserting batch: {e}")
            raise
    
    total_time = time.time() - start_time
    print(f"\n{'='*60}")
    print(f"✅ Qdrant sync complete!")
    print(f"   📊 Total products synced: {total}")
    print(f"   ⏱️  Total time: {total_time:.1f}s ({total_time/60:.1f} minutes)")
    print(f"   ⚡ Average: {total_time/total:.2f}s per product")
    print(f"{'='*60}")


# # -------------------
# # Step 3: Sync to Meta Catalog
# # -------------------
# def sync_to_meta_catalog(products):
#     """Upload products to Meta Catalog."""
#     if not META_ACCESS_TOKEN or not CATALOG_ID:
#         print("\n⚠️  Meta catalog credentials not configured. Skipping Meta sync.")
#         print("   Set ACCESS_TOKEN and CATALOG_ID in .env to enable Meta catalog sync.")
#         return
    
#     print(f"\n📦 Syncing products to Meta Catalog...")
#     print(f"📁 Catalog ID: {CATALOG_ID}")
#     print(f"🔗 API: graph.facebook.com/v23.0")
    
#     # Test catalog access first
#     print(f"\n🔍 Testing Meta Catalog access...")
#     try:
#         test_url = f"https://graph.facebook.com/v23.0/{CATALOG_ID}"
#         test_headers = {"Authorization": f"Bearer {META_ACCESS_TOKEN}"}
#         test_response = requests.get(test_url, headers=test_headers, timeout=10)
        
#         if test_response.status_code == 200:
#             catalog_info = test_response.json()
#             print(f"   ✅ Catalog accessible: {catalog_info.get('name', 'Unknown')}")
#             print(f"   📊 Catalog Type: {catalog_info.get('product_catalog', {}).get('type', 'Unknown')}")
#         else:
#             print(f"   ⚠️  Catalog access issue: Status {test_response.status_code}")
#             print(f"   Response: {test_response.text[:200]}")
#             print(f"\n   ⚠️  Meta sync may fail. Continuing anyway...")
#     except Exception as e:
#         print(f"   ❌ Error testing catalog access: {e}")
#         print(f"   ⚠️  Continuing with sync anyway...")
    
#     import time
#     start_time = time.time()
    
#     url = f"https://graph.facebook.com/v23.0/{CATALOG_ID}/products"
#     headers = {
#         "Authorization": f"Bearer {META_ACCESS_TOKEN}",
#         "Content-Type": "application/json"
#     }
    
#     synced_count = 0
#     failed_count = 0
#     total_variants = sum(len(p.get("variants", [])) for p in products)
    
#     print(f"\n📊 Total variants to sync: {total_variants}")
#     print(f"📝 Sample payload structure (first product):")
    
#     # Show sample payload for debugging
#     if products and products[0].get("variants"):
#         sample_product = products[0]
#         sample_variant = sample_product["variants"][0]
#         sample_payload = {
#             "name": sample_product.get("title", "Untitled")[:80],
#             "description": clean_html(sample_product.get("body_html", ""))[:100] + "...",
#             "price": price_to_kes_int(sample_variant.get("price", "0")),
#             "currency": "KES",
#             "availability": "in stock",
#             "condition": "new",
#             "image_url": (sample_product.get("images", [{}])[0].get("src", "N/A") if sample_product.get("images") else "N/A")[:80] + "...",
#             "url": f"https://{SHOPIFY_STORE}/products/{sample_product.get('handle', '')}",
#             "brand": sample_product.get("vendor", "GreenBay Market"),
#             "retailer_id": f"shopify_{sample_product['id']}_{sample_variant['id']}"
#         }
#         import json
#         print(json.dumps(sample_payload, indent=2))
    
#     print(f"\n🚀 Starting Meta catalog sync...")
    
#     for p_idx, product in enumerate(products):
#         for variant in product.get("variants", []):
#             # Get image URL (required by Meta)
#             images = product.get("images") or []
#             image_url = images[0].get("src") if len(images) > 0 else None

#             # Skip products without images (Meta requirement)
#             if not image_url:
#                 failed_count += 1
#                 if failed_count <= 5:
#                     print(f"   ⚠️  Skipping {product.get('title', 'Unknown')[:50]} - No image")
#                 continue
            
#             # Check inventory - ONLY sync in-stock items
#             inventory_qty = variant.get("inventory_quantity", 0)
#             if inventory_qty <= 0:
#                 failed_count += 1
#                 if failed_count <= 5:
#                     print(f"   ⚠️  Skipping {product.get('title', 'Unknown')[:50]} - Out of stock (qty: {inventory_qty})")
#                 continue
            
#             # Prepare Meta catalog payload
#             meta_payload = {
#                 "retailer_id": f"shopify_{product['id']}_{variant['id']}",
#                 "name": product.get("title", "Untitled"),
#                 "description": clean_html(product.get("body_html", ""))[:5000],
#                 "price": price_to_kes_int(variant.get("price", "0")),
#                 "currency": "KES",
#                 "availability": "in stock",  # Only in-stock items reach here
#                 "condition": "new",
#                 "image_url": image_url,
#                 "url": f"https://{SHOPIFY_STORE}/products/{product.get('handle', '')}",
#                 "brand": product.get("vendor", "GreenBay Market"),
#                 "inventory": inventory_qty  # Include inventory quantity
#             }
            
#             try:
#                 # Try CREATE first
#                 response = requests.post(url, headers=headers, json=meta_payload, timeout=10)
                
#                 # If duplicate, try UPDATE instead
#                 if response.status_code == 400:
#                     error_data = response.json()
#                     error_message = error_data.get('error', {}).get('message', '')
                    
#                     # Check if it's a duplicate error
#                     if 'Duplicate retailer_id' in error_message or '10800' in str(error_data):
#                         # Use batch API to update instead
#                         retailer_id = meta_payload['retailer_id']
#                         update_payload = {
#                             "requests": [{
#                                 "method": "UPDATE",
#                                 "retailer_id": retailer_id,
#                                 "data": meta_payload
#                             }]
#                         }
                        
#                         batch_url = f"https://graph.facebook.com/v23.0/{CATALOG_ID}/batch"
#                         response = requests.post(batch_url, headers=headers, json=update_payload, timeout=10)
                
#                 if response.status_code in [200, 201]:
#                     synced_count += 1
#                     if synced_count % 50 == 0:
#                         print(f"   ✅ Synced {synced_count}/{total_variants} variants...")
#                 else:
#                     failed_count += 1
#                     if failed_count <= 5:  # Show first 5 errors with full details
#                         print(f"\n   ⚠️  FAILED: {meta_payload['retailer_id']}")
#                         print(f"      Status: {response.status_code}")
#                         print(f"      Response: {response.text[:200]}")
#                         print(f"      Product: {meta_payload.get('name', 'N/A')[:50]}")
#                         print(f"      Price: {meta_payload.get('price')} {meta_payload.get('currency')}")
#                         print(f"      Image URL: {meta_payload.get('image_url', 'N/A')[:80]}")
#                     elif failed_count == 6:
#                         print(f"   ⚠️  (Hiding further error details, {total_variants - synced_count - 6} more failures expected)")
#             except Exception as e:
#                 failed_count += 1
#                 if failed_count <= 5:  # Show first 5 errors with full details
#                     print(f"\n   ❌ EXCEPTION: {meta_payload['retailer_id']}")
#                     print(f"      Error: {str(e)}")
#                     print(f"      Product: {meta_payload.get('name', 'N/A')[:50]}")
#                 import traceback
#                 if failed_count == 1:  # Full traceback for first error only
#                     traceback.print_exc()
        
#         if (p_idx + 1) % 20 == 0:
#             print(f"   🔄 Processed {p_idx + 1}/{len(products)} products...")
    
#     total_time = time.time() - start_time
#     print(f"\n{'='*60}")
#     print(f"✅ Meta catalog sync complete!")
#     print(f"   ✅ Synced: {synced_count} variants")
#     print(f"   ❌ Failed: {failed_count} variants")
#     print(f"   ⏱️  Time: {total_time:.1f}s ({total_time/60:.1f} minutes)")
#     if synced_count > 0:
#         print(f"   ⚡ Average: {total_time/synced_count:.2f}s per variant")
#     print(f"{'='*60}")


# -------------------
# Step 2: Fetch Shopify collections
# -------------------
def fetch_collections():
    """Fetch all collections from Shopify with pagination, including metafields."""
    print(f"\n📚 Fetching collections from Shopify...")
    
    # Add initial delay before starting collections fetch to avoid rate limit
    # Products fetch just completed, so wait before hitting API again
    print("⏱️  Rate limiting: waiting 3 seconds after product fetch...")
    time.sleep(3)
    
    url = f"https://{SHOPIFY_STORE}/admin/api/{SHOPIFY_API_VERSION}/custom_collections.json?limit=250"
    headers = {"X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN}
    collections = []
    page_count = 0

    while url:
        page_count += 1
        print(f"📄 Fetching collections page {page_count}...", end='', flush=True)
        
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            
            page_collections = data.get("custom_collections", [])
            print(f" {len(page_collections)} collections")
            
            for collection in page_collections:
                # Fetch metafields for this collection
                collection_id = collection.get("id")
                metafields = fetch_metafields("collections", collection_id, headers)
                if metafields:
                    collection["metafields"] = metafields
                
                collections.append(collection)
            
            # Pagination via Link header
            link_header = resp.headers.get("Link")
            if link_header and 'rel="next"' in link_header:
                parts = link_header.split(",")
                next_link = [p for p in parts if 'rel="next"' in p]
                url = next_link[0].split(";")[0].strip(" <>") if next_link else None
                
                # Rate limiting: Wait 2 seconds between pages
                if url:
                    print("⏱️  Rate limiting: waiting 2 seconds before next page...")
                    time.sleep(2)
            else:
                url = None
                
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 429:
                # Rate limit hit - wait and retry
                # Retry-After can be '2.0' (float string) or '2' (int string)
                retry_after_str = e.response.headers.get('Retry-After', '10')
                retry_after = int(float(retry_after_str))  # Convert to float first, then int
                print(f"\n⚠️  Rate limit hit (429). Waiting {retry_after} seconds before retry...")
                time.sleep(retry_after)
                # Retry same URL without incrementing page_count
                page_count -= 1
                continue
            else:
                print(f"\n❌ HTTP Error fetching collections page {page_count}: {e}")
                raise
        except requests.exceptions.RequestException as e:
            print(f"\n❌ Error fetching collections page {page_count}: {e}")
            raise

    print(f"✅ Fetched {len(collections)} collections from Shopify")
    
    # Count collections with metafields
    collections_with_metafields = sum(1 for c in collections if c.get("metafields"))
    if collections_with_metafields > 0:
        print(f"   📋 {collections_with_metafields} collections have metafields")
    
    return collections


def save_collections_to_json(collections, filename="shopify_collections.json"):
    """
    Save Shopify collections data to a local JSON file.
    
    Args:
        collections: List of collection dictionaries from Shopify
        filename: Name of the JSON file to save
    """
    try:
        filepath = os.path.join(os.path.dirname(__file__), filename)
        
        # Prepare data with metadata
        data = {
            "sync_timestamp": datetime.now(timezone.utc).isoformat(),
            "total_collections": len(collections),
            "shopify_store": SHOPIFY_STORE,
            "api_version": SHOPIFY_API_VERSION,
            "collections": collections
        }
        
        # Calculate statistics
        published_count = sum(1 for c in collections if c.get("published_at"))
        total_metafields = sum(len(c.get("metafields", [])) for c in collections)
        
        data["statistics"] = {
            "published_collections": published_count,
            "draft_collections": len(collections) - published_count,
            "total_metafields": total_metafields,
            "collections_with_metafields": sum(1 for c in collections if len(c.get("metafields", [])) > 0),
        }
        
        print(f"\n💾 Saving collections to JSON file...")
        print(f"📁 File path: {filepath}")
        print(f"📊 Data summary:")
        print(f"   • Collections: {len(collections)}")
        print(f"   • Published: {published_count}")
        print(f"   • Metafields: {total_metafields}")
        
        # Save to file with pretty formatting
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        
        # Get file size
        file_size = os.path.getsize(filepath)
        file_size_kb = file_size / 1024
        
        print(f"\n✅ Collections saved successfully!")
        print(f"   📄 File: {filename}")
        print(f"   📏 Size: {file_size_kb:.2f} KB ({file_size:,} bytes)")
        print(f"   📍 Location: {filepath}")
        
        return filepath
        
    except Exception as e:
        print(f"\n❌ Error saving collections to JSON: {e}")
        import traceback
        traceback.print_exc()
        return None


# -------------------
# Step 3: Save products to JSON file
# -------------------
def save_products_to_json(products, filename="shopify_products.json"):
    """
    Save Shopify products data to a local JSON file.
    
    Args:
        products: List of product dictionaries from Shopify
        filename: Name of the JSON file to save
    """
    try:
        filepath = os.path.join(os.path.dirname(__file__), filename)
        
        # Prepare data with metadata
        data = {
            "sync_timestamp": datetime.now(timezone.utc).isoformat(),
            "total_products": len(products),
            "shopify_store": SHOPIFY_STORE,
            "api_version": SHOPIFY_API_VERSION,
            "products": products
        }
        
        # Calculate statistics
        total_variants = sum(len(p.get("variants", [])) for p in products)
        total_images = sum(len(p.get("images", [])) for p in products)
        total_metafields = sum(len(p.get("metafields", [])) for p in products)
        
        data["statistics"] = {
            "total_variants": total_variants,
            "total_images": total_images,
            "total_metafields": total_metafields,
            "products_with_multiple_variants": sum(1 for p in products if len(p.get("variants", [])) > 1),
            "products_with_images": sum(1 for p in products if len(p.get("images", [])) > 0),
            "products_with_metafields": sum(1 for p in products if len(p.get("metafields", [])) > 0),
        }
        
        print(f"\n💾 Saving products to JSON file...")
        print(f"📁 File path: {filepath}")
        print(f"📊 Data summary:")
        print(f"   • Products: {len(products)}")
        print(f"   • Variants: {total_variants}")
        print(f"   • Images: {total_images}")
        print(f"   • Metafields: {total_metafields}")
        
        # Save to file with pretty formatting
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        
        # Get file size
        file_size = os.path.getsize(filepath)
        file_size_mb = file_size / (1024 * 1024)
        
        print(f"\n✅ Products saved successfully!")
        print(f"   📄 File: {filename}")
        print(f"   📏 Size: {file_size_mb:.2f} MB ({file_size:,} bytes)")
        print(f"   📍 Location: {filepath}")
        
        return filepath
        
    except Exception as e:
        print(f"\n❌ Error saving products to JSON: {e}")
        import traceback
        traceback.print_exc()
        return None


# -------------------
# Step 4: Main sync function
# -------------------
def main():
    """Main sync function."""
    import time
    overall_start = time.time()
    
    print("\n" + "=" * 60)
    print("🔄 SHOPIFY PRODUCT SYNC")
    print("=" * 60)
    print(f"🕐 Started at: {datetime.now(timezone.utc).isoformat()}")
    print(f"🌍 Timezone: UTC")
    print("=" * 60)
    
    try:
        # Step 1: Fetch products
        print("\n📥 STEP 1: Fetching products from Shopify...")
        print("-" * 60)
        fetch_start = time.time()
        products = fetch_available_products()
        fetch_time = time.time() - fetch_start
        print(f"⏱️  Fetch completed in {fetch_time:.1f}s ({fetch_time/60:.1f} minutes)")
        
        if not products:
            print("\n⚠️  No in-stock products found. Exiting.")
            print("💡 Tip: Check that products in Shopify have inventory > 0")
            return
        
        # Step 2: Fetch collections
        print("\n" + "-" * 60)
        print("📚 STEP 2: Fetching collections from Shopify...")
        print("-" * 60)
        collections_start = time.time()
        collections = fetch_collections()
        collections_time = time.time() - collections_start
        print(f"⏱️  Collections fetch completed in {collections_time:.1f}s")
        
        # Save collections to JSON
        print("\n💾 Saving collections to JSON file...")
        collections_json_start = time.time()
        collections_json_file = save_collections_to_json(collections)
        collections_json_time = time.time() - collections_json_start
        print(f"⏱️  Collections JSON save completed in {collections_json_time:.1f}s")
        
        # Step 3: Save products to JSON file
        print("\n" + "-" * 60)
        print("💾 STEP 3: Saving products to JSON file...")
        print("-" * 60)
        json_start = time.time()
        json_file = save_products_to_json(products)
        json_time = time.time() - json_start
        print(f"⏱️  JSON save completed in {json_time:.1f}s")
        
        # Step 4: Cleanup & Sync to Qdrant
        print("\n" + "-" * 60)
        print("📥 STEP 4: Syncing to Qdrant...")
        print("-" * 60)
        qdrant_start = time.time()
        
        # 4a. Cleanup out-of-stock products from Qdrant
        current_product_ids = [p.get("id") for p in products]
        cleanup_qdrant_products(current_product_ids)
        
        # 4b. Sync/update in-stock products to Qdrant
        sync_to_qdrant(products)
        qdrant_time = time.time() - qdrant_start
        
        # # Step 3: Sync to Meta Catalog (optional)
        # print("\n" + "-" * 60)
        # print("📥 STEP 3: Syncing to Meta Catalog...")
        # print("-" * 60)
        # if META_ACCESS_TOKEN and CATALOG_ID:
        #     meta_start = time.time()
        #     sync_to_meta_catalog(products)
        #     meta_time = time.time() - meta_start
        # else:
        #     print("\n⚠️  Meta catalog credentials not configured. Skipping.")
        #     print("   Set ACCESS_TOKEN and CATALOG_ID in .env to enable.")
        #     meta_time = 0
        
        # Final summary
        overall_time = time.time() - overall_start
        
        print("\n" + "=" * 60)
        print("✅ SYNC COMPLETE!")
        print("=" * 60)
        print(f"📊 Summary:")
        print(f"  • Products fetched: {len(products)} (in-stock only)")
        print(f"  • Metafields fetched: {sum(len(p.get('metafields', [])) for p in products)}")
        print(f"  • Collections fetched: {len(collections)}")
        print(f"  • Qdrant: Cleaned up + synced with metafields")
        print(f"  • Products JSON: {json_file if json_file else 'Failed'}")
        print(f"  • Collections JSON: {collections_json_file if collections_json_file else 'Failed'}")
        print(f"  • Qdrant collection: {QDRANT_COLLECTION}")
        print(f"  • Sync timestamp: {datetime.now(timezone.utc).isoformat()}")
        print(f"\n⏱️  Performance:")
        print(f"  • Products fetch: {fetch_time:.1f}s ({fetch_time/60:.1f} min)")
        print(f"  • Collections fetch: {collections_time:.1f}s")
        print(f"  • Products JSON save: {json_time:.1f}s")
        print(f"  • Collections JSON save: {collections_json_time:.1f}s")
        print(f"  • Qdrant sync: {qdrant_time:.1f}s ({qdrant_time/60:.1f} min)")
        print(f"  • Total time: {overall_time:.1f}s ({overall_time/60:.1f} min)")
        # if meta_time > 0:
        #     print(f"  • Meta sync: {meta_time:.1f}s ({meta_time/60:.1f} min)")
        # print(f"  • Total time: {overall_time:.1f}s ({overall_time/60:.1f} min)")
        # print("=" * 60)
        
    except KeyboardInterrupt:
        print("\n\n⚠️  Sync interrupted by user")
        print("=" * 60)
        raise
    except Exception as e:
        print(f"\n\n❌ SYNC FAILED!")
        print("=" * 60)
        print(f"Error: {e}")
        print("=" * 60)
        import traceback
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()
