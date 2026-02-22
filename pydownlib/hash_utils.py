import asyncio
import hashlib


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
            algorithm: str = "md5",
    ) -> str:
        """
        Calculate file hash asynchronously by offloading CPU/IO work to a
        thread pool so the event loop stays responsive during large file reads.

        Args:
            filepath:  Path to file
            algorithm: Hash algorithm (md5, sha1, sha256)

        Returns:
            Hex digest of the file

        Raises:
            ValueError:        If algorithm is not supported
            FileNotFoundError: If file does not exist
        """
        algorithm = algorithm.lower()

        if not cls.validate_algorithm(algorithm):
            raise ValueError(
                f"Unsupported algorithm: {algorithm}. "
                f"Supported: {', '.join(cls.SUPPORTED_ALGORITHMS)}"
            )

        try:
            return await asyncio.to_thread(
                cls._calculate_hash_sync, filepath, algorithm
            )
        except FileNotFoundError:
            raise FileNotFoundError(f"File not found: {filepath}")

    @staticmethod
    def _calculate_hash_sync(filepath: str, algorithm: str) -> str:
        """
        Synchronous hash calculation — intended to run in a thread pool.

        Reads the file in 1 MB chunks to minimise the number of Python-level
        loop iterations while keeping memory usage constant.
        """
        hash_obj = hashlib.new(algorithm)
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                hash_obj.update(chunk)
        return hash_obj.hexdigest()
