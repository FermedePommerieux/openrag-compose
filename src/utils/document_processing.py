import os
from collections import defaultdict

from utils.logging_config import get_logger

logger = get_logger(__name__)

HYBRID_CHUNKING_SCHEMA_VERSION = 2


class HybridChunkingError(RuntimeError):
    """Raised when an explicitly requested Docling hybrid chunk cannot be produced."""


def _structural_supplement_chunks(
    doc_dict: dict,
    *,
    covered_refs: set[str],
    max_tokens: int,
) -> list[dict]:
    """Preserve Docling text items omitted by HybridChunker.

    Docling deliberately classifies page headers and footers as document
    furniture, which HybridChunker may omit from semantic body chunks. Those
    regions often contain identity, legal, navigation, or payment evidence.
    Emit only uncovered items and retain their page, region label, and spatial
    position so downstream retrieval can verify relationships without relying
    on mention order.
    """
    import tiktoken

    tokenizer = tiktoken.get_encoding("cl100k_base")
    token_limit = max(1, int(max_tokens))
    grouped: dict[tuple[int, str], list[dict]] = defaultdict(list)

    for index, item in enumerate(doc_dict.get("texts", [])):
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        item_ref = str(item.get("self_ref") or f"#/texts/{index}")
        if item_ref in covered_refs:
            continue

        provenance = item.get("prov") or []
        first_prov = provenance[0] if provenance and isinstance(provenance[0], dict) else {}
        try:
            page = int(first_prov.get("page_no") or 1)
        except (TypeError, ValueError):
            page = 1
        label = str(item.get("label") or "text")
        bbox = first_prov.get("bbox") if isinstance(first_prov.get("bbox"), dict) else {}
        grouped[(page, label)].append(
            {
                "index": index,
                "text": text,
                "left": bbox.get("l"),
                "top": bbox.get("t"),
            }
        )

    supplements: list[dict] = []
    for (page, label), items in sorted(grouped.items()):
        # Reading order inside repeated page furniture is spatial, not the
        # serialization order returned by every converter version.
        items.sort(
            key=lambda item: (
                -(float(item["top"]) if item["top"] is not None else 0.0),
                float(item["left"]) if item["left"] is not None else 0.0,
                item["index"],
            )
        )
        header = f"[Document structural region]\nPage: {page}\nRegion: {label}"
        header_tokens = len(tokenizer.encode(header))
        available = max(1, token_limit - header_tokens - 2)
        current_records: list[str] = []
        current_tokens = 0

        def flush(*, page: int = page, label: str = label, header: str = header) -> None:
            nonlocal current_records, current_tokens
            if not current_records:
                return
            supplements.append(
                {
                    "page": page,
                    "type": "docling_structural_supplement",
                    "labels": [label],
                    "text": f"{header}\n" + "\n".join(current_records),
                }
            )
            current_records = []
            current_tokens = 0

        for item in items:
            left = "unknown" if item["left"] is None else f"{float(item['left']):.1f}"
            top = "unknown" if item["top"] is None else f"{float(item['top']):.1f}"
            prefix = f"- Position(left={left}, top={top}); Text: "
            prefix_tokens = len(tokenizer.encode(prefix))
            text_tokens = tokenizer.encode(item["text"])
            text_limit = max(1, available - prefix_tokens)
            parts = [
                tokenizer.decode(text_tokens[start : start + text_limit])
                for start in range(0, len(text_tokens), text_limit)
            ] or [""]
            for part in parts:
                record = prefix + part
                record_tokens = len(tokenizer.encode(record))
                if current_records and current_tokens + record_tokens > available:
                    flush()
                current_records.append(record)
                current_tokens += record_tokens
        flush()

    return supplements


