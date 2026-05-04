"""
Perceptual image hashing service (CR-3).

Uses average hash (aHash) to detect duplicate or near-duplicate images.
Stores hashes per evaluation session to detect resubmission of previously
rejected items.
"""

from __future__ import annotations

import base64
import io
from dataclasses import dataclass
from typing import Optional

from loguru import logger

# In-memory hash store: maps hash -> session info
# In production, this should be backed by Redis or PostgreSQL
_hash_store: dict[str, dict] = {}


@dataclass
class DuplicateCheckResult:
    """Result of duplicate image check."""
    is_duplicate: bool
    matching_session_id: Optional[str] = None
    matching_hash: Optional[str] = None
    similarity: float = 0.0
    message: str = ""


def _compute_ahash(image_data: str, hash_size: int = 8) -> str | None:
    """Compute average hash (aHash) for an image.

    Returns hex string representation of the hash, or None on failure.
    """
    try:
        from PIL import Image
        import numpy as np

        # Decode base64
        if "," in image_data:
            image_data = image_data.split(",", 1)[1]

        raw = base64.b64decode(image_data)
        img = Image.open(io.BytesIO(raw))

        # Convert to grayscale and resize to hash_size x hash_size
        img = img.convert("L").resize((hash_size, hash_size), Image.LANCZOS)

        # Compute average hash
        pixels = np.array(img)
        mean_val = pixels.mean()
        bits = (pixels > mean_val).flatten()

        # Convert to hex string
        hash_int = sum(bit << i for i, bit in enumerate(bits))
        return format(hash_int, f"0{hash_size * hash_size // 4}x")

    except ImportError:
        logger.warning("Pillow not installed — image hashing disabled")
        return None
    except Exception as e:
        logger.warning(f"Failed to compute image hash: {e}")
        return None


def _hamming_distance(hash1: str, hash2: str) -> int:
    """Calculate Hamming distance between two hex hash strings."""
    try:
        int1 = int(hash1, 16)
        int2 = int(hash2, 16)
        xor = int1 ^ int2
        return bin(xor).count("1")
    except (ValueError, TypeError):
        return 64  # Maximum distance


def check_duplicates(
    image_data_list: list[str],
    current_session_id: str = "",
    threshold: int = 5,
) -> DuplicateCheckResult:
    """Check if any uploaded images match previously rejected sessions.

    Parameters
    ----------
    image_data_list : list[str]
        Base64-encoded image data strings.
    current_session_id : str
        Current evaluation session ID (to exclude self-matches).
    threshold : int
        Maximum Hamming distance to consider a match (lower = stricter).
        Default 5 out of 64 bits = ~92% similarity.

    Returns
    -------
    DuplicateCheckResult
    """
    if not image_data_list:
        return DuplicateCheckResult(is_duplicate=False)

    current_hashes = []

    for img_data in image_data_list[:8]:
        h = _compute_ahash(img_data)
        if h:
            current_hashes.append(h)

    if not current_hashes:
        return DuplicateCheckResult(is_duplicate=False)

    # Check against stored hashes from previous sessions
    for stored_hash, session_info in _hash_store.items():
        if session_info.get("session_id") == current_session_id:
            continue  # Skip self

        for current_hash in current_hashes:
            distance = _hamming_distance(current_hash, stored_hash)
            similarity = 1.0 - (distance / 64.0)

            if distance <= threshold:
                logger.warning(
                    f"CR-3 Duplicate detected: hash distance {distance} "
                    f"(similarity {similarity:.1%}) "
                    f"matching session {session_info.get('session_id', 'unknown')}"
                )
                return DuplicateCheckResult(
                    is_duplicate=True,
                    matching_session_id=session_info.get("session_id"),
                    matching_hash=stored_hash,
                    similarity=similarity,
                    message=(
                        "These photos appear to match a previous submission. "
                        "Please contact our team directly for assistance."
                    ),
                )

    return DuplicateCheckResult(is_duplicate=False)


def store_session_hashes(
    image_data_list: list[str],
    session_id: str,
    decision: str = "",
) -> int:
    """Store image hashes for a completed evaluation session.

    Only stores hashes for rejected/redirected sessions to catch
    resubmission attempts.

    Returns number of hashes stored.
    """
    # Only store hashes for rejected sessions
    if decision not in ("reject", "redirect_to_agents", "decline"):
        return 0

    stored = 0
    for img_data in image_data_list[:8]:
        h = _compute_ahash(img_data)
        if h:
            _hash_store[h] = {
                "session_id": session_id,
                "decision": decision,
                "stored_at": __import__("datetime").datetime.now().isoformat(),
            }
            stored += 1

    if stored:
        logger.info(f"CR-3: Stored {stored} image hashes for session {session_id}")

    return stored


def get_store_size() -> int:
    """Return the number of stored hashes."""
    return len(_hash_store)
