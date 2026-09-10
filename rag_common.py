"""
RAG Knowledge Base — Shared Core Logic
========================================
Shared parsing, chunking, embedding, reranking, and retrieval logic used by both
MCP server entry points:

  - rag_mcp_server_sse.py   (SSE/HTTP transport, used by the LangGraph agent at runtime)
  - rag_mcp_server.py       (stdio transport, used by VS Code's own mcp.json)

Both processes read/write the SAME on-disk Chroma store (CHROMA_PERSIST_DIR, default
"./chroma_db" resolved relative to the process cwd — both launchers cd to the project
root first). Keeping the parsing/chunking/retrieval logic in one module avoids the two
servers drifting out of sync with each other or with what's actually persisted in the DB.

NOTE: rag_mcp_portable/rag_mcp_server.py is a separate, self-contained distributable
copy with its own chroma_db and is intentionally NOT wired to this module.
"""

import os
import json
import logging
import math
import re as _re
from dataclasses import dataclass
from typing import Any, Optional

from bs4 import BeautifulSoup, NavigableString, Tag
from langchain_core.documents import Document

logger = logging.getLogger("rag_common")


# =============================================================================
# STRUCTURED HTML -> BLOCKS PARSER
# =============================================================================
# BeautifulSoup's get_text(separator="\n") breaks inline <span> tags onto
# separate lines and completely flattens <table> data. This walks the DOM tree
# and emits an ORDERED LIST OF BLOCKS (heading / table / text / code) rather than
# a flat string, so the chunker downstream can respect structural boundaries
# (never split a table mid-row, always keep a heading breadcrumb attached).
# =============================================================================

# Tags whose children should be concatenated *inline* (no newline between them)
_INLINE_TAGS = frozenset({
    "span", "a", "strong", "b", "em", "i", "u", "s", "sub", "sup",
    "code", "mark", "small", "abbr", "cite", "q", "kbd", "var",
    "font", "label",
})


def _collect_inline_text(tag: Tag) -> str:
    """Recursively collect text from an element, joining inline children
    with a space instead of a newline."""
    parts: list[str] = []
    for child in tag.children:
        if isinstance(child, NavigableString):
            txt = child.get_text()
            txt = " ".join(txt.split())
            if txt:
                parts.append(txt)
        elif isinstance(child, Tag):
            if child.name == "br":
                parts.append("\n")
            elif child.name in _INLINE_TAGS:
                inner = _collect_inline_text(child)
                if inner:
                    parts.append(inner)
            else:
                inner = _collect_inline_text(child)
                if inner:
                    parts.append(inner)
    return " ".join(parts)


def _parse_table(table_tag: Tag) -> str:
    """Convert an HTML <table> to a Markdown pipe-delimited table."""
    rows: list[list[str]] = []
    for tr in table_tag.find_all("tr"):
        cells: list[str] = []
        for td in tr.find_all(["td", "th"]):
            cell_text = _collect_inline_text(td).replace("|", "/").strip()
            cell_text = " ".join(cell_text.split())
            cells.append(cell_text)
        if cells:
            rows.append(cells)

    if not rows:
        return ""

    max_cols = max(len(r) for r in rows)
    for r in rows:
        while len(r) < max_cols:
            r.append("")

    lines: list[str] = []
    lines.append("| " + " | ".join(rows[0]) + " |")
    lines.append("| " + " | ".join("---" for _ in rows[0]) + " |")
    for row in rows[1:]:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _render_list(list_tag: Tag, out_lines: list[str], indent: int = 0) -> None:
    """Render a (possibly nested) <ol>/<ul> into indented markdown list lines."""
    is_ordered = list_tag.name == "ol"
    counter = int(list_tag.get("start", 1))
    pad = "  " * indent
    for li in list_tag.find_all("li", recursive=False):
        li_text = _collect_inline_text(li).strip()
        if li_text:
            prefix = f"{counter}. " if is_ordered else "- "
            out_lines.append(pad + prefix + li_text)
            counter += 1
        for nested in li.find_all(["ol", "ul"], recursive=False):
            _render_list(nested, out_lines, indent=indent + 1)