def process_text_file(file_path: str) -> dict:
    """
    Process a plain text file without using docling.
    Returns the same structure as extract_relevant() for consistency.

    Args:
        file_path: Path to the .txt file

    Returns:
        dict with keys: id, filename, mimetype, chunks
    """
    from utils.hash_utils import hash_id

    # Read the file
    with open(file_path, encoding="utf-8", errors="replace") as f:
        content = f.read()

    # Compute hash
    file_hash = hash_id(file_path)
    filename = os.path.basename(file_path)

    # Split content into chunks of ~1000 characters to match typical docling chunk sizes
    # This ensures embeddings stay within reasonable token limits
    chunk_size = 1000
    chunks = []

    # Split by paragraphs first (double newline)
    paragraphs = content.split("\n\n")
    current_chunk = ""
    chunk_index = 0

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        # If adding this paragraph would exceed chunk size, save current chunk
        if len(current_chunk) + len(para) + 2 > chunk_size and current_chunk:
            chunks.append(
                {
                    "page": chunk_index + 1,  # Use chunk_index + 1 as "page" number
                    "type": "text",
                    "text": current_chunk.strip(),
                }
            )
            chunk_index += 1
            current_chunk = para
        else:
            if current_chunk:
                current_chunk += "\n\n" + para
            else:
                current_chunk = para

    # Add the last chunk if any
    if current_chunk.strip():
        chunks.append({"page": chunk_index + 1, "type": "text", "text": current_chunk.strip()})

    # If no chunks were created (empty file), create a single empty chunk
    if not chunks:
        chunks.append({"page": 1, "type": "text", "text": ""})

    return {
        "id": file_hash,
        "filename": filename,
        "mimetype": "text/plain",
        "chunks": chunks,
    }


def extract_relevant(doc_dict: dict) -> dict:
    """
    Given the full export_to_dict() result:
      - Grabs origin metadata (hash, filename, mimetype)
      - Finds every text fragment in `texts`, groups them by page_no
      - Flattens tables in `tables` into tab-separated text, grouping by row
      - Concatenates each page's fragments and each table into its own chunk
    Returns a slimmed dict ready for indexing, with each chunk under "text".
    """
    origin = doc_dict.get("origin", {})
    chunks = []

    # 1) process free-text fragments
    page_texts = defaultdict(list)
    for txt in doc_dict.get("texts", []):
        prov = txt.get("prov", [])
        page_no = prov[0].get("page_no") if prov else None
        if page_no is None:
            page_no = 1
        page_texts[page_no].append(txt.get("text", "").strip())

    for page in sorted(page_texts):
        chunks.append({"page": page, "type": "text", "text": "\n".join(page_texts[page])})

    # 2) process tables
    for t_idx, table in enumerate(doc_dict.get("tables", [])):
        prov = table.get("prov", [])
        page_no = prov[0].get("page_no") if prov else None
        if page_no is None:
            page_no = 1

        # group cells by their row index
        rows = defaultdict(list)
        table_data = table.get("data")
        if table_data:
            for cell in table_data.get("table_cells", []):
                r = cell.get("start_row_offset_idx")
                c = cell.get("start_col_offset_idx")
                text = cell.get("text", "").strip()
                rows[r].append((c, text))

        # build a tab‑separated line for each row, in order
        flat_rows = []
        for r in sorted(rows):
            cells = [txt for _, txt in sorted(rows[r], key=lambda x: x[0])]
            flat_rows.append("\t".join(cells))

        chunks.append(
            {
                "page": page_no,
                "type": "table",
                "table_index": t_idx,
                "text": "\n".join(flat_rows),
            }
        )

    return {
        "id": origin.get("binary_hash"),
        "filename": origin.get("filename"),
        "mimetype": origin.get("mimetype"),
        "chunks": chunks,
    }


def resplit_chunks_character_windows(
    chunks: list,
    chunk_size: int,
    chunk_overlap: int,
) -> list:
    """Split long chunk texts into fixed-size character windows with overlap.

    Used by the non-Langflow ingestion path when the UI supplies ``chunkSize`` /
    ``chunkOverlap``. Invalid combinations (non-positive size or overlap >= size)
    return ``chunks`` unchanged.
    """
    if chunk_size <= 0 or chunk_overlap >= chunk_size:
        return chunks
    stride = chunk_size - chunk_overlap
    if stride <= 0:
        return chunks

    out: list = []
    for ch in chunks:
        text = ch.get("text") if isinstance(ch, dict) else None
        if not isinstance(text, str):
            out.append(dict(ch) if isinstance(ch, dict) else ch)
            continue
        if len(text) <= chunk_size:
            out.append(dict(ch))
            continue
        start = 0
        while start < len(text):
            end = min(start + chunk_size, len(text))
            piece = text[start:end]
            new_ch = dict(ch)
            new_ch["text"] = piece
            out.append(new_ch)
            if end >= len(text):
                break
            start += stride
    return out


