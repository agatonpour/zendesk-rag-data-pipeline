import logging
import time
from typing import Dict, Optional
from urllib.parse import quote

import requests


LOGGER = logging.getLogger(__name__)


class SharePointClient:
    GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"

    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        host: str,
        site_path: str,
        drive_name: str,
        root_folder: str,
        timeout: int = 60,
    ) -> None:
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.host = host
        self.site_path = site_path
        self.drive_name = drive_name
        self.root_folder = root_folder.strip("/")
        self.timeout = timeout

        self.session = requests.Session()
        self._site_id: Optional[str] = None
        self._drive_id: Optional[str] = None
        self._access_token: Optional[str] = None
        self._access_token_expires_at: float = 0.0

    def upload_text_file(self, filename: str, content: str) -> None:
        drive_id = self._get_drive_id()
        target_path = self._build_target_path(filename)
        encoded_path = quote(target_path)
        url = f"{self.GRAPH_BASE_URL}/drives/{drive_id}/root:/{encoded_path}:/content"
        headers = self._auth_headers()
        headers["Content-Type"] = "text/html; charset=utf-8"
        response = self.session.put(url, headers=headers, data=content.encode("utf-8"), timeout=self.timeout)
        self._raise_for_status(response)
        LOGGER.info("Uploaded %s to SharePoint path %s", filename, target_path)

    def delete_file(self, filename: str) -> None:
        drive_id = self._get_drive_id()
        target_path = self._build_target_path(filename)
        encoded_path = quote(target_path)
        url = f"{self.GRAPH_BASE_URL}/drives/{drive_id}/root:/{encoded_path}:"
        response = self.session.delete(url, headers=self._auth_headers(), timeout=self.timeout)
        if response.status_code == 404:
            LOGGER.info("SharePoint file %s did not exist, nothing to delete", target_path)
            return
        self._raise_for_status(response)
        LOGGER.info("Deleted %s from SharePoint path %s", filename, target_path)

    def _get_site_id(self) -> str:
        if self._site_id:
            return self._site_id

        site_resource = f"{self.host}:{self.site_path}"
        url = f"{self.GRAPH_BASE_URL}/sites/{site_resource}"
        response = self.session.get(url, headers=self._auth_headers(), timeout=self.timeout)
        self._raise_for_status(response)
        site = response.json()
        site_id = site.get("id")
        if not site_id:
            raise RuntimeError("Microsoft Graph did not return a site id")
        self._site_id = site_id
        LOGGER.info("Resolved SharePoint site id for %s%s", self.host, self.site_path)
        return site_id

    def _get_drive_id(self) -> str:
        if self._drive_id:
            return self._drive_id

        site_id = self._get_site_id()
        url = f"{self.GRAPH_BASE_URL}/sites/{site_id}/drives"
        response = self.session.get(url, headers=self._auth_headers(), timeout=self.timeout)
        self._raise_for_status(response)
        drives = response.json().get("value", [])
        for drive in drives:
            if drive.get("name") == self.drive_name:
                drive_id = drive.get("id")
                if not drive_id:
                    raise RuntimeError(f"Drive '{self.drive_name}' is missing an id")
                self._drive_id = drive_id
                LOGGER.info("Resolved SharePoint drive '%s'", self.drive_name)
                return drive_id
        available = ", ".join(sorted(drive.get("name", "") for drive in drives))
        raise RuntimeError(f"Could not find SharePoint drive '{self.drive_name}'. Available drives: {available}")

    def _auth_headers(self) -> Dict[str, str]:
        token = self._get_access_token()
        return {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    def _get_access_token(self) -> str:
        now = time.time()
        if self._access_token and now < self._access_token_expires_at:
            return self._access_token

        url = f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token"
        payload = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials",
        }
        response = self.session.post(url, data=payload, timeout=self.timeout)
        self._raise_for_status(response)
        body = response.json()
        token = body.get("access_token")
        if not token:
            raise RuntimeError("Azure AD token response did not include an access token")
        expires_in = int(body.get("expires_in", 3600))
        self._access_token = token
        self._access_token_expires_at = now + max(0, expires_in - 60)
        return token

    def _build_target_path(self, filename: str) -> str:
        if self.root_folder:
            return f"{self.root_folder}/{filename}"
        return filename

    @staticmethod
    def _raise_for_status(response: requests.Response) -> None:
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            detail = response.text[:1000]
            raise requests.HTTPError(f"{exc}. Response body: {detail}") from exc