def html_to_structured_blocks(soup: BeautifulSoup) -> list[dict]:
    """Walk a parsed BeautifulSoup tree and return an ordered list of blocks:

        {"type": "heading", "level": 1-6, "text": str}
        {"type": "table", "level": None, "text": str}   # markdown table
        {"type": "text", "level": None, "text": str}    # paragraph / list / blockquote
        {"type": "code", "level": None, "text": str}     # fenced code block

    This replaces the naive ``soup.get_text(separator='\\n')`` (and the older
    flat-string html_to_structured_text) so the chunker can respect structural
    boundaries instead of cutting blindly on character count.
    """
    body = soup.find("body") or soup
    blocks: list[dict] = []

    def _walk(element: Tag) -> None:  # noqa: C901 - complexity acceptable for a tree walk
        for child in element.children:
            if isinstance(child, NavigableString):
                txt = child.get_text().strip()
                if txt:
                    blocks.append({"type": "text", "level": None, "text": txt})
                continue

            if not isinstance(child, Tag):
                continue

            tag = child.name

            if tag == "table":
                md_table = _parse_table(child)
                if md_table:
                    blocks.append({"type": "table", "level": None, "text": md_table})
                continue

            if _re.match(r"^h[1-6]$", tag):
                text = _collect_inline_text(child).strip()
                if text:
                    blocks.append({"type": "heading", "level": int(tag[1]), "text": text})
                continue

            if tag in ("ol", "ul"):
                list_lines: list[str] = []
                _render_list(child, list_lines, indent=0)
                if list_lines:
                    blocks.append({"type": "text", "level": None, "text": "\n".join(list_lines)})
                continue

            if tag in ("p", "blockquote"):
                text = _collect_inline_text(child).strip()
                if text:
                    blocks.append({"type": "text", "level": None, "text": text})
                continue

            if tag in ("pre", "code"):
                code_text = child.get_text()
                if code_text.strip():
                    blocks.append({"type": "code", "level": None, "text": "```\n" + code_text.strip() + "\n```"})
                continue

            if tag in ("style", "script", "img", "link", "meta", "noscript"):
                continue

            # Generic container (div, section, etc.) - recurse
            _walk(child)

    _walk(body)
    return blocks


# =============================================================================
# STRUCTURE-AWARE CHUNKER
# =============================================================================

def _split_large_table(table_text: str, max_chars: int) -> list[str]:
    """Split an oversized markdown table into row-groups, repeating the header
    row + separator row in each group so no group loses column context. Never
    splits a row itself."""
    lines = table_text.split("\n")
    if len(lines) < 3:
        return [table_text]

    header, sep, body_rows = lines[0], lines[1], lines[2:]
    groups: list[str] = []
    current = [header, sep]
    current_len = len(header) + len(sep)

    for row in body_rows:
        if current_len + len(row) > max_chars and len(current) > 2:
            groups.append("\n".join(current))
            current = [header, sep]
            current_len = len(header) + len(sep)
        current.append(row)
        current_len += len(row)

    if len(current) > 2:
        groups.append("\n".join(current))

    return groups if groups else [table_text]


