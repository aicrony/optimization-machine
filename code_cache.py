"""
Code Change Cache
Stores generated code changes locally to avoid redundant Claude API calls
"""

import os
import json
import logging
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Any

# Configure logging
logging.basicConfig(
    level=os.getenv('LOG_LEVEL', 'INFO'),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Cache directory
CACHE_DIR = Path(__file__).parent / 'cache' / 'code_changes'


class CodeCache:
    """Caches generated code changes to avoid redundant API calls"""

    def __init__(self, cache_dir: Path = CACHE_DIR):
        self.cache_dir = cache_dir
        self._ensure_cache_dir()

    def _ensure_cache_dir(self):
        """Create cache directory if it doesn't exist"""
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _get_cache_path(self, strategy_id: int) -> Path:
        """Get cache file path for a strategy"""
        return self.cache_dir / f"strategy_{strategy_id}.json"

    def save(self, strategy_id: int, code_result: Dict[str, Any]) -> bool:
        """
        Save generated code changes to cache

        Args:
            strategy_id: Strategy ID
            code_result: Result from strategy_engine.generate_code()
                         Should contain 'success', 'code_changes', etc.

        Returns:
            True if saved successfully
        """
        try:
            cache_path = self._get_cache_path(strategy_id)

            cache_data = {
                'strategy_id': strategy_id,
                'cached_at': datetime.utcnow().isoformat(),
                'code_result': code_result
            }

            with open(cache_path, 'w') as f:
                json.dump(cache_data, f, indent=2)

            logger.info(f"Cached code changes for strategy {strategy_id} at {cache_path}")
            return True

        except Exception as e:
            logger.error(f"Failed to cache code changes for strategy {strategy_id}: {e}")
            return False

    def load(self, strategy_id: int) -> Optional[Dict[str, Any]]:
        """
        Load cached code changes for a strategy

        Args:
            strategy_id: Strategy ID

        Returns:
            The cached code_result dict, or None if not found
        """
        try:
            cache_path = self._get_cache_path(strategy_id)

            if not cache_path.exists():
                logger.debug(f"No cache found for strategy {strategy_id}")
                return None

            with open(cache_path, 'r') as f:
                cache_data = json.load(f)

            cached_at = cache_data.get('cached_at', 'unknown')
            logger.info(f"Loaded cached code changes for strategy {strategy_id} (cached at {cached_at})")

            return cache_data.get('code_result')

        except Exception as e:
            logger.error(f"Failed to load cache for strategy {strategy_id}: {e}")
            return None

    def exists(self, strategy_id: int) -> bool:
        """Check if cache exists for a strategy"""
        return self._get_cache_path(strategy_id).exists()

    def delete(self, strategy_id: int) -> bool:
        """
        Delete cache for a strategy (call after successful completion)

        Args:
            strategy_id: Strategy ID

        Returns:
            True if deleted (or didn't exist)
        """
        try:
            cache_path = self._get_cache_path(strategy_id)

            if cache_path.exists():
                cache_path.unlink()
                logger.info(f"Deleted cache for strategy {strategy_id}")

            return True

        except Exception as e:
            logger.error(f"Failed to delete cache for strategy {strategy_id}: {e}")
            return False

    def list_cached(self) -> List[int]:
        """List all cached strategy IDs"""
        try:
            cached = []
            for file in self.cache_dir.glob("strategy_*.json"):
                try:
                    # Extract strategy ID from filename
                    strategy_id = int(file.stem.replace("strategy_", ""))
                    cached.append(strategy_id)
                except ValueError:
                    continue
            return sorted(cached)
        except Exception as e:
            logger.error(f"Failed to list cached strategies: {e}")
            return []

    def get_cache_info(self, strategy_id: int) -> Optional[Dict[str, Any]]:
        """
        Get metadata about a cached strategy (without loading full code)

        Returns:
            Dict with cached_at, file_size, num_changes, or None
        """
        try:
            cache_path = self._get_cache_path(strategy_id)

            if not cache_path.exists():
                return None

            with open(cache_path, 'r') as f:
                cache_data = json.load(f)

            code_result = cache_data.get('code_result', {})
            code_changes = code_result.get('code_changes', [])

            return {
                'strategy_id': strategy_id,
                'cached_at': cache_data.get('cached_at'),
                'file_size_kb': round(cache_path.stat().st_size / 1024, 2),
                'num_changes': len(code_changes),
                'files_modified': [c.get('file_path', 'unknown') for c in code_changes[:10]]
            }

        except Exception as e:
            logger.error(f"Failed to get cache info for strategy {strategy_id}: {e}")
            return None


# Singleton instance
_code_cache = None


def get_code_cache() -> CodeCache:
    """Get or create code cache singleton"""
    global _code_cache
    if _code_cache is None:
        _code_cache = CodeCache()
    return _code_cache