def chunk_docling_hybrid(
    doc_dict: dict,
    *,
    max_tokens: int,
    merge_peers: bool,
) -> list[dict]:
    """Chunk a Docling JSON document with its structure-aware HybridChunker.

    Hybrid is an explicit operator choice.  A missing optional dependency or
    an incompatible document must therefore fail the upload before character
    chunks can be written under the wrong strategy.
    """
    try:
        import tiktoken
        from docling_core.transforms.chunker.hybrid_chunker import HybridChunker
        from docling_core.transforms.chunker.tokenizer.openai import OpenAITokenizer
        from docling_core.types.doc.document import DoclingDocument
    except ImportError as exc:
        raise HybridChunkingError(
            "Hybrid chunking was requested but docling-core[chunking-openai] is unavailable"
        ) from exc

    try:
        tokenizer = OpenAITokenizer(
            tokenizer=tiktoken.get_encoding("cl100k_base"),
            max_tokens=max(1, int(max_tokens)),
        )
        chunker = HybridChunker(tokenizer=tokenizer, merge_peers=merge_peers)
        document = DoclingDocument.model_validate(doc_dict)
        chunks: list[dict] = []
        covered_refs: set[str] = set()
        for chunk in chunker.chunk(dl_doc=document):
            page = 1
            for item in getattr(getattr(chunk, "meta", None), "doc_items", []) or []:
                item_ref = getattr(item, "self_ref", None)
                if item_ref is not None:
                    covered_refs.add(str(item_ref))
                provenance = getattr(item, "prov", None) or []
                if provenance:
                    page = getattr(provenance[0], "page_no", None) or page
                    break
            text = chunker.contextualize(chunk=chunk).strip()
            if text:
                chunks.append({"page": page, "type": "docling_hybrid", "text": text})
        if not chunks:
            raise HybridChunkingError("Hybrid chunking produced no usable chunks")
        supplements = _structural_supplement_chunks(
            doc_dict,
            covered_refs=covered_refs,
            max_tokens=max_tokens,
        )
        if supplements:
            logger.info(
                "Preserving Docling structural regions omitted by HybridChunker",
                supplement_chunks=len(supplements),
            )
            chunks.extend(supplements)
        return chunks
    except Exception as exc:
        logger.warning("Docling HybridChunker failed", error=str(exc))
        raise HybridChunkingError(f"Hybrid chunking failed: {exc}") from exc


def split_chunks_by_max_tokens(
    chunks: list[dict], max_tokens: int, model: str | None = None
) -> list[dict]:
    """Split any chunks whose token count exceeds max_tokens.

    Ensures that when chunk texts are sent to the embedding model, they don't get
    further split by token limits in the batching helper, allowing the final
    embeddings and chunks to align 1-to-1.
    """
    import tiktoken

    from config.settings import get_embedding_model

    model = model or get_embedding_model()
    try:
        encoding = tiktoken.encoding_for_model(model)
    except KeyError:
        encoding = tiktoken.get_encoding("cl100k_base")

    new_chunks = []
    for chunk in chunks:
        text = chunk.get("text", "")
        if not text:
            new_chunks.append(chunk)
            continue

        tokens = encoding.encode(text)
        if len(tokens) <= max_tokens:
            new_chunks.append(chunk)
            continue

        # Split tokens into segments of at most max_tokens size
        for i in range(0, len(tokens), max_tokens):
            chunk_tokens = tokens[i : i + max_tokens]
            chunk_text = encoding.decode(chunk_tokens)
            new_chunk = dict(chunk)
            new_chunk["text"] = chunk_text
            new_chunks.append(new_chunk)

    return new_chunks