def chunk_blocks(
    blocks: list[dict],
    file: str,
    category: str,
    chunk_size: int = 1000,
    chunk_overlap: int = 200,
    max_table_chars: int = 3000,
) -> list[dict]:
    """Turn an ordered list of blocks into chunks that respect structural
    boundaries: tables are never split mid-row, and every chunk is prefixed
    with a heading breadcrumb so it's understandable out of context.

    Returns a list of {"text": <chunk text incl. breadcrumb prefix>,
                        "heading_path": <breadcrumb string, no file/category>}.
    """
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    splitter = RecursiveCharacterTextSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)

    chunks: list[dict] = []
    heading_stack: list[tuple[int, str]] = []
    buffer_parts: list[str] = []

    def breadcrumb() -> str:
        return " > ".join(h[1] for h in heading_stack)

    def full_prefix() -> str:
        crumb = breadcrumb()
        return f"{file} > {category} > {crumb}" if crumb else f"{file} > {category}"

    def flush_buffer() -> None:
        nonlocal buffer_parts
        if not buffer_parts:
            return
        text = "\n\n".join(buffer_parts).strip()
        buffer_parts = []
        if not text:
            return
        prefix = full_prefix()
        crumb = breadcrumb()
        for piece in splitter.split_text(text):
            piece = piece.strip()
            if piece:
                chunks.append({"text": f"[{prefix}]\n{piece}", "heading_path": crumb})

    for block in blocks:
        btype = block["type"]

        if btype == "heading":
            flush_buffer()
            level = block["level"]
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, block["text"]))

        elif btype == "table":
            flush_buffer()
            prefix = full_prefix()
            crumb = breadcrumb()
            table_text = block["text"]
            if len(table_text) <= max_table_chars:
                chunks.append({"text": f"[{prefix}]\n{table_text}", "heading_path": crumb})
            else:
                for group in _split_large_table(table_text, max_table_chars):
                    chunks.append({"text": f"[{prefix}]\n{group}", "heading_path": crumb})

        else:  # "text" / "code"
            buffer_parts.append(block["text"])

    flush_buffer()
    return chunks


# =============================================================================
# EMBEDDING MODEL FACTORY
# =============================================================================

def get_embeddings(provider: str, model: str, api_key: str = ""):
    """Factory function that returns a LangChain embeddings instance based on the provider."""

    if provider == "google":
        from langchain_google_genai import GoogleGenerativeAIEmbeddings
        return GoogleGenerativeAIEmbeddings(model=model, google_api_key=api_key)

    elif provider == "openai":
        from langchain_openai import OpenAIEmbeddings
        return OpenAIEmbeddings(model=model, openai_api_key=api_key)

    elif provider == "cohere":
        from langchain_cohere import CohereEmbeddings
        return CohereEmbeddings(model=model, cohere_api_key=api_key)

    elif provider == "huggingface":
        from langchain_huggingface import HuggingFaceEmbeddings
        from sentence_transformers import SentenceTransformer

        logger.info(f"Ensuring local embedding model is available: {model}")
        try:
            SentenceTransformer(model_name_or_path=model)
        except Exception as exc:
            raise RuntimeError(
                "Failed to load or download the local HuggingFace embedding model "
                f"'{model}'. Verify the model name and internet access."
            ) from exc

        return HuggingFaceEmbeddings(model_name=model)

    else:
        raise ValueError(
            f"Unknown embedding provider: '{provider}'. "
            f"Supported: google, openai, cohere, huggingface"
        )


# =============================================================================
# RERANKER FACTORY
# =============================================================================

def get_reranker(model_name: str):
    """Load a local cross-encoder reranker. Returns None (reranking disabled)
    if the model can't be loaded, so the server still boots without it."""
    try:
        from sentence_transformers import CrossEncoder
        logger.info(f"Loading reranker model: {model_name}")
        return CrossEncoder(model_name)
    except Exception as exc:
        logger.warning(
            f"Failed to load reranker model '{model_name}': {exc}. "
            "Reranking will be disabled; falling back to raw vector scores."
        )
        return None


def _sigmoid(x: float) -> float:
    try:
        return 1.0 / (1.0 + math.exp(-x))
    except OverflowError:
        return 0.0 if x < 0 else 1.0


# =============================================================================
# APP CONTEXT
# =============================================================================

@dataclass
class AppContext:
    vector_db: Any  # Chroma instance
    embeddings: Any  # Embeddings instance
    persist_dir: str
    docs_dir: str
    reranker: Any = None  # CrossEncoder instance, or None if disabled


