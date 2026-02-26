"""Qdrant service for semantic product search."""

from typing import List, Dict, Any, Optional
from qdrant_client import QdrantClient
from qdrant_client.http import models
from qdrant_client.http.models import Distance, VectorParams, PointStruct
from qdrant_client.models import ScoredPoint
import numpy as np
from loguru import logger

from app.config import get_settings

settings = get_settings()


def build_product_url(handle: str, store_domain: str = "greenbay.market") -> Optional[str]:
    """
    Build Shopify product URL from handle.
    
    Args:
        handle: Product handle from Shopify (e.g., "solstar-mwo25me9dbkbss-25l-microwave-oven-black")
        store_domain: Store domain (default: "greenbay.market")
        
    Returns:
        Full product URL or None if handle is missing
        
    Example:
        >>> build_product_url("solstar-mwo25me9dbkbss-25l-microwave-oven-black")
        'https://greenbay.market/products/solstar-mwo25me9dbkbss-25l-microwave-oven-black'
    """
    if handle:
        return f"https://{store_domain}/products/{handle}"
    return None


class QdrantService:
    """Service for interacting with Qdrant vector database."""
    
    def __init__(self):
        """Initialize Qdrant client and load sentence-transformers model once."""
        self.client = QdrantClient(
            url=settings.qdrant_url,
            api_key=settings.qdrant_api_key
        )
        self.collection_name = settings.qdrant_collection_name
        
        # Load sentence-transformers model ONCE at initialization
        # This prevents loading the model for every query (performance optimization)
        self._embedding_model = None
        self._cross_encoder = None  # Will be loaded on-demand for reranking
        try:
            from sentence_transformers import SentenceTransformer
            self._embedding_model = SentenceTransformer('all-mpnet-base-v2')
            logger.info("✓ Sentence-transformers model loaded successfully (all-mpnet-base-v2)")
        except ImportError:
            logger.warning("sentence-transformers not installed, will use dummy vectors")
        except Exception as e:
            logger.error(f"Error loading sentence-transformers model: {e}")
        
    async def search_products(
        self, 
        query: str, 
        limit: int = 5,
        offset: int = 0,
        score_threshold: float = 0.3,
        filter_conditions: Optional[Dict] = None,
        use_reranking: Optional[bool] = None,
        rerank_top_k: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Search for products using semantic similarity with optional server-side reranking.
        
        Args:
            query: Search query text
            limit: Maximum number of results to return
            offset: Number of results to skip (for pagination)
            score_threshold: Minimum similarity score
            filter_conditions: Optional filters (e.g., price range, category)
            use_reranking: Whether to use server-side reranking (default: from config)
            rerank_top_k: Number of candidates to fetch for reranking (default: from config)
            
        Returns:
            List of product dictionaries with metadata, reranked for better relevance
        """
        try:
            # Use config defaults if not specified
            if use_reranking is None:
                use_reranking = settings.qdrant_use_reranking
            if rerank_top_k is None:
                rerank_top_k = settings.qdrant_rerank_top_k
            
            # Build filter if conditions provided
            qdrant_filter = self._build_filter(filter_conditions)
            
            # Get query vector for initial search
            query_vector = self._get_query_vector(query)
            
            if use_reranking:
                # Reranking implementation using client-side cross-encoder
                # Note: True server-side reranking requires Qdrant Cloud/Enterprise or
                # a collection with multiple vector fields (hybrid search with prefetch)
                try:
                    # Step 1: Fetch more candidates than needed for reranking
                    search_results = self.client.query_points(
                        collection_name=self.collection_name,
                        query=query_vector,
                        limit=rerank_top_k,  # Fetch more candidates
                        query_filter=qdrant_filter,
                        score_threshold=score_threshold,
                        with_payload=True
                    ).points
                    
                    # Step 2: Rerank candidates using cross-encoder
                    if len(search_results) > limit:
                        reranked_results = await self._rerank_with_cross_encoder(
                            query=query,
                            candidates=search_results,
                            top_k=limit + offset
                        )
                        search_results = reranked_results
                    else:
                        # Not enough results to rerank, use as-is
                        search_results = search_results[:limit + offset]
                    
                    logger.info(f"✓ Reranked {len(search_results)} results using cross-encoder")
                    
                except Exception as rerank_error:
                    logger.warning(f"Reranking failed ({rerank_error}), falling back to standard search")
                    # Fallback to standard search without reranking
                    search_results = self.client.query_points(
                        collection_name=self.collection_name,
                        query=query_vector,
                        limit=limit + offset,
                        query_filter=qdrant_filter,
                        score_threshold=score_threshold,
                        with_payload=True
                    ).points
            else:
                # Standard search without reranking
                total_to_fetch = limit + offset
                search_results = self.client.query_points(
                    collection_name=self.collection_name,
                    query=query_vector,
                    limit=total_to_fetch,
                    query_filter=qdrant_filter,
                    score_threshold=score_threshold,
                    with_payload=True
                ).points

            # Handle results from search() vs query_points()
            # search() returns list of ScoredPoint directly
            # query_points() returns .points attribute
            if not isinstance(search_results, list):
                # If it's a query_points result object, extract points
                if hasattr(search_results, 'points'):
                    search_results = search_results.points
                else:
                    # Convert to list if it's an iterator
                    search_results = list(search_results)
            
            # Apply offset by slicing results
            search_results = search_results[offset:offset + limit]
            
            products = []
            for point in search_results:
                # Handle both ScoredPoint and dict-like objects
                if isinstance(point, ScoredPoint):
                    payload = point.payload or {}
                    point_id = point.id
                    score = point.score
                else:
                    # Fallback for other point types
                    payload = getattr(point, 'payload', {}) or {}
                    point_id = getattr(point, 'id', None)
                    score = getattr(point, 'score', 0.0)
                
                # Build product URL from handle
                handle = payload.get("handle")
                product_url = build_product_url(handle) if handle else None
                
                product = {
                    "id": point_id,
                    "score": score,
                    "payload": payload,
                    "url": product_url  # Add product URL
                }
                products.append(product)
                
            logger.info(f"Found {len(products)} products for query: '{query}' (reranking: {use_reranking})")
            return products
            
        except Exception as e:
            logger.error(f"Error searching products: {e}", exc_info=True)
            return []
    
    async def get_product_by_id(self, product_id: str) -> Optional[Dict[str, Any]]:
        """
        Get a specific product by ID.
        
        Args:
            product_id: Product identifier
            
        Returns:
            Product dictionary or None if not found
        """
        try:
            # Convert string ID to integer for Qdrant
            point_id = int(product_id)
            result = self.client.retrieve(
                collection_name=self.collection_name,
                ids=[point_id],
                with_payload=True
            )
            
            if result:
                payload = result[0].payload
                handle = payload.get("handle")
                product_url = build_product_url(handle) if handle else None
                
                return {
                    "id": result[0].id,
                    "payload": payload,
                    "url": product_url  # Add product URL
                }
            return None
            
        except Exception as e:
            logger.error(f"Error getting product {product_id}: {e}")
            return None
    
    async def get_products_by_ids(self, product_ids: List[str]) -> List[Dict[str, Any]]:
        """
        Get multiple products by their IDs.
        
        Args:
            product_ids: List of product identifiers
            
        Returns:
            List of product dictionaries
        """
        try:
            # Convert string IDs to integers for Qdrant
            point_ids = [int(pid) for pid in product_ids]
            results = self.client.retrieve(
                collection_name=self.collection_name,
                ids=point_ids,
                with_payload=True
            )
            
            products = []
            for result in results:
                payload = result.payload
                handle = payload.get("handle")
                product_url = build_product_url(handle) if handle else None
                
                products.append({
                    "id": result.id,
                    "payload": payload,
                    "url": product_url  # Add product URL
                })
                
            return products
            
        except Exception as e:
            logger.error(f"Error getting products {product_ids}: {e}")
            return []
    
    async def search_similar_products(
        self, 
        product_id: str, 
        limit: int = 5,
        exclude_self: bool = True
    ) -> List[Dict[str, Any]]:
        """
        Find products similar to a given product.
        
        Args:
            product_id: Reference product ID
            limit: Maximum number of similar products
            exclude_self: Whether to exclude the reference product
            
        Returns:
            List of similar product dictionaries
        """
        try:
            # Get the reference product's vector
            reference_product = await self.get_product_by_id(product_id)
            if not reference_product:
                return []
            
            # Search for similar products
            query_vector = reference_product.get("vector")  # Assuming vector is stored
            if not query_vector:
                logger.warning(f"Product {product_id} has no vector stored")
                return []
            
            search_results = self.client.query_points(
                collection_name=self.collection_name,
                query=query_vector,  # query parameter accepts the vector directly
                limit=limit + (1 if exclude_self else 0)
            ).points

            
            products = []
            for point in search_results:
                # Handle both ScoredPoint and dict-like objects
                if isinstance(point, ScoredPoint):
                    payload = point.payload or {}
                    point_id = point.id
                    score = point.score
                else:
                    # Fallback for other point types
                    payload = getattr(point, 'payload', {}) or {}
                    point_id = getattr(point, 'id', None)
                    score = getattr(point, 'score', 0.0)
                
                if exclude_self and str(point_id) == str(product_id):
                    continue
                    
                handle = payload.get("handle")
                product_url = build_product_url(handle) if handle else None
                
                products.append({
                    "id": point_id,
                    "score": score,
                    "payload": payload,
                    "url": product_url  # Add product URL
                })
                
            return products[:limit]
            
        except Exception as e:
            logger.error(f"Error finding similar products to {product_id}: {e}")
            return []
    
    def _get_query_vector(self, query: str) -> List[float]:
        """
        Convert query text to vector representation using pre-loaded sentence-transformers model.
        
        Args:
            query: Search query text
            
        Returns:
            Vector representation of the query using all-mpnet-base-v2 (768 dimensions)
        """
        try:
            # Use the pre-loaded model (loaded once in __init__)
            if self._embedding_model is not None:
                # Generate embedding for the query using the pre-loaded model
                embedding = self._embedding_model.encode(query, convert_to_tensor=False)
                logger.debug(f"Generated query vector for '{query}' (768 dimensions)")
            return embedding.tolist()
            
            # Model not loaded, use dummy vector
            logger.warning("Embedding model not available, using dummy vector")
            return [0.0] * 768
            
        except Exception as e:
            logger.error(f"Error generating vector for query '{query}': {e}")
            # Fallback to dummy vector if generation fails
            return [0.0] * 768
    
    async def _rerank_with_cross_encoder(
        self,
        query: str,
        candidates: List[Any],
        top_k: int = 5
    ) -> List[Any]:
        """
        Rerank search results using a cross-encoder model for better relevance.
        This is a client-side reranking approach that works with any Qdrant setup.
        
        Args:
            query: Original search query text
            candidates: List of candidate results from initial search
            top_k: Number of top results to return after reranking
            
        Returns:
            Reranked list of candidates
        """
        try:
            # Lazy load cross-encoder model on first use
            if not hasattr(self, '_cross_encoder') or self._cross_encoder is None:
                try:
                    from sentence_transformers import CrossEncoder
                    # Use a lightweight cross-encoder model optimized for reranking
                    self._cross_encoder = CrossEncoder('cross-encoder/ms-marco-MiniLM-L-6-v2')
                    logger.info("✓ CrossEncoder reranker model loaded")
                except ImportError:
                    logger.warning("sentence-transformers CrossEncoder not available, using score-based reranking")
                    self._cross_encoder = None
                except Exception as e:
                    logger.warning(f"Failed to load reranker model: {e}")
                    self._cross_encoder = None
            
            # Use cross-encoder for reranking if available
            if self._cross_encoder is not None:
                # Prepare query-document pairs for reranking
                pairs = []
                for candidate in candidates:
                    # Extract text from payload for reranking
                    payload = candidate.payload if hasattr(candidate, 'payload') else getattr(candidate, 'payload', {})
                    product_name = payload.get('name', '')
                    product_description = payload.get('description', '')[:300]  # Limit description length
                    
                    # Combine name and description for reranking
                    document_text = f"{product_name} {product_description}".strip()
                    if not document_text:
                        document_text = query  # Fallback if no text available
                    pairs.append([query, document_text])
                
                # Get reranking scores (batch processing for efficiency)
                rerank_scores = self._cross_encoder.predict(pairs, show_progress_bar=False)
                
                # Combine candidates with rerank scores
                scored_candidates = list(zip(candidates, rerank_scores))
                
                # Sort by rerank score (descending)
                scored_candidates.sort(key=lambda x: x[1], reverse=True)
                
                # Return top_k reranked candidates
                reranked = [candidate for candidate, _ in scored_candidates[:top_k]]
                
                logger.debug(f"Reranked {len(candidates)} candidates to top {len(reranked)} results")
                return reranked
            else:
                # Fallback: sort by original score
                logger.debug("Using score-based reranking (no cross-encoder available)")
                scored_candidates = []
                for candidate in candidates:
                    score = candidate.score if hasattr(candidate, 'score') else getattr(candidate, 'score', 0.0)
                    scored_candidates.append((candidate, score))
                
                scored_candidates.sort(key=lambda x: x[1], reverse=True)
                return [candidate for candidate, _ in scored_candidates[:top_k]]
                
        except Exception as e:
            logger.error(f"Error in reranking: {e}", exc_info=True)
            # Fallback: return original candidates sorted by score
            scored_candidates = []
            for candidate in candidates:
                score = candidate.score if hasattr(candidate, 'score') else getattr(candidate, 'score', 0.0)
                scored_candidates.append((candidate, score))
            
            scored_candidates.sort(key=lambda x: x[1], reverse=True)
            return [candidate for candidate, _ in scored_candidates[:top_k]]
    
    def _build_filter(self, conditions: Optional[Dict]) -> Optional[models.Filter]:
        """
        Build Qdrant filter from conditions.
        
        Args:
            conditions: Filter conditions dictionary
            
        Returns:
            Qdrant filter object or None
        """
        if not conditions:
            return None
            
        # TODO: Implement filter building logic
        # Example filters: price range, category, availability
        return None
    
    async def health_check(self) -> bool:
        """
        Check if Qdrant service is healthy.
        
        Returns:
            True if service is healthy, False otherwise
        """
        try:
            collections = self.client.get_collections()
            return True
        except Exception as e:
            logger.error(f"Qdrant health check failed: {e}")
            return False


# Global service instance
qdrant_service = QdrantService()
