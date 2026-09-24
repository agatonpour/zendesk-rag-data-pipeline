import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

from dotenv import load_dotenv

from sharepoint.client import SharePointClient
from sync.state import BlobStateStore, FileStateStore, SyncState
from zendesk.client import ZendeskClient
from zendesk.export import build_ticket_filename, render_ticket_html


LOGGER = logging.getLogger(__name__)
ALLOWED_STATUSES = {"solved", "closed"}
LAST_SYNC_TIME_KEY = "last_incremental_sync_time"
LAST_RETENTION_CUTOFF_KEY = "last_retention_cutoff"
TICKET_FILENAMES_KEY = "ticket_filenames"
SYNC_OVERLAP_MINUTES = 5
RETENTION_MONTHS = 15


def run_sync(load_local_env: bool = False) -> None:
    configure_logging()
    if load_local_env:
        load_dotenv()

    env = load_config()
    zendesk_client = ZendeskClient(
        subdomain=env["ZENDESK_SUBDOMAIN"],
        email=env["ZENDESK_EMAIL"],
        api_token=env["ZENDESK_API_TOKEN"],
    )
    sharepoint_client = SharePointClient(
        tenant_id=env["AZURE_TENANT_ID"],
        client_id=env["AZURE_CLIENT_ID"],
        client_secret=env["AZURE_CLIENT_SECRET"],
        host=env["SHAREPOINT_HOST"],
        site_path=env["SHAREPOINT_SITE_PATH"],
        drive_name=env["SHAREPOINT_DRIVE_NAME"],
        root_folder=env["SHAREPOINT_ROOT_FOLDER"],
    )

    LOGGER.info("Starting sync in %s mode", env["SYNC_MODE"])

    if env["SYNC_MODE"] == "test":
        run_test_sync(
            zendesk_client=zendesk_client,
            sharepoint_client=sharepoint_client,
            start_time=env["ZENDESK_START_TIME"],
        )
        return

    state = SyncState(build_state_store(env))
    run_incremental_sync(
        zendesk_client=zendesk_client,
        sharepoint_client=sharepoint_client,
        state=state,
        configured_start_time=env["ZENDESK_START_TIME"],
    )


def build_state_store(config: Dict[str, str]):
    backend = config["SYNC_STATE_BACKEND"]
    if backend == "blob":
        return BlobStateStore(
            connection_string=config["AZURE_WEBJOBS_STORAGE"],
            container_name=config["SYNC_STATE_CONTAINER"],
            blob_name=config["SYNC_STATE_BLOB_NAME"],
        )

    return FileStateStore(config["SYNC_STATE_PATH"])


def run_test_sync(
    zendesk_client: ZendeskClient,
    sharepoint_client: SharePointClient,
    start_time: str,
) -> None:
    LOGGER.info("Running test sync from start time %s without saving cursor state", start_time)

    processed_count = 0
    uploaded_count = 0

    query = f"updated>={to_search_datetime(start_time)}"
    ticket_stream = zendesk_client.iter_search_export(query=query)
    ticket_filenames: Dict[str, str] = {}

    while True:
        try:
            ticket = next(ticket_stream)
        except StopIteration:
            break

        processed_count += 1
        if ticket.get("status") not in ALLOWED_STATUSES:
            LOGGER.info(
                "Skipping ticket %s in test mode because status is %s",
                ticket.get("id"),
                ticket.get("status"),
            )
            continue

        ticket_id = ticket["id"]
        comments = zendesk_client.get_ticket_comments(ticket_id)
        html_document = render_ticket_html(ticket, comments)
        filename = resolve_ticket_filename(ticket=ticket, ticket_filenames=ticket_filenames)
        sharepoint_client.upload_text_file(filename, html_document)
        ticket_filenames[str(ticket_id)] = filename
        uploaded_count += 1

    LOGGER.info(
        "Test sync completed. Processed %s tickets, uploaded %s HTML files.",
        processed_count,
        uploaded_count,
    )


