import logging
import time
from datetime import datetime, timezone
from typing import Dict, Generator, List, Optional
from urllib.parse import urlparse, parse_qs

import requests


LOGGER = logging.getLogger(__name__)


class ZendeskClient:
    def __init__(self, subdomain: str, email: str, api_token: str, timeout: int = 60) -> None:
        self.base_url = f"https://{subdomain}.zendesk.com/api/v2"
        self.session = requests.Session()
        self.session.auth = (f"{email}/token", api_token)
        self.session.headers.update({"Accept": "application/json"})
        self.timeout = timeout

    def iter_incremental_tickets(
        self, start_time: str, cursor: Optional[str] = None
    ) -> Generator[Dict, None, Optional[str]]:
        next_cursor = cursor
        while True:
            payload = self._get_incremental_tickets_page(start_time=start_time, cursor=next_cursor)
            tickets = payload.get("tickets", [])
            LOGGER.info(
                "Fetched %s tickets from incremental export (end_of_stream=%s)",
                len(tickets),
                payload.get("end_of_stream"),
            )
            for ticket in tickets:
                yield ticket

            after_cursor = payload.get("after_cursor")
            if payload.get("end_of_stream"):
                return after_cursor or next_cursor

            next_cursor = after_cursor or self._extract_cursor_from_next_page(payload.get("after_url"))
            if not next_cursor:
                raise RuntimeError("Zendesk incremental export did not provide a usable cursor")

    def get_ticket_comments(self, ticket_id: int) -> List[Dict]:
        url = f"{self.base_url}/tickets/{ticket_id}/comments.json"
        comments: List[Dict] = []

        while url:
            response = self.session.get(url, timeout=self.timeout)
            self._raise_for_status(response)
            payload = response.json()
            comments.extend(payload.get("comments", []))
            url = payload.get("next_page")

        LOGGER.info("Fetched %s comments for ticket %s", len(comments), ticket_id)
        return comments

    def get_ticket(self, ticket_id: int) -> Dict:
        url = f"{self.base_url}/tickets/{ticket_id}.json"
        response = self.session.get(url, timeout=self.timeout)
        self._raise_for_status(response)
        ticket = response.json().get("ticket")
        if not ticket:
            raise RuntimeError(f"Zendesk did not return a ticket payload for ticket {ticket_id}")
        LOGGER.info("Fetched ticket %s with status %s", ticket_id, ticket.get("status"))
        return ticket

    def iter_search_export(self, query: str) -> Generator[Dict, None, None]:
        url = f"{self.base_url}/search/export.json"
        params = {
            "filter[type]": "ticket",
            "query": query,
            "page[size]": 100,
        }

        while url:
            response = self._get_with_rate_limit_retry(url=url, params=params)
            payload = response.json()
            results = payload.get("results", [])
            meta = payload.get("meta", {})
            has_more = bool(meta.get("has_more"))
            LOGGER.info(
                "Fetched %s tickets from Zendesk search export (has_more=%s)",
                len(results),
                has_more,
            )
            for ticket in results:
                yield ticket

            if not has_more:
                break

            links = payload.get("links", {})
            url = links.get("next")
            params = None

    def _get_incremental_tickets_page(self, start_time: str, cursor: Optional[str]) -> Dict:
        endpoint = f"{self.base_url}/incremental/tickets/cursor.json"
        params = {"cursor": cursor} if cursor else {"start_time": self._to_unix_timestamp(start_time)}
        response = self._get_with_rate_limit_retry(url=endpoint, params=params)
        return response.json()

    def _get_with_rate_limit_retry(self, url: str, params: Optional[Dict] = None) -> requests.Response:
        while True:
            response = self.session.get(url, params=params, timeout=self.timeout)
            if response.status_code != 429:
                self._raise_for_status(response)
                return response

            retry_after = self._get_retry_after_seconds(response)
            LOGGER.warning(
                "Zendesk rate limit hit for %s. Waiting %s seconds before retrying.",
                url,
                retry_after,
            )
            time.sleep(retry_after)

    @staticmethod
    def _extract_cursor_from_next_page(next_page: Optional[str]) -> Optional[str]:
        if not next_page:
            return None
        parsed = urlparse(next_page)
        values = parse_qs(parsed.query).get("cursor")
        return values[0] if values else None

    @staticmethod
    def _to_unix_timestamp(value: str) -> int:
        normalized = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())

    @staticmethod
    def _to_search_datetime(value: str) -> str:
        normalized = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    @staticmethod
    def _get_retry_after_seconds(response: requests.Response) -> int:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return max(1, int(retry_after))
            except ValueError:
                pass
        return 60

    @staticmethod
    def _raise_for_status(response: requests.Response) -> None:
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            detail = response.text[:1000]
            raise requests.HTTPError(f"{exc}. Response body: {detail}") from exc
