"""
Zoho Desk MCP Server
====================
A Model Context Protocol (MCP) server that exposes Zoho Desk API as tools.
Supports tickets, contacts, accounts, agents, departments, KB articles,
threads, comments, and attachments.

Usage:
    python zoho_desk_mcp_server.py

Configure by uncommenting ONE of the environment loading options below,
then uncommenting the environment variable reads beneath them.
"""

import os
import sys
import json
import asyncio
import time
import base64
import logging
from pathlib import Path
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx
from mcp.server.fastmcp import FastMCP

AGENT_DIR = Path(__file__).resolve().parent / "agent"
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from oauth_token_cache import get_or_refresh_oauth_token

# Configure logging (NEVER use print() in stdio MCP servers — it corrupts JSON-RPC)
logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger("zoho_desk_mcp")

# Suppress httpx / hpack request logs — they leak credentials (client_id, secret,
# refresh_token) in query params and OAuth tokens in Authorization headers.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("hpack").setLevel(logging.WARNING)

# =============================================================================
# ENVIRONMENT LOADING — Uncomment ONE option
# =============================================================================

# === OPTION B: Load from a SINGLE env file ===
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent / "agent" / ".env")

# === OPTION C: Rely on system environment / VS Code envFile ===
# (No loading needed — env vars injected by the caller, e.g. via .vscode/mcp.json envFile)

# =============================================================================
# ENVIRONMENT VARIABLES — Uncomment these after choosing an option above
# =============================================================================

DESK_CLIENT_ID = os.environ.get("DESK_CLIENT_ID")
DESK_CLIENT_SECRET = os.environ.get("DESK_CLIENT_SECRET")
DESK_REFRESH_TOKEN = os.environ.get("DESK_REFRESH_TOKEN")
DESK_ORG_ID = os.environ.get("DESK_ORG_ID")
DESK_API_URL = os.environ.get("DESK_API_URL", "https://desk.zoho.in/api/v1")
DESK_ACCOUNTS_URL = os.environ.get("DESK_ACCOUNTS_URL", "https://accounts.zoho.in")

# --- Region URL reference ---
# US:  DESK_API_URL=https://desk.zoho.com/api/v1      DESK_ACCOUNTS_URL=https://accounts.zoho.com
# EU:  DESK_API_URL=https://desk.zoho.eu/api/v1       DESK_ACCOUNTS_URL=https://accounts.zoho.eu
# IN:  DESK_API_URL=https://desk.zoho.in/api/v1       DESK_ACCOUNTS_URL=https://accounts.zoho.in
# AU:  DESK_API_URL=https://desk.zoho.com.au/api/v1   DESK_ACCOUNTS_URL=https://accounts.zoho.com.au
# JP:  DESK_API_URL=https://desk.zoho.jp/api/v1       DESK_ACCOUNTS_URL=https://accounts.zoho.jp


# =============================================================================
# ZOHO OAUTH HANDLER
# =============================================================================