def run_incremental_sync(
    zendesk_client: ZendeskClient,
    sharepoint_client: SharePointClient,
    state: SyncState,
    configured_start_time: str,
) -> None:
    run_started_at = utc_now()
    ticket_filenames = state.get_dict(TICKET_FILENAMES_KEY)
    window_start = determine_incremental_window_start(
        configured_start_time=configured_start_time,
        saved_sync_time=state.get(LAST_SYNC_TIME_KEY),
    )
    query = (
        f"updated>={to_search_datetime(window_start.isoformat())} "
        f"updated<{to_search_datetime(run_started_at.isoformat())}"
    )

    LOGGER.info(
        "Incremental sync will query tickets updated from %s to %s using zendesk search export",
        to_search_datetime(window_start.isoformat()),
        to_search_datetime(run_started_at.isoformat()),
    )

    processed_count = 0
    uploaded_count = 0
    deleted_count = 0

    for ticket in zendesk_client.iter_search_export(query=query):
        processed_count += 1
        ticket_id = ticket["id"]

        if ticket.get("status") in ALLOWED_STATUSES:
            sync_ticket_html(
                zendesk_client=zendesk_client,
                sharepoint_client=sharepoint_client,
                ticket=ticket,
                ticket_filenames=ticket_filenames,
            )
            uploaded_count += 1
        else:
            LOGGER.info(
                "Ticket %s is %s and will be removed from SharePoint if present",
                ticket_id,
                ticket.get("status"),
            )
            if delete_ticket_html(
                sharepoint_client=sharepoint_client,
                ticket=ticket,
                ticket_filenames=ticket_filenames,
            ):
                deleted_count += 1

    retention_deleted_count = cleanup_expired_tickets(
        zendesk_client=zendesk_client,
        sharepoint_client=sharepoint_client,
        state=state,
        configured_start_time=configured_start_time,
        now=run_started_at,
        ticket_filenames=ticket_filenames,
    )

    state.set_dict(TICKET_FILENAMES_KEY, ticket_filenames)
    state.set(LAST_SYNC_TIME_KEY, run_started_at.isoformat().replace("+00:00", "Z"))

    LOGGER.info(
        "Incremental sync completed. Processed %s tickets, uploaded %s HTML files, deleted %s updated-ineligible files, and deleted %s expired files.",
        processed_count,
        uploaded_count,
        deleted_count,
        retention_deleted_count,
    )


def cleanup_expired_tickets(
    zendesk_client: ZendeskClient,
    sharepoint_client: SharePointClient,
    state: SyncState,
    configured_start_time: str,
    now: datetime,
    ticket_filenames: Dict[str, str],
) -> int:
    current_cutoff = subtract_months(now, RETENTION_MONTHS)
    lower_bound = determine_retention_lower_bound(
        configured_start_time=configured_start_time,
        saved_cutoff=state.get(LAST_RETENTION_CUTOFF_KEY),
    )

    if lower_bound >= current_cutoff:
        LOGGER.info("No retention cleanup needed. Current cutoff is %s", to_search_datetime(current_cutoff.isoformat()))
        state.set(LAST_RETENTION_CUTOFF_KEY, current_cutoff.isoformat().replace("+00:00", "Z"))
        return 0

    query = (
        f"created>={to_search_datetime(lower_bound.isoformat())} "
        f"created<{to_search_datetime(current_cutoff.isoformat())}"
    )
    LOGGER.info(
        "Running retention cleanup for tickets created from %s to %s",
        to_search_datetime(lower_bound.isoformat()),
        to_search_datetime(current_cutoff.isoformat()),
    )

    deleted_count = 0
    for ticket in zendesk_client.iter_search_export(query=query):
        if delete_ticket_html(
            sharepoint_client=sharepoint_client,
            ticket=ticket,
            ticket_filenames=ticket_filenames,
        ):
            deleted_count += 1

    state.set(LAST_RETENTION_CUTOFF_KEY, current_cutoff.isoformat().replace("+00:00", "Z"))
    return deleted_count


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


def load_config() -> Dict[str, str]:
    required_keys = [
        "AZURE_TENANT_ID",
        "AZURE_CLIENT_ID",
        "AZURE_CLIENT_SECRET",
        "SHAREPOINT_HOST",
        "SHAREPOINT_SITE_PATH",
        "SHAREPOINT_DRIVE_NAME",
        "SHAREPOINT_ROOT_FOLDER",
        "ZENDESK_SUBDOMAIN",
        "ZENDESK_EMAIL",
        "ZENDESK_API_TOKEN",
        "ZENDESK_START_TIME",
    ]

    config: Dict[str, str] = {}
    missing = []
    for key in required_keys:
        value = os.getenv(key)
        if value:
            config[key] = value
        else:
            missing.append(key)

    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")

    config["SYNC_MODE"] = os.getenv("SYNC_MODE", "test").strip().lower()
    if config["SYNC_MODE"] not in {"test", "incremental"}:
        raise RuntimeError("SYNC_MODE must be either 'test' or 'incremental'")

    config["SYNC_STATE_BACKEND"] = get_env_with_aliases("SYNC_STATE_BACKEND", "STATE_BACKEND", default="file").strip().lower()
    if config["SYNC_STATE_BACKEND"] not in {"file", "blob"}:
        raise RuntimeError("SYNC_STATE_BACKEND must be either 'file' or 'blob'")

    if config["SYNC_STATE_BACKEND"] == "blob":
        config["AZURE_WEBJOBS_STORAGE"] = require_env("AzureWebJobsStorage")
        config["SYNC_STATE_CONTAINER"] = require_env_with_aliases("SYNC_STATE_CONTAINER", "STATE_CONTAINER_NAME")
        config["SYNC_STATE_BLOB_NAME"] = get_env_with_aliases(
            "SYNC_STATE_BLOB_NAME",
            "STATE_BLOB_NAME",
            default="sync-state/sync_state.json",
        )
    else:
        config["SYNC_STATE_PATH"] = require_env_with_aliases("SYNC_STATE_PATH", "STATE_PATH")

    return config


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def require_env_with_aliases(*names: str) -> str:
    value = get_env_with_aliases(*names)
    if not value:
        raise RuntimeError(f"Missing required environment variable. Expected one of: {', '.join(names)}")
    return value


