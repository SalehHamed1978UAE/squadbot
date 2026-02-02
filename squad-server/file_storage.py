"""
Squad Bot — File Storage Manager
Handles local filesystem storage for shared files with versioning.
"""

import os
import base64
import shutil
import logging
from pathlib import Path
from typing import Optional, Tuple

from models import (
    generate_file_checksum, is_text_mime_type,
    MAX_FILE_SIZE_BYTES
)

logger = logging.getLogger(__name__)


class FileStorageError(Exception):
    """Base exception for file storage errors."""
    pass


class FileStorage:
    """
    Manages file storage on the local filesystem.

    Storage structure:
    /data/squads/{squad_id}/files/{file_id}/v{version}/{filename}
    """

    def __init__(self, base_path: str = "./data"):
        self.base_path = Path(base_path)
        self._ensure_base_exists()

    def _ensure_base_exists(self):
        """Ensure the base storage directory exists."""
        self.base_path.mkdir(parents=True, exist_ok=True)

    def _get_file_dir(self, squad_id: str, file_id: str, version: int) -> Path:
        """Get the directory path for a file version."""
        return self.base_path / "squads" / squad_id / "files" / file_id / f"v{version}"

    def _get_file_path(self, squad_id: str, file_id: str, version: int, filename: str) -> Path:
        """Get the full path for a file version."""
        return self._get_file_dir(squad_id, file_id, version) / filename

    def get_storage_key(self, squad_id: str, file_id: str, version: int, filename: str) -> str:
        """Generate a storage key for a file version."""
        return f"squads/{squad_id}/files/{file_id}/v{version}/{filename}"

    def store_file(self, squad_id: str, file_id: str, version: int, filename: str,
                   content: bytes) -> Tuple[str, str, int]:
        """
        Store a file and return (storage_key, checksum, size_bytes).

        Args:
            squad_id: The squad ID
            file_id: The file ID
            version: The version number
            filename: The filename
            content: The file content as bytes

        Returns:
            Tuple of (storage_key, checksum, size_bytes)

        Raises:
            FileStorageError: If storage fails
        """
        size_bytes = len(content)

        if size_bytes > MAX_FILE_SIZE_BYTES:
            raise FileStorageError(f"File size {size_bytes} exceeds maximum of {MAX_FILE_SIZE_BYTES} bytes")

        # Calculate checksum
        checksum = generate_file_checksum(content)

        # Create directory and write file
        file_path = self._get_file_path(squad_id, file_id, version, filename)
        try:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_bytes(content)
        except OSError as e:
            raise FileStorageError(f"Failed to store file: {e}")

        storage_key = self.get_storage_key(squad_id, file_id, version, filename)
        logger.info(f"Stored file: {storage_key} ({size_bytes} bytes)")

        return storage_key, checksum, size_bytes

    def store_file_from_content(self, squad_id: str, file_id: str, version: int,
                                 filename: str, content: str, mime_type: str,
                                 encoding: str = "auto") -> Tuple[str, str, int]:
        """
        Store a file from string content (text or base64).

        Args:
            squad_id: The squad ID
            file_id: The file ID
            version: The version number
            filename: The filename
            content: The content as string (text or base64-encoded)
            mime_type: The MIME type
            encoding: "text", "base64", or "auto" (detect from mime_type)

        Returns:
            Tuple of (storage_key, checksum, size_bytes)
        """
        # Determine if content is text or base64
        if encoding == "auto":
            is_text = is_text_mime_type(mime_type)
        else:
            is_text = (encoding == "text")

        # Convert to bytes
        if is_text:
            content_bytes = content.encode('utf-8')
        else:
            try:
                content_bytes = base64.b64decode(content)
            except Exception as e:
                raise FileStorageError(f"Invalid base64 content: {e}")

        return self.store_file(squad_id, file_id, version, filename, content_bytes)

    def read_file(self, storage_key: str) -> Optional[bytes]:
        """
        Read a file by its storage key.

        Args:
            storage_key: The storage key from store_file

        Returns:
            File content as bytes, or None if not found
        """
        file_path = self.base_path / storage_key
        if not file_path.exists():
            return None
        try:
            return file_path.read_bytes()
        except OSError as e:
            logger.error(f"Failed to read file {storage_key}: {e}")
            return None

    def read_file_as_content(self, storage_key: str, mime_type: str) -> Optional[Tuple[str, str]]:
        """
        Read a file and return content with encoding info.

        Args:
            storage_key: The storage key
            mime_type: The MIME type

        Returns:
            Tuple of (content, encoding) where encoding is "text" or "base64",
            or None if file not found
        """
        content_bytes = self.read_file(storage_key)
        if content_bytes is None:
            return None

        if is_text_mime_type(mime_type):
            try:
                return content_bytes.decode('utf-8'), "text"
            except UnicodeDecodeError:
                # Fall back to base64 if not valid UTF-8
                return base64.b64encode(content_bytes).decode('ascii'), "base64"
        else:
            return base64.b64encode(content_bytes).decode('ascii'), "base64"

    def delete_file_version(self, storage_key: str) -> bool:
        """
        Delete a specific file version.

        Args:
            storage_key: The storage key

        Returns:
            True if deleted, False if not found
        """
        file_path = self.base_path / storage_key
        if not file_path.exists():
            return False
        try:
            file_path.unlink()
            # Try to remove empty parent directories
            self._cleanup_empty_dirs(file_path.parent)
            logger.info(f"Deleted file: {storage_key}")
            return True
        except OSError as e:
            logger.error(f"Failed to delete file {storage_key}: {e}")
            return False

    def delete_all_versions(self, squad_id: str, file_id: str) -> bool:
        """
        Delete all versions of a file.

        Args:
            squad_id: The squad ID
            file_id: The file ID

        Returns:
            True if deleted, False if not found
        """
        file_dir = self.base_path / "squads" / squad_id / "files" / file_id
        if not file_dir.exists():
            return False
        try:
            shutil.rmtree(file_dir)
            # Try to remove empty parent directories
            self._cleanup_empty_dirs(file_dir.parent)
            logger.info(f"Deleted all versions: {squad_id}/{file_id}")
            return True
        except OSError as e:
            logger.error(f"Failed to delete file {squad_id}/{file_id}: {e}")
            return False

    def _cleanup_empty_dirs(self, path: Path):
        """Remove empty parent directories up to base_path."""
        try:
            while path != self.base_path and path.is_dir():
                if any(path.iterdir()):
                    break  # Directory not empty
                path.rmdir()
                path = path.parent
        except OSError:
            pass  # Ignore cleanup errors

    def verify_checksum(self, storage_key: str, expected_checksum: str) -> bool:
        """
        Verify a file's checksum.

        Args:
            storage_key: The storage key
            expected_checksum: The expected SHA-256 checksum

        Returns:
            True if checksum matches, False otherwise
        """
        content = self.read_file(storage_key)
        if content is None:
            return False
        actual_checksum = generate_file_checksum(content)
        return actual_checksum == expected_checksum

    def get_storage_usage(self, squad_id: str) -> int:
        """
        Calculate total storage used by a squad.

        Args:
            squad_id: The squad ID

        Returns:
            Total bytes used
        """
        squad_dir = self.base_path / "squads" / squad_id / "files"
        if not squad_dir.exists():
            return 0

        total = 0
        for file_path in squad_dir.rglob("*"):
            if file_path.is_file():
                total += file_path.stat().st_size
        return total
