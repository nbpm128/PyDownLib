"""
Hash verification utilities for download manager
"""

import hashlib
import aiofiles


class HashVerifier:
    """Utility class for file hash verification"""

    SUPPORTED_ALGORITHMS = ("md5", "sha1", "sha256")

    @classmethod
    def validate_algorithm(cls, algorithm: str) -> bool:
        """
        Validate if algorithm is supported
        
        Args:
            algorithm: Hash algorithm name
            
        Returns:
            True if supported, False otherwise
        """
        return algorithm.lower() in HashVerifier.SUPPORTED_ALGORITHMS

    @classmethod
    async def calculate_hash(
        cls,
        filepath: str,
        algorithm: str = "md5"
    ) -> str:
        """
        Calculate file hash asynchronously
        
        Args:
            filepath: Path to file
            algorithm: Hash algorithm (md5, sha1, sha256)
            
        Returns:
            Hex digest of the file
            
        Raises:
            ValueError: If algorithm is not supported
            FileNotFoundError: If file does not exist
        """
        if not HashVerifier.validate_algorithm(algorithm):
            raise ValueError(
                f"Unsupported algorithm: {algorithm}. "
                f"Supported: {', '.join(HashVerifier.SUPPORTED_ALGORITHMS)}"
            )

        hash_obj = hashlib.new(algorithm.lower())

        try:
            async with aiofiles.open(filepath, 'rb') as f:
                while True:
                    chunk = await f.read(8192)
                    if not chunk:
                        break
                    hash_obj.update(chunk)
        except FileNotFoundError:
            raise FileNotFoundError(f"File not found: {filepath}")

        return hash_obj.hexdigest()
