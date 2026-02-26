"""Simple in-memory cache for storing last product search results per user."""

from typing import Dict, List, Any
from datetime import datetime, timedelta


class SearchCache:
    """In-memory cache keyed by user_phone to store recent search results and pagination state."""

    def __init__(self) -> None:
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._ttl_seconds: int = 1800  # 30 minutes

    def set_results(self, user_phone: str, results: List[Dict[str, Any]], query: str = "", offset: int = 0) -> None:
        """Store search results with query and pagination state."""
        self._cache[user_phone] = {
            "results": results,  # All results from search
            "query": query,
            "offset": offset,  # How many have been shown so far
            "timestamp": datetime.utcnow(),
        }
    
    def get_next_batch(self, user_phone: str, batch_size: int = 5) -> Dict[str, Any]:
        """Get the next batch of results that haven't been shown yet."""
        entry = self._cache.get(user_phone)
        if not entry:
            return {"results": [], "has_more": False, "total": 0, "shown": 0}
        
        # Expire old entries
        if datetime.utcnow() - entry["timestamp"] > timedelta(seconds=self._ttl_seconds):
            del self._cache[user_phone]
            return {"results": [], "has_more": False, "total": 0, "shown": 0}
        
        all_results = entry.get("results", [])
        current_offset = entry.get("offset", 0)
        total = len(all_results)
        
        # Get next batch
        next_batch = all_results[current_offset:current_offset + batch_size]
        new_offset = current_offset + len(next_batch)
        has_more = new_offset < total
        
        # Update offset
        entry["offset"] = new_offset
        
        return {
            "results": next_batch,
            "has_more": has_more,
            "total": total,
            "shown": new_offset,
            "query": entry.get("query", "")
        }

    def get_results(self, user_phone: str) -> List[Dict[str, Any]]:
        """Get cached search results."""
        entry = self._cache.get(user_phone)
        if not entry:
            return []
        # Expire old entries
        if datetime.utcnow() - entry["timestamp"] > timedelta(seconds=self._ttl_seconds):
            del self._cache[user_phone]
            return []
        return entry.get("results", [])
    
    def get_search_state(self, user_phone: str) -> Dict[str, Any]:
        """Get the complete search state including query and offset."""
        entry = self._cache.get(user_phone)
        if not entry:
            return {"query": "", "offset": 0, "results": []}
        # Expire old entries
        if datetime.utcnow() - entry["timestamp"] > timedelta(seconds=self._ttl_seconds):
            del self._cache[user_phone]
            return {"query": "", "offset": 0, "results": []}
        return {
            "query": entry.get("query", ""),
            "offset": entry.get("offset", 0),
            "results": entry.get("results", [])
        }


# Global singleton
search_cache = SearchCache()