async def create_app_context() -> AppContext:
    """Build the shared vector store / embeddings / reranker used by both MCP
    server entry points. Each entry point wraps this in its own
    @asynccontextmanager lifespan (for its own logger/startup messages) and
    calls this to do the actual work."""
    from langchain_chroma import Chroma

    provider = os.environ.get("EMBEDDING_PROVIDER", "huggingface")
    model = os.environ.get("EMBEDDING_MODEL", "BAAI/bge-base-en-v1.5")
    api_key = os.environ.get("EMBEDDING_API_KEY", "")
    persist_dir = os.environ.get("CHROMA_PERSIST_DIR", "./chroma_db")
    docs_dir = os.environ.get("DOCS_DIR", "./zoho-finance-1/templates")
    reranker_model = os.environ.get("RERANKER_MODEL", "BAAI/bge-reranker-base")

    logger.info(f"Initializing embeddings: provider={provider}, model={model}")
    embeddings = get_embeddings(provider, model, api_key)

    logger.info(f"Loading/creating ChromaDB at {persist_dir}")
    vector_db = Chroma(persist_directory=persist_dir, embedding_function=embeddings)

    reranker = get_reranker(reranker_model)

    return AppContext(
        vector_db=vector_db,
        embeddings=embeddings,
        persist_dir=persist_dir,
        docs_dir=docs_dir,
        reranker=reranker,
    )


# =============================================================================
# CATEGORY HELPERS
# =============================================================================

def _get_categories(ctx: AppContext) -> set:
    try:
        collection = ctx.vector_db._collection
        result = collection.get(include=["metadatas"])
        cats = set()
        if result and result.get("metadatas"):
            for md in result["metadatas"]:
                if md and "category" in md:
                    cats.add(md["category"])
        return cats
    except Exception:
        return set()


def list_categories_impl(ctx: AppContext) -> str:
    """List all unique document categories currently in the knowledge base."""
    try:
        collection = ctx.vector_db._collection
        result = collection.get(include=["metadatas"])

        if not result or not result.get("metadatas"):
            return json.dumps({"categories": [], "message": "No documents in the knowledge base."})

        categories = set()
        for metadata in result["metadatas"]:
            if metadata and "category" in metadata:
                categories.add(metadata["category"])

        category_list = sorted(categories)
        return json.dumps({
            "categories": category_list,
            "total_categories": len(category_list),
            "total_documents": len(result["metadatas"]),
        }, indent=2)

    except Exception as e:
        return json.dumps({"error": str(e), "categories": []})


# =============================================================================
# SEARCH
# =============================================================================

def search_knowledge_base_impl(
    ctx: AppContext,
    query: str,
    k: int = 5,
    category: Optional[str] = None,
    min_score: float = 0.0,
) -> str:
    """Search the knowledge base: wide vector-similarity recall -> optional
    category filter -> cross-encoder rerank -> min_score threshold -> top-k.

    Returns JSON with category/title/heading_path/content/score per result, or
    an explicit empty result + message if nothing clears the relevance bar
    (rather than always forcing back k weak matches).
    """
    k = max(1, min(k, 20))

    if category:
        valid_categories = _get_categories(ctx)
        if valid_categories and category not in valid_categories:
            return json.dumps({
                "error": f"Unknown category '{category}'.",
                "valid_categories": sorted(valid_categories),
            })

    filter_dict = {"category": category} if category else None
    candidate_k = max(k * 4, 20)

    try:
        candidates = ctx.vector_db.similarity_search_with_relevance_scores(
            query, k=candidate_k, filter=filter_dict
        )
    except Exception as exc:
        logger.warning(
            f"similarity_search_with_relevance_scores failed ({exc}); "
            "falling back to similarity_search_with_score"
        )
        candidates = ctx.vector_db.similarity_search_with_score(query, k=candidate_k, filter=filter_dict)

    if not candidates:
        return json.dumps({"results": [], "message": "No matching documents found."})

    if ctx.reranker is not None:
        pairs = [(query, doc.page_content) for doc, _ in candidates]
        try:
            raw_scores = ctx.reranker.predict(pairs)
            scored = [
                (doc, _sigmoid(float(raw)))
                for (doc, _vec_score), raw in zip(candidates, raw_scores)
            ]
        except Exception as exc:
            logger.warning(f"Reranking failed ({exc}); falling back to vector scores")
            scored = [(doc, float(vec_score)) for doc, vec_score in candidates]
    else:
        scored = [(doc, float(vec_score)) for doc, vec_score in candidates]

    scored.sort(key=lambda pair: pair[1], reverse=True)

    results = []
    for doc, score in scored:
        if score < min_score:
            continue
        results.append({
            "category": doc.metadata.get("category", "unknown"),
            "title": doc.metadata.get("title", "unknown"),
            "heading_path": doc.metadata.get("heading_path", ""),
            "content": doc.page_content,
            "score": round(score, 4),
        })
        if len(results) >= k:
            break

    if not results:
        return json.dumps({
            "results": [],
            "message": f"No documents met the minimum relevance score ({min_score}).",
        })

    return json.dumps({"results": results, "total": len(results)}, indent=2, default=str, ensure_ascii=False)