def get_env_with_aliases(*names: str, default: Optional[str] = None) -> Optional[str]:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return default


def determine_incremental_window_start(configured_start_time: str, saved_sync_time: Optional[str]) -> datetime:
    configured_dt = parse_iso_datetime(configured_start_time)
    if not saved_sync_time:
        return configured_dt

    overlapped_dt = parse_iso_datetime(saved_sync_time) - timedelta(minutes=SYNC_OVERLAP_MINUTES)
    return max(configured_dt, overlapped_dt)


def determine_retention_lower_bound(configured_start_time: str, saved_cutoff: Optional[str]) -> datetime:
    if saved_cutoff:
        return parse_iso_datetime(saved_cutoff)
    return parse_iso_datetime(configured_start_time)


def parse_iso_datetime(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_search_datetime(value: str) -> str:
    return parse_iso_datetime(value).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def subtract_months(value: datetime, months: int) -> datetime:
    year = value.year
    month = value.month - months
    while month <= 0:
        month += 12
        year -= 1

    day = min(value.day, days_in_month(year, month))
    return value.replace(year=year, month=month, day=day)


def days_in_month(year: int, month: int) -> int:
    if month == 12:
        next_month = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        next_month = datetime(year, month + 1, 1, tzinfo=timezone.utc)
    current_month = datetime(year, month, 1, tzinfo=timezone.utc)
    return (next_month - current_month).days


def sync_ticket_html(
    zendesk_client: ZendeskClient,
    sharepoint_client: SharePointClient,
    ticket: Dict,
    ticket_filenames: Dict[str, str],
) -> None:
    ticket_id = ticket["id"]
    ticket_key = str(ticket_id)
    filename = resolve_ticket_filename(ticket=ticket, ticket_filenames=ticket_filenames)
    previous_filename = ticket_filenames.get(ticket_key)

    if previous_filename and previous_filename != filename:
        LOGGER.info("Ticket %s title changed, deleting old SharePoint file %s", ticket_id, previous_filename)
        sharepoint_client.delete_file(previous_filename)

    comments = zendesk_client.get_ticket_comments(ticket_id)
    html_document = render_ticket_html(ticket, comments)
    sharepoint_client.upload_text_file(filename, html_document)
    ticket_filenames[ticket_key] = filename


def delete_ticket_html(
    sharepoint_client: SharePointClient,
    ticket: Dict,
    ticket_filenames: Dict[str, str],
) -> bool:
    ticket_key = str(ticket["id"])
    filename = ticket_filenames.pop(ticket_key, None) or resolve_ticket_filename(ticket, ticket_filenames)
    sharepoint_client.delete_file(filename)
    return True


def resolve_ticket_filename(ticket: Dict, ticket_filenames: Dict[str, str]) -> str:
    ticket_key = str(ticket["id"])
    current_filename = ticket_filenames.get(ticket_key)
    candidate_filename = build_ticket_filename(ticket)

    if not is_filename_claimed_by_other_ticket(candidate_filename, ticket_key, ticket_filenames):
        return candidate_filename

    if current_filename and not is_filename_claimed_by_other_ticket(current_filename, ticket_key, ticket_filenames):
        return current_filename

    return build_ticket_filename(ticket, suffix=f"-{ticket['id']}")


def is_filename_claimed_by_other_ticket(
    filename: str,
    ticket_key: str,
    ticket_filenames: Dict[str, str],
) -> bool:
    for existing_ticket_key, existing_filename in ticket_filenames.items():
        if existing_ticket_key != ticket_key and existing_filename == filename:
            return True
    return False
