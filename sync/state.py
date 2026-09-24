import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional, Protocol

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from azure.storage.blob import BlobServiceClient


LOGGER = logging.getLogger(__name__)


class StateStore(Protocol):
    def load_state(self) -> Dict[str, Any]:
        ...

    def save_state(self, state: Dict[str, Any]) -> None:
        ...


class FileStateStore:
    def __init__(self, path: str) -> None:
        self.path = Path(path)

    def load_state(self) -> Dict[str, Any]:
        if not self.path.exists():
            LOGGER.info("Sync state file does not exist yet: %s", self.path)
            return {}

        if self.path.stat().st_size == 0:
            LOGGER.warning("Sync state file is empty, treating it as a fresh state: %s", self.path)
            return {}

        with self.path.open("r", encoding="utf-8") as handle:
            try:
                data = json.load(handle)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Sync state file is not valid JSON: {self.path}") from exc

        if not isinstance(data, dict):
            raise ValueError(f"Sync state file must contain a JSON object: {self.path}")

        return data

    def save_state(self, state: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, self.path)
        LOGGER.info("Saved sync state to %s", self.path)


class BlobStateStore:
    def __init__(self, connection_string: str, container_name: str, blob_name: str) -> None:
        blob_service = BlobServiceClient.from_connection_string(connection_string)
        self.container_client = blob_service.get_container_client(container_name)
        self.blob_client = self.container_client.get_blob_client(blob_name)
        self.container_name = container_name
        self.blob_name = blob_name

    def load_state(self) -> Dict[str, Any]:
        try:
            payload = self.blob_client.download_blob().readall()
        except ResourceNotFoundError:
            LOGGER.info(
                "Sync state blob does not exist yet: %s/%s",
                self.container_name,
                self.blob_name,
            )
            return {}

        if not payload:
            LOGGER.warning(
                "Sync state blob is empty, treating it as a fresh state: %s/%s",
                self.container_name,
                self.blob_name,
            )
            return {}

        try:
            data = json.loads(payload.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Sync state blob is not valid JSON: {self.container_name}/{self.blob_name}"
            ) from exc

        if not isinstance(data, dict):
            raise ValueError(
                f"Sync state blob must contain a JSON object: {self.container_name}/{self.blob_name}"
            )

        return data

    def save_state(self, state: Dict[str, Any]) -> None:
        try:
            self.container_client.create_container()
        except ResourceExistsError:
            pass
        payload = json.dumps(state, indent=2, sort_keys=True) + "\n"
        self.blob_client.upload_blob(payload.encode("utf-8"), overwrite=True)
        LOGGER.info("Saved sync state to blob %s/%s", self.container_name, self.blob_name)


class SyncState:
    def __init__(self, store: StateStore) -> None:
        self.store = store

    def load(self) -> Dict[str, Any]:
        return self.store.load_state()

    def save(self, state: Dict[str, Any]) -> None:
        self.store.save_state(state)

    def get(self, key: str) -> Optional[str]:
        state = self.load()
        value = state.get(key)
        return value if isinstance(value, str) and value else None

    def set(self, key: str, value: str) -> None:
        state = self.load()
        state[key] = value
        self.save(state)

    def get_dict(self, key: str) -> Dict[str, str]:
        state = self.load()
        value = state.get(key)
        if not isinstance(value, dict):
            return {}
        return {str(map_key): str(map_value) for map_key, map_value in value.items()}

    def set_dict(self, key: str, value: Dict[str, str]) -> None:
        state = self.load()
        state[key] = value
        self.save(state)