# =============================================================================
# INGEST
# =============================================================================

def ingest_documents_impl(
    ctx: AppContext,
    directory: str = "",
    clear_existing: bool = False,
) -> str:
    """Ingest HTML documents from a directory into the knowledge base using
    structure-aware chunking (tables/headings kept intact, breadcrumb-prefixed
    chunks)."""
    from langchain_chroma import Chroma

    docs_dir = directory or ctx.docs_dir

    if not os.path.isdir(docs_dir):
        return json.dumps({"error": f"Directory not found: {docs_dir}"})

    if clear_existing:
        logger.info("Clearing existing ChromaDB collection...")
        try:
            ctx.vector_db.delete_collection()
            ctx.vector_db = Chroma(
                persist_directory=ctx.persist_dir,
                embedding_function=ctx.embeddings,
            )
            logger.info("ChromaDB collection cleared.")
        except Exception as e:
            logger.error(f"Failed to clear collection: {e}")

    all_docs = []

    for root, dirs, files in os.walk(docs_dir):
        for file in files:
            if not file.endswith(".html"):
                continue
            path = os.path.join(root, file)

            rel_path = os.path.relpath(root, docs_dir)
            category = rel_path if rel_path != "." else "root"

            folder_names_list = rel_path.split(os.sep) if rel_path != "." else ["root"]
            folders_meta_str = ", ".join(folder_names_list)

            try:
                with open(path, "r", encoding="utf-8") as f:
                    soup = BeautifulSoup(f, "html.parser")
                    blocks = html_to_structured_blocks(soup)

                if blocks:
                    chunks = chunk_blocks(blocks, file=file, category=category)
                    for chunk in chunks:
                        all_docs.append(Document(
                            page_content=chunk["text"],
                            metadata={
                                "category": category,
                                "title": file,
                                "folder_names": folders_meta_str,
                                "heading_path": chunk["heading_path"],
                            },
                        ))
                    logger.info(f"Parsed: {category}/{file} into {len(chunks)} chunks")
            except Exception as e:
                logger.warning(f"Failed to parse {path}: {e}")

    if not all_docs:
        return json.dumps({"error": "No HTML documents found in the directory.", "directory": docs_dir})

    logger.info(f"Embedding {len(all_docs)} documents...")
    ctx.vector_db.add_documents(all_docs)
    logger.info("Ingestion complete.")

    categories = {}
    for doc in all_docs:
        cat = doc.metadata["category"]
        categories[cat] = categories.get(cat, 0) + 1

    return json.dumps({
        "status": "success",
        "total_documents": len(all_docs),
        "categories": categories,
        "directory": docs_dir,
        "clear_existing": clear_existing,
    }, indent=2)
