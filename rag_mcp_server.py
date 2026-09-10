"""
RAG Knowledge Base MCP Server — stdio Transport
================================================
A Model Context Protocol (MCP) server that provides retrieval-augmented generation
(RAG) tools over a ChromaDB vector store. Supports searching, ingesting new documents,
and listing categories. Used by VS Code's own mcp.json (in-editor Copilot Chat).

All parsing/chunking/retrieval logic lives in rag_common.py, shared with the SSE
version (rag_mcp_server_sse.py, used by the LangGraph agent runtime) since both
read/write the same on-disk Chroma store. This file only wires up the FastMCP
stdio transport + lifespan.

Usage:
    python rag_mcp_server.py

IMPORTANT: If you switch embedding models, you MUST re-embed all documents
(ingest_documents with clear_existing=True) because different models
produce vectors with incompatible dimensions and semantics.
"""

import sys
import logging
from pathlib import Path
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent / "agent" / ".env")

from mcp.server.fastmcp import FastMCP

from rag_common import (
    AppContext,
    create_app_context,
    ingest_documents_impl,
    list_categories_impl,
    search_knowledge_base_impl,
)

# Configure logging (NEVER use print() in stdio MCP servers — it corrupts JSON-RPC)
logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger("rag_mcp")


# =============================================================================
# MCP SERVER LIFESPAN — shared resources (vector store, embeddings, reranker)
# =============================================================================

@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
    """Initialize ChromaDB, embeddings, and reranker for the MCP server lifetime."""
    ctx = await create_app_context()
    logger.info("RAG MCP server initialized.")
    yield ctx
    logger.info("RAG MCP server shut down.")


# =============================================================================
# MCP SERVER & TOOL DEFINITIONS
# =============================================================================

mcp = FastMCP("RAG Knowledge Base", lifespan=app_lifespan)


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
# ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    mcp.run(transport="stdio")
