"""Delivery service for managing delivery operations."""

from typing import List, Dict, Any, Optional
from sqlalchemy.orm import Session
from datetime import datetime
import json
import os
from loguru import logger

from app.database.db import get_db_session
from app.database.models import Delivery, DeliveryStatus
from app.config import get_settings

settings = get_settings()


class DeliveryService:
    """Service for managing delivery operations."""
    
    def __init__(self):
        """Initialize delivery service."""
        self.delivery_zones = self._load_delivery_zones()
        self._llm_client = None
    
    def _load_delivery_zones(self) -> Dict[str, Any]:
        """Load delivery zones configuration with exact Jumia rates."""
        # Exact Jumia delivery rates as specified
        default_zones = {
            "zones": {
                "1": {
                    "name": "Nairobi",
                    "cities": [
                        "nairobi", "cbd", "westlands", "karen", "kasarani", "embakasi", 
                        "kilimani", "parklands", "lavington", "kileleshwa", "langata", 
                        "south c", "south b", "runda", "muthaiga", "ngong road", "mombasa road"
                    ],
                    "rates": {
                        "small": 160,
                        "medium": 250,
                        "large": 550
                    },
                    "timeline": "1 day"
                },
                "2": {
                    "name": "Near Nairobi",
                    "cities": [
                        "kitengela", "kiambu", "thika", "athi river", "kikuyu", "ruiru", 
                        "kangundo", "tala", "gatundu", "juja", "limuru"
                    ],
                    "rates": {
                        "small": 200,
                        "medium": 280,
                        "large": 600
                    },
                    "timeline": "1 day"
                },
                "3": {
                    "name": "Major Cities",
                    "cities": [
                        "nakuru", "kisumu", "eldoret", "nyeri", "meru", "machakos", 
                        "naivasha", "gilgil", "muranga", "karatina", "embu", "nanyuki", 
                        "chogoria", "maua", "kajiado", "masii", "kenol"
                    ],
                    "rates": {
                        "small": 250,
                        "medium": 350,
                        "large": 1200
                    },
                    "timeline": "3 days"
                },
                "4": {
                    "name": "Western & Rift Valley",
                    "cities": [
                        "narok", "bomet", "sotik", "kisii", "homa bay", "oyugis", 
                        "migori", "isiolo", "kericho", "litein", "siaya", "bondo", 
                        "maseno", "kakamega"
                    ],
                    "rates": {
                        "small": 300,
                        "medium": 490,
                        "large": 1200
                    },
                    "timeline": "3 days"
                },
                "5": {
                    "name": "Remote Areas",
                    "cities": [
                        "kitale", "kapenguria", "bungoma", "busia", "malaba", "kehancha", 
                        "isebania", "makueni", "emali", "kibwezi", "loitoktok", "garissa"
                    ],
                    "rates": {
                        "small": 400,
                        "medium": 550,
                        "large": 1200
                    },
                    "timeline": "4 days"
                },
                "6": {
                    "name": "Very Remote Areas",
                    "cities": [
                        "wajir", "kakuma", "lamu", "lokichar", "lodwar", "maralal", 
                        "mpeketoni", "marsabit", "hola", "wundanyi"
                    ],
                    "rates": {
                        "small": 450,
                        "medium": 600,
                        "large": 1500
                    },
                    "timeline": "5 days"
                }
            },
            "package_sizes": {
                "small": {
                    "weight_range": "0-5kg",
                    "description": "Phones, tablets, phone cases, chargers, earphones, power banks, screen protectors, small electronics"
                },
                "medium": {
                    "weight_range": "5-15kg", 
                    "description": "Laptops, small fridges (90L), microwaves, small TVs (32-43\"), blenders, toasters, iron boxes, table fans"
                },
                "large": {
                    "weight_range": "15-35kg",
                    "description": "Fridges 190L+, TVs 50\"+, washing machines, dryers, air conditioners, cookers, large appliances"
                }
            },
            "installation_services": {
                "fridge": {"cost": 1500, "description": "Proper setup and testing"},
                "tv": {"cost": 3500, "description": "Wall mounting and cable management"},
                "washing_machine": {"cost": 2000, "description": "Plumbing connection"},
                "air_conditioner": {"cost": 5000, "description": "Complete installation and testing"}
            }
        }
        
        # Try to load from config file if it exists
        config_path = getattr(settings, 'jumia_delivery_zones_config', './config/delivery_zones.json')
        if os.path.exists(config_path):
            try:
                with open(config_path, 'r') as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Failed to load delivery zones config: {e}")
        
        return default_zones
    
    def calculate_delivery_options(
        self,
        location: str,
        weight_kg: float,
        product_name: str
    ) -> Dict[str, Any]:
        """
        Calculate delivery options for a location.
        
        Args:
            location: Delivery location
            weight_kg: Product weight in kg
            product_name: Product name for size classification
            
        Returns:
            Dictionary with delivery options
        """
        try:
            # Determine delivery zone
            zone = self._get_delivery_zone(location)
            if not zone:
                return {
                    "success": False,
                    "message": f"Delivery not available to {location}",
                    "suggestion": "Which major city is closest to you? (e.g., Nairobi, Kisumu, Nakuru, Eldoret, Mombasa)",
                    "requires_clarification": True
                }
            
            # Determine package size
            package_size = self._get_package_size(weight_kg, product_name)
            
            # Get delivery cost
            delivery_cost = self.delivery_zones["zones"][zone]["rates"][package_size]
            timeline = self.delivery_zones["zones"][zone]["timeline"]
            zone_name = self.delivery_zones["zones"][zone]["name"]
            
            # Check if installation is available
            installation_options = self._get_installation_options(product_name)
            
            return {
                "success": True,
                "best_option": {
                    "location": location,
                    "zone": zone,
                    "zone_name": zone_name,
                    "package_size": package_size,
                    "cost": delivery_cost,
                    "timeline": timeline,
                    "weight_kg": weight_kg
                },
                "delivery": {
                    "location": location,
                    "zone": zone,
                    "zone_name": zone_name,
                    "package_size": package_size,
                    "cost": delivery_cost,
                    "timeline": timeline,
                    "weight_kg": weight_kg
                },
                "installation": installation_options
            }
            
        except Exception as e:
            logger.error(f"Error calculating delivery options: {e}")
            return {
                "success": False,
                "message": "Failed to calculate delivery options",
                "error": str(e)
            }
    
    def get_delivery_quote(self, location: str, product_name: str) -> Dict[str, Any]:
        """
        Get quick delivery quote.
        
        Args:
            location: Delivery location
            product_name: Product name
            
        Returns:
            Dictionary with delivery quote
        """
        # Estimate weight based on product name
        estimated_weight = self._estimate_weight(product_name)
        
        return self.calculate_delivery_options(location, estimated_weight, product_name)
    
    def compare_delivery_options(self, location: str) -> Dict[str, Any]:
        """
        Compare delivery options for different package sizes.
        
        Args:
            location: Delivery location
            
        Returns:
            Dictionary with delivery comparison
        """
        try:
            zone = self._get_delivery_zone(location)
            if not zone:
                return {
                    "success": False,
                    "message": f"Delivery not available to {location}"
                }
            
            zone_info = self.delivery_zones["zones"][zone]
            package_sizes = self.delivery_zones["package_sizes"]
            
            options = []
            for size, info in package_sizes.items():
                cost = zone_info["rates"][size]
                options.append({
                    "size": size,
                    "description": info["description"],
                    "weight_range": info["weight_range"],
                    "cost": cost
                })
            
            return {
                "success": True,
                "location": location,
                "zone": zone,
                "zone_name": zone_info["name"],
                "timeline": zone_info["timeline"],
                "options": options
            }
            
        except Exception as e:
            logger.error(f"Error comparing delivery options: {e}")
            return {
                "success": False,
                "message": "Failed to compare delivery options",
                "error": str(e)
            }
    
    def delivery_zones_info(self) -> Dict[str, Any]:
        """
        Get information about delivery zones.
        
        Returns:
            Dictionary with delivery zones information
        """
        try:
            zones_info = []
            for zone_id, zone_data in self.delivery_zones["zones"].items():
                zones_info.append({
                    "zone": zone_id,
                    "name": zone_data["name"],
                    "cities": zone_data["cities"],
                    "timeline": zone_data["timeline"]
                })
            
            return {
                "success": True,
                "zones": zones_info,
                "package_sizes": self.delivery_zones["package_sizes"],
                "installation_services": self.delivery_zones["installation_services"]
            }
            
        except Exception as e:
            logger.error(f"Error getting delivery zones info: {e}")
            return {
                "success": False,
                "message": "Failed to get delivery zones info",
                "error": str(e)
            }
    
    def _get_delivery_zone(self, location: str) -> Optional[str]:
        """
        Determine delivery zone from location string.
        
        Handles both simple locations (e.g., "Nairobi") and detailed addresses
        (e.g., "46 street, Hawk Lane, Lamu").
        
        Args:
            location: Location string from customer
            
        Returns:
            Zone ID (1-6) or None if not found
        """
        if not location:
            return None
            
        # Clean and normalize location
        location_clean = location.lower().strip()
        
        # Split by common separators to extract main area
        separators = [',', ';', '|', '\n', '\t']
        location_parts = [location_clean]
        
        for sep in separators:
            new_parts = []
            for part in location_parts:
                new_parts.extend([p.strip() for p in part.split(sep) if p.strip()])
            location_parts = new_parts
        
        # Check each part against zone cities
        for part in location_parts:
            part = part.strip()
            if not part:
                continue
                
            # Direct match
            for zone_id, zone_info in self.delivery_zones["zones"].items():
                for city in zone_info["cities"]:
                    if city.lower() == part.lower():
                        logger.info(f"Found zone {zone_id} for location '{part}' in '{location}'")
                        return zone_id
            
            # Partial match (e.g., "westlands" matches "westlands area")
            for zone_id, zone_info in self.delivery_zones["zones"].items():
                for city in zone_info["cities"]:
                    if city.lower() in part.lower() or part.lower() in city.lower():
                        logger.info(f"Found zone {zone_id} for partial match '{part}' -> '{city}' in '{location}'")
                        return zone_id
        
        # If no match found, return None (will trigger "closest city" question)
        logger.warning(f"No zone found for location: '{location}'")
        return None
    
    def _get_package_size(self, weight_kg: float, product_name: str) -> str:
        """
        Determine package size based on product type and weight.
        
        Uses exact classification as specified:
        - SMALL (0-5kg): Phones, tablets, accessories, small electronics
        - MEDIUM (5-15kg): Laptops, small fridges (90L), microwaves, small TVs (32-43")
        - LARGE (15-35kg): Fridges 190L+, TVs 50"+, washing machines, dryers, air conditioners
        """
        product_lower = product_name.lower()
        
        # LARGE PACKAGES (15-35kg) - Priority check
        if any(keyword in product_lower for keyword in [
            "fridge", "refrigerator", "washing machine", "dryer", "air conditioner", 
            "cooker", "oven", "tv 50", "television 50", "tv 55", "television 55",
            "tv 65", "television 65", "tv 75", "television 75"
        ]):
            return "large"
        
        # Check for large fridges (190L+)
        if "fridge" in product_lower or "refrigerator" in product_lower:
            # Extract capacity if mentioned
            import re
            capacity_match = re.search(r'(\d+)l', product_lower)
            if capacity_match:
                capacity = int(capacity_match.group(1))
                if capacity >= 190:
                    return "large"
                else:
                    return "medium"  # Small fridges (90L) are medium
        
        # Check for large TVs (50"+)
        if "tv" in product_lower or "television" in product_lower:
            # Extract size if mentioned
            import re
            size_match = re.search(r'(\d+)"', product_lower)
            if size_match:
                size = int(size_match.group(1))
                if size >= 50:
                    return "large"
                else:
                    return "medium"  # Small TVs (32-43") are medium
        
        # MEDIUM PACKAGES (5-15kg)
        if any(keyword in product_lower for keyword in [
            "laptop", "computer", "microwave", "tv 32", "television 32", 
            "tv 43", "television 43", "blender", "toaster", "iron", 
            "table fan", "small fridge", "fridge 90"
        ]):
            return "medium"
        
        # SMALL PACKAGES (0-5kg)
        if any(keyword in product_lower for keyword in [
            "phone", "smartphone", "tablet", "ipad", "accessory", "headphone", 
            "earphone", "charger", "cable", "power bank", "screen protector", 
            "case", "cover"
        ]):
            return "small"
        
        # Default based on weight if product type unclear
        if weight_kg <= 5:
            return "small"
        elif weight_kg <= 15:
            return "medium"
        else:
            return "large"
    
    def _estimate_weight(self, product_name: str) -> float:
        """Estimate product weight based on product name (uses LLM with heuristic fallback)."""
        llm_weight = self._estimate_weight_with_llm(product_name)
        if llm_weight:
            return llm_weight
        
        product_lower = product_name.lower()
        
        # Weight estimates in kg
        if any(keyword in product_lower for keyword in ["fridge", "refrigerator"]):
            return 25.0
        elif any(keyword in product_lower for keyword in ["washing machine"]):
            return 30.0
        elif any(keyword in product_lower for keyword in ["cooker", "oven"]):
            return 20.0
        elif any(keyword in product_lower for keyword in ["tv", "television"]):
            if "55" in product_name or "65" in product_name:
                return 12.0
            else:
                return 8.0
        elif any(keyword in product_lower for keyword in ["laptop", "computer"]):
            return 2.5
        elif any(keyword in product_lower for keyword in ["phone", "smartphone"]):
            return 0.2
        elif any(keyword in product_lower for keyword in ["tablet", "ipad"]):
            return 0.6
        elif any(keyword in product_lower for keyword in ["microwave"]):
            return 8.0
        elif any(keyword in product_lower for keyword in ["air conditioner"]):
            return 15.0
        else:
            return 5.0  # Default medium size

    def _estimate_weight_with_llm(self, product_name: str) -> Optional[float]:
        """
        Use an LLM to estimate product weight and package size.
        
        Returns:
            Float weight in KG if successful, otherwise None (fallback to heuristics).
        """
        if not settings.openai_api_key:
            return None
        
        try:
            if self._llm_client is None:
                from openai import OpenAI
                self._llm_client = OpenAI(api_key=settings.openai_api_key)
            
            prompt = f"""
You are estimating shipping weight.

Product name: "{product_name}"

Return a JSON object with:
{{
  "weight_kg": number between 0.1 and 50,
  "package_size": "small" | "medium" | "large"
}}

Guidelines:
- Small (0-5kg): phones, tablets, accessories, small electronics
- Medium (5-15kg): laptops, microwaves, small TVs up to 43", small fridges (90L)
- Large (15-35kg): big fridges 190L+, TVs 50\"+, washing machines, dryers, cookers
- If unsure, make your best guess based on product specs in the name.
"""
            response = self._llm_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=150
            )
            content = response.choices[0].message.content.strip()
            
            import json as _json
            import re as _re
            
            json_match = _re.search(r'\{.*\}', content, _re.DOTALL)
            if not json_match:
                return None
            
            data = _json.loads(json_match.group())
            weight = float(data.get("weight_kg", 0))
            if weight <= 0:
                return None
            return min(max(weight, 0.1), 50.0)
        except Exception as e:
            logger.warning(f"LLM weight estimation failed for '{product_name}': {e}")
            return None
    
    def _get_installation_options(self, product_name: str) -> Optional[Dict[str, Any]]:
        """Get installation options for a product."""
        product_lower = product_name.lower()
        
        for service_key, service_info in self.delivery_zones["installation_services"].items():
            if service_key in product_lower:
                return {
                    "available": True,
                    "cost": service_info["cost"],
                    "description": service_info["description"]
                }
        
        return None


# Global service instance
delivery_service = DeliveryService()
