# Zendesk Tickets → SharePoint Pipeline for RAG

## Overview

A Python pipeline that turns resolved **Zendesk support tickets** into structured HTML documents in **SharePoint**, making support knowledge available to downstream **RAG** applications.

## What it does

- Exports solved and closed tickets with metadata and comment history
- Synchronizes changes using saved checkpoints and overlapping time windows
- Handles ticket renames, filename collisions, and API rate limits
- Removes ineligible tickets and applies a 15-month retention cleanup
- Runs nightly with Azure Functions, using local or Azure Blob Storage state

## Architecture

**Zendesk Tickets → HTML Export & Incremental Sync → SharePoint → Downstream RAG Application**

This repository provides the ingestion layer; retrieval and AI inference run separately.

## Run locally

Requires Python 3.10+, Zendesk API access, and an Azure app with access to the target SharePoint library and folder.

```bash
pip install -r requirements.txt
cp .env.example .env
# Fill in .env with your own configuration.
python main.py
```

The sync writes to SharePoint; `SYNC_MODE=test` also uploads documents. Exported content includes internal comments and ticket identifiers, so use an access-controlled destination. For Azure Functions, use Blob Storage state and supply settings as environment variables.
