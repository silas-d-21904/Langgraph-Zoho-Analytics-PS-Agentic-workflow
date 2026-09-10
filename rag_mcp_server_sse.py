"""
RAG Knowledge Base MCP Server — SSE (HTTP) Transport
=====================================================
A persistent HTTP version of the RAG MCP server that keeps the embedding model
(and reranker) loaded in memory. The agent connects via SSE URL instead of
spawning a subprocess, so the embedding model loads exactly once — surviving
agent restarts and hot-reloads.

All parsing/chunking/retrieval logic lives in rag_common.py, shared with the
stdio version (rag_mcp_server.py) since both read/write the same on-disk
Chroma store. This file only wires up the FastMCP transport + lifespan.

Usage:
    python rag_mcp_server_sse.py                         # defaults: port 8100
    RAG_PORT=9000 python rag_mcp_server_sse.py           # custom port

Configure embedding provider / model / reranker via environment variables
(same as stdio version).
"""

import os
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

# Load agent/.env so embedding config matches the LangGraph server
_AGENT_ENV = Path(__file__).resolve().parent / "agent" / ".env"
if _AGENT_ENV.exists():
    load_dotenv(_AGENT_ENV)

from mcp.server.fastmcp import FastMCP

from rag_common import (
    AppContext,
    create_app_context,
    ingest_documents_impl,
    list_categories_impl,
    search_knowledge_base_impl,
)

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("rag_mcp_sse")


# =============================================================================
# MCP SERVER LIFESPAN — shared resources (vector store, embeddings, reranker)
# =============================================================================

@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
    """Initialize ChromaDB, embeddings, and reranker for the MCP server lifetime."""
    ctx = await create_app_context()
    logger.info("RAG MCP server (SSE) initialized and ready.")
    yield ctx
    logger.info("RAG MCP server (SSE) shut down.")


# =============================================================================
# MCP SERVER & TOOL DEFINITIONS
# =============================================================================

# Read host/port early — FastMCP.run() does NOT accept these kwargs;
# they must be passed to the constructor where Settings stores them.
_RAG_HOST = os.environ.get("RAG_HOST", "0.0.0.0")
_RAG_PORT = int(os.environ.get("RAG_PORT", "8100"))

mcp = FastMCP("RAG Knowledge Base", lifespan=app_lifespan, host=_RAG_HOST, port=_RAG_PORT)


# ---------------------------------------------------------------------------
# SEARCH
# ---------------------------------------------------------------------------

@mcp.tool()
async def search_knowledge_base(
    query: str,
    k: int = 5,
    category: str = "",
    min_score: float = 0.0,
) -> str:
    """Search the local knowledge base for relevant documents using semantic similarity,
    with cross-encoder reranking for improved precision.

    Returns the top-k most relevant document chunks with their content, metadata
    (category, title, heading_path), and a relevance score (0-1, higher is better).
    Use this to find information from ingested HTML documents about Zoho Analytics
    report building, financial reports, inventory tracking, etc.

    If no results clear min_score, an empty result set with a message is returned
    instead of forcing back weak/irrelevant matches — try rephrasing the query or
    lowering min_score rather than repeating the same call.

    Args:
        query: The search query text (natural language).
        k: Number of results to return (default 5, max 20). Use a custom value based on context of topic.
        category: Optional category to restrict the search to (see list_categories for valid values).
                  Leave empty to search all categories.
        min_score: Minimum relevance score (0-1) a result must meet to be returned. Default 0.0 (no threshold).
    """
    ctx: AppContext = mcp.get_context().request_context.lifespan_context
    return search_knowledge_base_impl(ctx, query, k=k, category=category or None, min_score=min_score)


# ---------------------------------------------------------------------------
# INGEST
# ---------------------------------------------------------------------------

@mcp.tool()
async def ingest_documents(
    directory: str = "",
    clear_existing: bool = False,
) -> str:
    """Ingest HTML documents from a directory into the knowledge base.

    Scans the given directory for subfolders (treated as categories), finds all .html
    files in each subfolder, parses them with BeautifulSoup, and embeds + stores them
    in ChromaDB using structure-aware chunking (tables and headings are never split
    mid-content, and every chunk is prefixed with a heading breadcrumb).

    IMPORTANT: If you changed the embedding model, set clear_existing=True to rebuild
    the entire vector store (different models produce incompatible vectors). Also set
    clear_existing=True after any chunking-logic change to avoid mixing old and new
    chunk boundaries in the same collection.

    Args:
        directory: Path to the documents directory. Each subfolder is treated as a category.
                   Defaults to the DOCS_DIR environment variable (usually ./zoho-finance-1/templates).
        clear_existing: If True, deletes ALL existing documents before ingesting.
                        Set to True when switching embedding models. Default False.
    """
    ctx: AppContext = mcp.get_context().request_context.lifespan_context
    return ingest_documents_impl(ctx, directory=directory, clear_existing=clear_existing)


# ---------------------------------------------------------------------------
# LIST CATEGORIES
# ---------------------------------------------------------------------------

@mcp.tool()
async def list_categories() -> str:
    """List all unique document categories currently in the knowledge base.

    Categories correspond to subfolder names from the ingested documents directory
    (e.g. 'mis-reports', 'fifo-inventory-tracking', 'account-payables').
    """
    ctx: AppContext = mcp.get_context().request_context.lifespan_context
    return list_categories_impl(ctx)


# =============================================================================
# ENTRY POINT — SSE (HTTP) transport
# =============================================================================

if __name__ == "__main__":
    logger.info(f"Starting RAG MCP server on http://{_RAG_HOST}:{_RAG_PORT}/sse")
    mcp.run(transport="sse")