class ZohoAuth:
    """Handles Zoho OAuth2 token refresh. Auto-refreshes 5 minutes before expiry."""

    def __init__(self, client_id: str, client_secret: str, refresh_token: str, accounts_url: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.accounts_url = accounts_url
        self.access_token: Optional[str] = None
        self.token_expiry: float = 0

    async def get_access_token(self, http_client: httpx.AsyncClient) -> str:
        """Returns a valid access token, refreshing if needed."""
        if self.access_token and time.time() < (self.token_expiry - 300):
            return self.access_token

        def refresh() -> tuple[str, int | float]:
            logger.info("Refreshing Zoho Desk access token...")
            with httpx.Client(timeout=30) as sync_client:
                resp = sync_client.post(
                    f"{self.accounts_url}/oauth/v2/token",
                    params={
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                        "grant_type": "refresh_token",
                        "refresh_token": self.refresh_token,
                    },
                )
            resp.raise_for_status()
            data = resp.json()
            if "access_token" not in data:
                raise RuntimeError(f"Token refresh failed: {data}")
            return data["access_token"], data.get("expires_in", 3600)

        self.access_token, self.token_expiry = await asyncio.to_thread(
            get_or_refresh_oauth_token,
            provider="zoho_desk",
            accounts_url=self.accounts_url,
            client_id=self.client_id,
            refresh_token=self.refresh_token,
            refresh=refresh,
        )
        logger.info("Using valid Zoho Desk access token.")
        return self.access_token


# =============================================================================
# ZOHO DESK API CLIENT
# =============================================================================

class ZohoDeskClient:
    """Async wrapper around Zoho Desk REST API v1."""

    def __init__(self, http_client: httpx.AsyncClient, auth: ZohoAuth, api_url: str, org_id: str):
        self.http = http_client
        self.auth = auth
        self.api_url = api_url.rstrip("/")
        self.org_id = org_id

    async def _headers(self) -> dict:
        token = await self.auth.get_access_token(self.http)
        return {
            "Authorization": f"Zoho-oauthtoken {token}",
            "orgId": self.org_id,
            "Content-Type": "application/json",
        }

    async def _get(self, path: str, params: Optional[dict] = None) -> Any:
        headers = await self._headers()
        resp = await self.http.get(f"{self.api_url}{path}", headers=headers, params=params)
        resp.raise_for_status()
        return resp.json()

    async def _get_raw(self, path: str, params: Optional[dict] = None) -> httpx.Response:
        """GET that returns the raw httpx Response (for binary downloads)."""
        headers = await self._headers()
        headers.pop("Content-Type", None)  # not needed for downloads
        resp = await self.http.get(f"{self.api_url}{path}", headers=headers, params=params)
        resp.raise_for_status()
        return resp

    async def _post(self, path: str, data: dict) -> Any:
        headers = await self._headers()
        resp = await self.http.post(f"{self.api_url}{path}", headers=headers, json=data)
        resp.raise_for_status()
        return resp.json()

    async def _patch(self, path: str, data: dict) -> Any:
        headers = await self._headers()
        resp = await self.http.patch(f"{self.api_url}{path}", headers=headers, json=data)
        resp.raise_for_status()
        return resp.json()

    async def _delete(self, path: str) -> dict:
        headers = await self._headers()
        resp = await self.http.delete(f"{self.api_url}{path}", headers=headers)
        resp.raise_for_status()
        return {"status": "deleted", "statusCode": resp.status_code}

    # --- Tickets ---
    async def list_tickets(self, params: Optional[dict] = None) -> Any:
        return await self._get("/tickets", params=params)

    async def get_ticket(self, ticket_id: str, params: Optional[dict] = None) -> Any:
        return await self._get(f"/tickets/{ticket_id}", params=params)

    async def create_ticket(self, data: dict) -> Any:
        return await self._post("/tickets", data)

    async def update_ticket(self, ticket_id: str, data: dict) -> Any:
        return await self._patch(f"/tickets/{ticket_id}", data)

    async def delete_ticket(self, ticket_id: str) -> Any:
        return await self._delete(f"/tickets/{ticket_id}")

    async def search_tickets(self, params: dict) -> Any:
        return await self._get("/tickets/search", params=params)

    async def get_tickets_by_view(self, view_id: str, params: Optional[dict] = None) -> Any:
        return await self._get(f"/views/{view_id}/tickets", params=params)

    # --- Contacts ---
    async def list_contacts(self, params: Optional[dict] = None) -> Any:
        return await self._get("/contacts", params=params)

    async def get_contact(self, contact_id: str) -> Any:
        return await self._get(f"/contacts/{contact_id}")

    async def create_contact(self, data: dict) -> Any:
        return await self._post("/contacts", data)

    async def update_contact(self, contact_id: str, data: dict) -> Any:
        return await self._patch(f"/contacts/{contact_id}", data)

    # --- Accounts ---
    async def list_accounts(self, params: Optional[dict] = None) -> Any:
        return await self._get("/accounts", params=params)

    async def get_account(self, account_id: str) -> Any:
        return await self._get(f"/accounts/{account_id}")

    # --- Agents ---
    async def list_agents(self, params: Optional[dict] = None) -> Any:
        return await self._get("/agents", params=params)

    # --- Departments ---
    async def list_departments(self, params: Optional[dict] = None) -> Any:
        return await self._get("/departments", params=params)

    # --- Knowledge Base Articles ---
    async def list_articles(self, params: Optional[dict] = None) -> Any:
        return await self._get("/articles", params=params)

    async def search_articles(self, params: dict) -> Any:
        return await self._get("/articles/search", params=params)

    # --- Threads & Comments ---
    async def get_ticket_threads(self, ticket_id: str, params: Optional[dict] = None) -> Any:
        """List all threads on a ticket (summaries only, no full content)."""
        return await self._get(f"/tickets/{ticket_id}/threads", params=params)

    async def get_thread(self, ticket_id: str, thread_id: str, params: Optional[dict] = None) -> Any:
        """Get a single thread's full details including content and attachments array."""
        return await self._get(f"/tickets/{ticket_id}/threads/{thread_id}", params=params)

    async def get_thread_original_content(self, ticket_id: str, thread_id: str, params: Optional[dict] = None) -> Any:
        """Get original mail content including headers for a thread."""
        return await self._get(f"/tickets/{ticket_id}/threads/{thread_id}/originalContent", params=params)

    async def get_ticket_comments(self, ticket_id: str, params: Optional[dict] = None) -> Any:
        """List all comments on a ticket."""
        return await self._get(f"/tickets/{ticket_id}/comments", params=params)

    async def get_comment(self, ticket_id: str, comment_id: str, params: Optional[dict] = None) -> Any:
        """Get a single comment's full details including attachments array."""
        return await self._get(f"/tickets/{ticket_id}/comments/{comment_id}", params=params)

    # --- Attachments ---
    async def list_ticket_attachments(self, ticket_id: str, params: Optional[dict] = None) -> Any:
        """List files uploaded directly on a ticket (not from threads/comments)."""
        return await self._get(f"/tickets/{ticket_id}/attachments", params=params)

    async def download_thread_attachment(
        self, ticket_id: str, thread_id: str, attachment_id: str
    ) -> httpx.Response:
        """Download a thread attachment's binary content via its href path."""
        return await self._get_raw(
            f"/tickets/{ticket_id}/threads/{thread_id}/attachments/{attachment_id}/content"
        )

    async def download_comment_attachment(
        self, ticket_id: str, comment_id: str, attachment_id: str
    ) -> httpx.Response:
        """Download a comment attachment's binary content via its href path."""
        return await self._get_raw(
            f"/tickets/{ticket_id}/comments/{comment_id}/attachments/{attachment_id}/content"
        )

    async def download_ticket_attachment(
        self, ticket_id: str, attachment_id: str
    ) -> httpx.Response:
        """Download a ticket-level attachment's binary content via its href path."""
        return await self._get_raw(
            f"/tickets/{ticket_id}/attachments/{attachment_id}/content"
        )


# =============================================================================
# MCP SERVER LIFESPAN — shared resources (HTTP client, auth, Desk client)
# =============================================================================

@dataclass
class AppContext:
    desk: ZohoDeskClient
    http_client: httpx.AsyncClient


@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
    """Initialize shared resources for the MCP server lifetime."""
    # -------------------------------------------------------------------------
    # Read credentials — these must be available when the server starts.
    # Uncomment the env var reads at the top of this file first.
    # -------------------------------------------------------------------------
    client_id = os.environ.get("DESK_CLIENT_ID", "")
    client_secret = os.environ.get("DESK_CLIENT_SECRET", "")
    refresh_token = os.environ.get("DESK_REFRESH_TOKEN", "")
    org_id = os.environ.get("DESK_ORG_ID", "")
    api_url = os.environ.get("DESK_API_URL", "https://desk.zoho.in/api/v1")
    accounts_url = os.environ.get("DESK_ACCOUNTS_URL", "https://accounts.zoho.in")

    if not all([client_id, client_secret, refresh_token, org_id]):
        logger.error(
            "Missing Zoho Desk credentials. Set DESK_CLIENT_ID, DESK_CLIENT_SECRET, "
            "DESK_REFRESH_TOKEN, and DESK_ORG_ID in your environment. "
            "See ZOHO_DESK_SETUP.md for instructions."
        )

    auth = ZohoAuth(client_id, client_secret, refresh_token, accounts_url)

    async with httpx.AsyncClient(timeout=30.0) as http_client:
        desk = ZohoDeskClient(http_client, auth, api_url, org_id)
        logger.info(f"Zoho Desk MCP server initialized (API: {api_url})")
        yield AppContext(desk=desk, http_client=http_client)

    logger.info("Zoho Desk MCP server shut down.")


# =============================================================================
# MCP SERVER & TOOL DEFINITIONS
# =============================================================================

mcp = FastMCP("Zoho Desk", lifespan=app_lifespan)


def _json(data: Any) -> str:
    """Serialize response data to JSON string for MCP tool output."""
    return json.dumps(data, indent=2, default=str)


def _parse_filename(content_disposition: str) -> str:
    """Extract filename from a Content-Disposition header value."""
    if not content_disposition:
        return "unknown"
    if "filename*=" in content_disposition:
        filename = content_disposition.split("filename*=")[-1].strip(' "\'')
        # Handle RFC 5987 encoding like UTF-8''filename.xlsx
        if "''" in filename:
            filename = filename.split("''", 1)[-1]
        return filename
    if "filename=" in content_disposition:
        return content_disposition.split("filename=")[-1].strip(' "\'')
    return "unknown"


# ---------------------------------------------------------------------------
# TICKETS
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_all_tickets(
    from_index: int = 0,
    limit: int = 50,
) -> str:
    """Fetch all support tickets from Zoho Desk. Use this tool whenever the user
    asks to "get tickets", "list tickets", "show tickets", "fetch tickets", or
    "retrieve all tickets". Returns ticket subject, status, priority, category,
    department, and other fields. No filters are required — call with defaults
    to get the first page of all tickets.

    Args:
        from_index: Start index for pagination (default 0).
        limit: How many tickets to return, 1-100 (default 50).
    """
    ctx: AppContext = mcp.get_context().request_context.lifespan_context
    params = {"from": from_index, "limit": min(limit, 100)}
    return _json(await ctx.desk.list_tickets(params))


@mcp.tool()
async def get_ticket_by_id(ticket_id: str) -> str:
    """Get full details of one ticket by its ID. Use when you already know the
    ticket ID and need its subject, description, status, priority, etc.

    Args:
        ticket_id: The numeric ticket ID.
    """
    ctx: AppContext = mcp.get_context().request_context.lifespan_context
    return _json(await ctx.desk.get_ticket(ticket_id))


@mcp.tool()
async def create_ticket(
    subject: str,
    department_id: str,
    contact_id: str = "",
    email: str = "",
    description: str = "",
    priority: str = "",
    status: str = "Open",
    category: str = "",
) -> str:
    """Create a new support ticket in Zoho Desk.

    Args:
        subject: Short title of the ticket (required).
        department_id: Department ID (required).
        contact_id: Contact ID of requester (optional).
        email: Requester email (optional).
        description: Full description (optional).
        priority: Low, Medium, High, or Urgent (optional).
        status: Open, Closed, On Hold (default Open).
        category: Ticket category (optional).
    """
    ctx: AppContext = mcp.get_context().request_context.lifespan_context
    data: dict[str, Any] = {"subject": subject, "departmentId": department_id, "status": status}
    if contact_id:
        data["contactId"] = contact_id
    if email:
        data["email"] = email
    if description:
        data["description"] = description
    if priority:
        data["priority"] = priority
    if category:
        data["category"] = category
    return _json(await ctx.desk.create_ticket(data))


@mcp.tool()
async def update_ticket(ticket_id: str, updates_json: str) -> str:
    """Update an existing ticket. Pass changes as a JSON string.

    Args:
        ticket_id: The ticket ID to update.
        updates_json: JSON like '{"status":"Closed","priority":"High"}'.
    """
    ctx: AppContext = mcp.get_context().request_context.lifespan_context
    data = json.loads(updates_json)
    return _json(await ctx.desk.update_ticket(ticket_id, data))


@mcp.tool()
async def delete_ticket(ticket_id: str) -> str:
    """Delete (trash) a ticket by its ID.

    Args:
        ticket_id: The ticket ID to delete.
    """
    ctx: AppContext = mcp.get_context().request_context.lifespan_context
    return _json(await ctx.desk.delete_ticket(ticket_id))


@mcp.tool()
async def search_tickets_by_keyword(
    keyword: str,
    from_index: int = 0,
    limit: int = 25,
) -> str:
    """Search tickets by a keyword or phrase. Searches subject, description, and
    ticket number. Only use this when the user wants to find specific tickets by
    text — NOT for getting all tickets (use get_all_tickets instead).

    Args:
        keyword: Text to search for in tickets (required).
        from_index: Pagination start (default 0).
        limit: Max results, 1-100 (default 25).
    """
    ctx: AppContext = mcp.get_context().request_context.lifespan_context
    params: dict[str, Any] = {
        "searchStr": keyword,
        "from": from_index,
        "limit": min(limit, 100),
    }
    return _json(await ctx.desk.search_tickets(params))


# ---------------------------------------------------------------------------
# CONTACTS
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_all_contacts(from_index: int = 0, limit: int = 25) -> str:
    """Fetch all contacts from Zoho Desk.

    Args:
        from_index: Pagination start (default 0).
        limit: Max results, 1-100 (default 25).
    """
    ctx: AppContext = mcp.get_context().request_context.lifespan_context
    params: dict[str, Any] = {"from": from_index, "limit": min(limit, 100)}
    return _json(await ctx.desk.list_contacts(params))


@mcp.tool()
async def get_contact_by_id(contact_id: str) -> str:
    """Get details of one contact by ID.

    Args:
        contact_id: The contact ID.
    """
    ctx: AppContext = mcp.get_context().request_context.lifespan_context
    return _json(await ctx.desk.get_contact(contact_id))


@mcp.tool()
async def create_contact(
    last_name: str,
    first_name: str = "",
    email: str = "",
    phone: str = "",
) -> str:
    """Create a new contact in Zoho Desk.

    Args:
        last_name: Last name (required).
        first_name: First name (optional).
        email: Email address (optional).
        phone: Phone number (optional).
    """
    ctx: AppContext = mcp.get_context().request_context.lifespan_context
    data: dict[str, Any] = {"lastName": last_name}
    if first_name:
        data["firstName"] = first_name
    if email:
        data["email"] = email
    if phone:
        data["phone"] = phone
    return _json(await ctx.desk.create_contact(data))


@mcp.tool()
async def update_contact(contact_id: str, updates_json: str) -> str:
    """Update a contact. Pass changes as JSON.

    Args:
        contact_id: The contact ID.
        updates_json: JSON like '{"email":"new@example.com"}'.
    """
    ctx: AppContext = mcp.get_context().request_context.lifespan_context
    data = json.loads(updates_json)
    return _json(await ctx.desk.update_contact(contact_id, data))


# ---------------------------------------------------------------------------
# ACCOUNTS (Organizations)
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_all_accounts(from_index: int = 0, limit: int = 25) -> str:
    """Fetch all accounts (organizations) from Zoho Desk.

    Args:
        from_index: Pagination start (default 0).
        limit: Max results, 1-100 (default 25).
    """
    ctx: AppContext = mcp.get_context().request_context.lifespan_context
    params = {"from": from_index, "limit": min(limit, 100)}
    return _json(await ctx.desk.list_accounts(params))


@mcp.tool()
async def get_account_by_id(account_id: str) -> str:
    """Get details of one account by ID.

    Args:
        account_id: The account ID.
    """
    ctx: AppContext = mcp.get_context().request_context.lifespan_context
    return _json(await ctx.desk.get_account(account_id))


# ---------------------------------------------------------------------------
# AGENTS & DEPARTMENTS
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_all_agents(from_index: int = 0, limit: int = 25) -> str:
    """Fetch all support agents from Zoho Desk.

    Args:
        from_index: Pagination start (default 0).
        limit: Max results, 1-50 (default 25).
    """
    ctx: AppContext = mcp.get_context().request_context.lifespan_context
    params: dict[str, Any] = {"from": from_index, "limit": min(limit, 50)}
    return _json(await ctx.desk.list_agents(params))


@mcp.tool()
async def get_all_departments(from_index: int = 0, limit: int = 25) -> str:
    """Fetch all departments from Zoho Desk.

    Args:
        from_index: Pagination start (default 0).
        limit: Max results (default 25).
    """
    ctx: AppContext = mcp.get_context().request_context.lifespan_context
    params = {"from": from_index, "limit": limit}
    return _json(await ctx.desk.list_departments(params))


# ---------------------------------------------------------------------------
# KNOWLEDGE BASE ARTICLES
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_all_articles(from_index: int = 0, limit: int = 25) -> str:
    """Fetch all knowledge-base articles from Zoho Desk.

    Args:
        from_index: Pagination start (default 0).
        limit: Max results, 1-100 (default 25).
    """
    ctx: AppContext = mcp.get_context().request_context.lifespan_context
    params: dict[str, Any] = {"from": from_index, "limit": min(limit, 100)}
    return _json(await ctx.desk.list_articles(params))


@mcp.tool()
async def search_articles_by_keyword(keyword: str, from_index: int = 0, limit: int = 25) -> str:
    """Search knowledge-base articles by keyword.

    Args:
        keyword: Text to search for (required).
        from_index: Pagination start (default 0).
        limit: Max results, 1-100 (default 25).
    """
    ctx: AppContext = mcp.get_context().request_context.lifespan_context
    params = {"searchStr": keyword, "from": from_index, "limit": min(limit, 100)}
    return _json(await ctx.desk.search_articles(params))


# ---------------------------------------------------------------------------
# THREADS, COMMENTS & ATTACHMENTS
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_ticket_threads(ticket_id: str, from_index: int = 0, limit: int = 25) -> str:
    """List all email threads/replies on a ticket. Returns thread summaries
    (not full content). Each thread includes hasAttach (boolean) and
    attachmentCount. To get the full thread content and its attachments array,
    call get_thread_by_id with the thread ID.

    Args:
        ticket_id: The ticket ID.
        from_index: Pagination start (default 0).
        limit: Max results, 1-100 (default 25).
    """
    ctx: AppContext = mcp.get_context().request_context.lifespan_context
    params = {"from": from_index, "limit": min(limit, 100)}
    return _json(await ctx.desk.get_ticket_threads(ticket_id, params))


@mcp.tool()
async def get_thread_by_id(ticket_id: str, thread_id: str, include_plain_text: bool = False) -> str:
    """Get the full details of a single thread, including its HTML content and
    its attachments array. This is the ONLY way to retrieve thread attachments —
    the Zoho Desk API embeds them inline in the thread response as an
    'attachments' list, where each item has id, name, size, and href (the
    download URL). Use this tool whenever you need to:
      - Read the full body of an email thread
      - Find attachments on a thread (check hasAttach in get_ticket_threads first)
      - Get attachment IDs so you can download them with download_thread_attachment

    Args:
        ticket_id: The ticket ID.
        thread_id: The thread ID (from get_ticket_threads).
        include_plain_text: If True, also returns plainText version of content.
    """
    ctx: AppContext = mcp.get_context().request_context.lifespan_context
    params = {}
    if include_plain_text:
        params["include"] = "plainText"
    return _json(await ctx.desk.get_thread(ticket_id, thread_id, params or None))


@mcp.tool()
async def get_thread_original_content(ticket_id: str, thread_id: str, inline: bool = False) -> str:
    """Get the original raw email content of a thread, including full mail
    headers (From, To, CC, MIME boundaries, etc.). Useful for debugging email
    delivery issues or extracting raw MIME parts.

    Args:
        ticket_id: The ticket ID.
        thread_id: The thread ID.
        inline: If True, inline attachments are rendered in the content.
    """
    ctx: AppContext = mcp.get_context().request_context.lifespan_context
    params = {}
    if inline:
        params["inline"] = "true"
    return _json(await ctx.desk.get_thread_original_content(ticket_id, thread_id, params or None))


@mcp.tool()
async def get_ticket_comments(ticket_id: str, from_index: int = 0, limit: int = 25) -> str:
    """List all internal/public comments on a ticket. Returns comment summaries.
    To get full comment details including its attachments array, call
    get_comment_by_id with a comment ID from this list.

    Args:
        ticket_id: The ticket ID.
        from_index: Pagination start (default 0).
        limit: Max results, 1-100 (default 25).
    """
    ctx: AppContext = mcp.get_context().request_context.lifespan_context
    params = {"from": from_index, "limit": min(limit, 100)}
    return _json(await ctx.desk.get_ticket_comments(ticket_id, params))


@mcp.tool()
async def get_comment_by_id(ticket_id: str, comment_id: str, include_mentions: bool = False) -> str:
    """Get the full details of a single comment, including its content and
    its attachments array. This is the ONLY way to retrieve comment attachments —
    the Zoho Desk API embeds them inline in the comment response as an
    'attachments' list, where each item has id, name, size, and href (the
    download URL). Use this tool whenever you need to:
      - Read the full body of a comment
      - Find attachments on a comment
      - Get attachment IDs so you can download them with download_comment_attachment

    Args:
        ticket_id: The ticket ID.
        comment_id: The comment ID (from get_ticket_comments).
        include_mentions: If True, also returns mention details.
    """
    ctx: AppContext = mcp.get_context().request_context.lifespan_context
    params = {}
    if include_mentions:
        params["include"] = "mentions,plainText"
    else:
        params["include"] = "plainText"
    return _json(await ctx.desk.get_comment(ticket_id, comment_id, params))


@mcp.tool()
async def get_ticket_attachments(ticket_id: str, from_index: int = 0, limit: int = 50) -> str:
    """List files uploaded directly on a ticket (via the Desk UI attachment field).
    These are SEPARATE from email/thread attachments and comment attachments.

    IMPORTANT — to find attachments sent via email:
      1. Call get_ticket_threads → find threads with hasAttach=true
      2. Call get_thread_by_id with that thread_id → read the 'attachments' array
      3. Call download_thread_attachment with the attachment ID from step 2

    Each returned attachment has id, name, size, createdTime, isPublic, and
    href (the download URL ending in /content).

    Args:
        ticket_id: The ticket ID.
        from_index: Pagination start (default 0).
        limit: Max results, 1-100 (default 50).
    """
    ctx: AppContext = mcp.get_context().request_context.lifespan_context
    params = {"from": from_index, "limit": min(limit, 100), "include": "creator"}
    return _json(await ctx.desk.list_ticket_attachments(ticket_id, params))


@mcp.tool()
async def download_thread_attachment(
    ticket_id: str, thread_id: str, attachment_id: str
) -> str:
    """Download the binary content of an email thread attachment. Returns the file
    as base64-encoded data with metadata (filename, content type, size in bytes).

    How to get the attachment_id:
      1. Call get_ticket_threads → find threads with hasAttach=true
      2. Call get_thread_by_id with that thread_id
      3. Read the 'attachments' array → each item has 'id', 'name', 'size', 'href'
      4. Pass the 'id' value here as attachment_id

    Args:
        ticket_id: The ticket ID.
        thread_id: The thread ID containing the attachment.
        attachment_id: The attachment ID from the thread's attachments array.
    """
    ctx: AppContext = mcp.get_context().request_context.lifespan_context
    resp = await ctx.desk.download_thread_attachment(ticket_id, thread_id, attachment_id)

    content_type = resp.headers.get("content-type", "application/octet-stream")
    content_disp = resp.headers.get("content-disposition", "")
    filename = _parse_filename(content_disp)

    raw_bytes = resp.content
    encoded = base64.b64encode(raw_bytes).decode("ascii")

    return _json({
        "filename": filename,
        "contentType": content_type,
        "sizeBytes": len(raw_bytes),
        "base64Content": encoded,
    })


@mcp.tool()
async def download_comment_attachment(
    ticket_id: str, comment_id: str, attachment_id: str
) -> str:
    """Download the binary content of a comment attachment. Returns the file
    as base64-encoded data with metadata (filename, content type, size in bytes).

    How to get the attachment_id:
      1. Call get_ticket_comments → get comment IDs
      2. Call get_comment_by_id with a comment_id
      3. Read the 'attachments' array → each item has 'id', 'name', 'size', 'href'
      4. Pass the 'id' value here as attachment_id

    Args:
        ticket_id: The ticket ID.
        comment_id: The comment ID containing the attachment.
        attachment_id: The attachment ID from the comment's attachments array.
    """
    ctx: AppContext = mcp.get_context().request_context.lifespan_context
    resp = await ctx.desk.download_comment_attachment(ticket_id, comment_id, attachment_id)

    content_type = resp.headers.get("content-type", "application/octet-stream")
    content_disp = resp.headers.get("content-disposition", "")
    filename = _parse_filename(content_disp)

    raw_bytes = resp.content
    encoded = base64.b64encode(raw_bytes).decode("ascii")

    return _json({
        "filename": filename,
        "contentType": content_type,
        "sizeBytes": len(raw_bytes),
        "base64Content": encoded,
    })


@mcp.tool()
async def download_ticket_attachment(
    ticket_id: str, attachment_id: str
) -> str:
    """Download the binary content of a ticket-level attachment (uploaded directly
    on the ticket, not from a thread or comment). Returns the file as
    base64-encoded data with metadata (filename, content type, size in bytes).

    How to get the attachment_id:
      1. Call get_ticket_attachments → each item has 'id', 'name', 'size', 'href'
      2. Pass the 'id' value here as attachment_id

    Args:
        ticket_id: The ticket ID.
        attachment_id: The attachment ID from get_ticket_attachments.
    """
    ctx: AppContext = mcp.get_context().request_context.lifespan_context
    resp = await ctx.desk.download_ticket_attachment(ticket_id, attachment_id)

    content_type = resp.headers.get("content-type", "application/octet-stream")
    content_disp = resp.headers.get("content-disposition", "")
    filename = _parse_filename(content_disp)

    raw_bytes = resp.content
    encoded = base64.b64encode(raw_bytes).decode("ascii")

    return _json({
        "filename": filename,
        "contentType": content_type,
        "sizeBytes": len(raw_bytes),
        "base64Content": encoded,
    })


# =============================================================================
# ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    mcp.run(transport="stdio")
