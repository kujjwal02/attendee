import logging
import os
import shutil
import threading
from pathlib import Path

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class FilesystemFileUploader:
    """Uploader for STORAGE_PROTOCOL == "filesystem".

    "Uploading" here means copying the locally-recorded file into the recording
    storage root (settings.RECORDING_STORAGE_ROOT), which in the self-host setup is
    an rclone FUSE mount of Google Drive. Mirrors the S3FileUploader interface
    (upload_file / wait_for_upload / delete_file / .filename) so bot_controller can
    use it interchangeably.

    Source and destination live on different filesystems (local disk -> FUSE mount),
    so we copy (never rename), then delete_file removes the local source afterwards.
    """

    def __init__(self, destination_directory, filename):
        self.destination_directory = destination_directory
        self.filename = filename
        self._upload_thread = None

    def upload_file(self, file_path: str, callback=None):
        self._upload_thread = threading.Thread(target=self._upload_worker, args=(file_path, callback), daemon=True)
        self._upload_thread.start()

    def _upload_worker(self, file_path: str, callback=None):
        try:
            source = Path(file_path)
            if not source.exists():
                raise FileNotFoundError(f"File not found: {source}")

            os.makedirs(self.destination_directory, exist_ok=True)
            destination = os.path.join(self.destination_directory, self.filename)
            shutil.copy(str(source), destination)

            logger.info(f"Successfully copied {source} to {destination}")

            if callback:
                callback(True)

        except Exception as e:
            logger.error(f"Upload error: {e}")
            if callback:
                callback(False)

    def wait_for_upload(self):
        if self._upload_thread and self._upload_thread.is_alive():
            self._upload_thread.join()

    def delete_file(self, file_path: str):
        """Delete the local source file after it has been copied to storage."""
        file_path = Path(file_path)
        if file_path.exists():
            file_path.unlink()
