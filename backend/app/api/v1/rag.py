"""
RAG Intelligence API — regulatory knowledge base query endpoint.
Copyright (C) 2024 Sarthak Doshi (github.com/SdSarthak)
SPDX-License-Identifier: AGPL-3.0-only

TODO for contributors (high difficulty):
  - Pre-load the EU AI Act, GDPR, ISO 42001, and NIST AI RMF as source documents
  - Add streaming responses via SSE for long answers
  - Implement document version tracking and re-ingestion workflows
"""

import time
import os
import shutil
import tempfile
from typing import List, Optional
from pydantic import BaseModel, Field
from fastapi import UploadFile, File
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.security import get_current_user
from app.models.rag_feedback import RAGFeedback
from app.models.rag_document import RagDocument, RagDocumentChunk
from app.models.user import SubscriptionTier, User
from app.modules.rag.document_loader import (
    load_documents_from_paths,
    load_documents_from_paths_with_metadata,
)
from app.modules.rag.vector_store import create_vector_store
from app.models.rag_query import RagQuery

router = APIRouter()



class RAGQueryRequest(BaseModel):
    question: str


class RAGQueryResponse(BaseModel):
    answer: str
    sources: list[str] = []
    answer_id: Optional[str] = None


class RAGIngestResponse(BaseModel):
    """Response returned after a successful document ingestion."""

    files_processed: int
    chunks_created: int
    index_size_bytes: int


class RagDocumentResponse(BaseModel):
    """Response model for a RAG document with metadata."""
    
    id: int
    filename: str
    title: Optional[str] = None
    description: Optional[str] = None
    file_size_bytes: int
    chunks_count: int
    document_type: Optional[str] = None
    embedding_model: Optional[str] = None
    is_indexed: bool
    created_at: str
    
    class Config:
        from_attributes = True


class RagDocumentListResponse(BaseModel):
    """Response model for listing RAG documents."""
    
    documents: list[RagDocumentResponse]
    total_count: int


class RagDocumentChunkResponse(BaseModel):
    """Response model for a RAG document chunk."""
    
    id: int
    chunk_index: int
    content_hash: Optional[str] = None
    faiss_id: Optional[str] = None
    start_page: Optional[int] = None
    end_page: Optional[int] = None
    created_at: str
    
    class Config:
        from_attributes = True


class RagDocumentChunksResponse(BaseModel):
    """Response model for listing chunks of a RAG document."""
    
    document_id: int
    filename: str
    chunks: list[RagDocumentChunkResponse]
    total_chunks: int


# ---------------------------------------------------------------------------
# POST /rag/ingest
# ---------------------------------------------------------------------------
@router.post(
    "/ingest",
    response_model=RAGIngestResponse,
    summary="Upload & index regulatory PDFs",
    tags=["RAG Intelligence"],
)
def ingest_documents(
    files: List[UploadFile] = File(..., description="One or more PDF files to ingest"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Accept one or more PDF uploads, process them through the document loader,
    build (or rebuild) the FAISS vector index, persist it to
    ``settings.FAISS_INDEX_PATH``, and register documents in the database.

    **Returns**
    - ``files_processed`` - number of PDFs successfully saved and chunked
    - ``chunks_created``  - total text chunks fed into the vector store
    - ``index_size_bytes`` - on-disk size of the persisted FAISS index

    **Errors**
    - ``400`` if no valid PDF files are supplied
    - ``503`` if the embedding model or FAISS build step fails
    """

    # ── 1. Validate: at least one PDF ─────────────────────────────────────
    pdf_files = [
        f for f in files
        if f.filename and f.filename.lower().endswith(".pdf")
        and f.content_type in ("application/pdf", "binary/octet-stream", None)
    ]
    if not pdf_files:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No valid PDF files supplied. Please upload files with a .pdf extension.",
        )

    # ── 2. Save uploads to a temporary directory ──────────────────────────
    tmp_dir = tempfile.mkdtemp(prefix="aegis_ingest_")
    saved_paths: list[str] = []

    try:
        for upload in pdf_files:
            dest = os.path.join(tmp_dir, os.path.basename(upload.filename))
            with open(dest, "wb") as buf:
                shutil.copyfileobj(upload.file, buf)
            saved_paths.append(dest)

        # ── 3. Chunk documents with metadata ─────────────────────────────
        chunks, chunks_metadata = load_documents_from_paths_with_metadata(saved_paths)
        if not chunks:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Could not extract any text from the supplied PDFs. "
                       "Ensure the files are not scanned images or password-protected.",
            )

        # ── 4. Build / rebuild FAISS index and persist to disk ────────────
        try:
            create_vector_store(saved_paths)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Failed to build FAISS index: {exc}",
            )

        # ── 5. Register documents and chunks in the database ──────────────
        embedding_model = settings.LLM_MODEL
        
        for file_path in saved_paths:
            filename = os.path.basename(file_path)
            file_size_bytes = os.path.getsize(file_path)
            
            # Create RagDocument record
            rag_document = RagDocument(
                filename=filename,
                file_path=file_path,
                file_size_bytes=file_size_bytes,
                title=filename.replace(".pdf", "").replace("_", " "),
                chunks_count=0,  # Will be updated below
                embedding_model=embedding_model,
                document_type="CUSTOM",  # Default to CUSTOM; can be updated by admin
                uploaded_by_id=current_user.id,
                is_indexed=False,  # Will be set to True after chunks are created
                index_version="1.0",
            )
            db.add(rag_document)
            db.flush()  # Flush to get the document ID
            
            # Get metadata for this file
            file_metadata = chunks_metadata.get(filename, {})
            chunk_info_list = file_metadata.get("chunks", [])
            
            # Create RagDocumentChunk records for each chunk
            for idx, chunk_info in enumerate(chunk_info_list):
                rag_chunk = RagDocumentChunk(
                    document_id=rag_document.id,
                    chunk_index=idx,
                    content="",  # Content is stored in FAISS, not in DB for performance
                    content_hash=chunk_info.get("content_hash", ""),
                    faiss_id=f"{rag_document.id}_{idx}",  # Use document_id_chunk_idx format
                    embedding_vector_dim=1536,  # OpenAI embeddings dimension
                    start_page=chunk_info.get("page", 0),
                    end_page=chunk_info.get("page", 0),
                )
                db.add(rag_chunk)
            
            # Update chunk count and mark as indexed
            rag_document.chunks_count = len(chunk_info_list)
            rag_document.is_indexed = True
        
        # Commit all database changes
        db.commit()

        # ── 6. Calculate on-disk index size ───────────────────────────────
        index_path = settings.FAISS_INDEX_PATH
        index_size_bytes = 0
        for fname in ("index.faiss", "index.pkl"):
            fpath = os.path.join(index_path, fname)
            if os.path.exists(fpath):
                index_size_bytes += os.path.getsize(fpath)

        return RAGIngestResponse(
            files_processed=len(saved_paths),
            chunks_created=len(chunks),
            index_size_bytes=index_size_bytes,
        )

    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Ingestion failed: {str(e)}",
        )
    finally:
        # ── 7. Always clean up the temp directory ─────────────────────────
        shutil.rmtree(tmp_dir, ignore_errors=True)


@router.post("/query", response_model=RAGQueryResponse)
def query_knowledge_base(
    request: RAGQueryRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Ask a regulatory question and get an answer grounded in source documents.

    Example questions:
    - "Does my CV-screening tool qualify as high-risk under the EU AI Act?"
    - "What are the transparency requirements for chatbots?"
    """
    try:
        from app.modules.rag.retrieval_chain import get_qa_chain
        from app.core.database import Base

        qa_chain = get_qa_chain()

        t_start = time.monotonic()
        result = qa_chain({"query": request.question})
        latency_ms = (time.monotonic() - t_start) * 1000

        source_docs = result.get("source_documents", [])
        sources = [str(doc.metadata.get("source", "")) for doc in source_docs]
        answer = str(result.get("result", ""))

        # Ensure tables exist on this DB bind (useful for test DB overrides)
        try:
            Base.metadata.create_all(bind=db.get_bind())
        except Exception:
            pass

        # Persist feedback row
        feedback = RAGFeedback(
            question=request.question,
            answer=answer,
            source_chunks=sources,
        )
        db.add(feedback)
        rag_query = RagQuery(
            user_id=current_user.id,
            question=request.question,
            answer_summary=str(result.get("result", ""))[:200],
            source_count=len(sources),
        )
        db.add(rag_query)
        db.commit()
        db.refresh(feedback)

        # Log to MLflow (non-blocking — failures are swallowed inside log_query)
        try:
            from app.modules.rag.ml_flow import log_query
            log_query(
                question=request.question,
                answer=answer,
                sources=sources,
                latency_ms=latency_ms,
            )
        except Exception:
            pass

        return RAGQueryResponse(answer=answer, sources=sources, answer_id=feedback.id)
    except FileNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(e),
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"RAG module error: {str(e)}",
        )


@router.get(
    "/ingest/documents",
    response_model=RagDocumentListResponse,
    summary="List all ingested regulatory documents",
    tags=["RAG Intelligence"],
)
def list_ingested_documents(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Retrieve a list of all uploaded regulatory documents with their metadata.
    
    **Returns**
    - List of documents with: id, filename, title, chunks_count, document_type, etc.
    - Total count of documents
    """
    documents = db.query(RagDocument).all()
    
    doc_responses = [
        RagDocumentResponse(
            id=doc.id,
            filename=doc.filename,
            title=doc.title,
            description=doc.description,
            file_size_bytes=doc.file_size_bytes,
            chunks_count=doc.chunks_count,
            document_type=doc.document_type,
            embedding_model=doc.embedding_model,
            is_indexed=bool(doc.is_indexed),
            created_at=doc.created_at.isoformat() if doc.created_at else "",
        )
        for doc in documents
    ]
    
    return RagDocumentListResponse(
        documents=doc_responses,
        total_count=len(documents),
    )


@router.get(
    "/ingest/documents/{document_id}/chunks",
    response_model=RagDocumentChunksResponse,
    summary="Get all chunks of a specific document",
    tags=["RAG Intelligence"],
)
def get_document_chunks(
    document_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Retrieve all text chunks extracted from a specific uploaded document.
    
    **Returns**
    - Document metadata (id, filename, total_chunks)
    - List of chunks with: chunk_index, content_hash, page numbers, etc.
    
    **Errors**
    - ``404`` if the document does not exist
    """
    document = db.query(RagDocument).filter(RagDocument.id == document_id).first()
    if not document:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Document with id {document_id} not found",
        )
    
    chunks = db.query(RagDocumentChunk).filter(
        RagDocumentChunk.document_id == document_id
    ).order_by(RagDocumentChunk.chunk_index).all()
    
    chunk_responses = [
        RagDocumentChunkResponse(
            id=chunk.id,
            chunk_index=chunk.chunk_index,
            content_hash=chunk.content_hash,
            faiss_id=chunk.faiss_id,
            start_page=chunk.start_page,
            end_page=chunk.end_page,
            created_at=chunk.created_at.isoformat() if chunk.created_at else "",
        )
        for chunk in chunks
    ]
    
    return RagDocumentChunksResponse(
        document_id=document.id,
        filename=document.filename,
        chunks=chunk_responses,
        total_chunks=len(chunks),
    )


@router.delete(
    "/ingest/documents/{document_id}",
    summary="Delete an ingested document and rebuild FAISS index",
    tags=["RAG Intelligence"],
)
def delete_document(
    document_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Delete a document and all its associated chunks from the database.
    After deletion, the FAISS index will be rebuilt from remaining documents.
    
    **Note**: This operation requires re-ingestion of all remaining documents
    to update the FAISS vector index.
    
    **Returns**
    - Success message with document filename
    
    **Errors**
    - ``404`` if the document does not exist
    - ``503`` if FAISS rebuild fails
    """
    document = db.query(RagDocument).filter(RagDocument.id == document_id).first()
    if not document:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Document with id {document_id} not found",
        )
    
    filename = document.filename
    
    try:
        # Delete all chunks for this document
        db.query(RagDocumentChunk).filter(
            RagDocumentChunk.document_id == document_id
        ).delete()
        
        # Delete the document
        db.delete(document)
        db.commit()
        
        # Rebuild FAISS index from remaining documents
        remaining_documents = db.query(RagDocument).filter(
            RagDocument.is_indexed == True
        ).all()
        
        if remaining_documents:
            remaining_paths = [doc.file_path for doc in remaining_documents]
            try:
                create_vector_store(remaining_paths)
            except Exception as exc:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=f"Failed to rebuild FAISS index after deletion: {exc}",
                )
        else:
            # If no documents remain, we could optionally clean up the FAISS index
            # For now, we'll just leave it as-is
            pass
        
        return {
            "message": f"Document '{filename}' successfully deleted",
            "document_id": document_id,
        }
    
    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Failed to delete document: {str(e)}",
        )


@router.get("/health", tags=["RAG Intelligence"])
def rag_health():
    """Check if the RAG module is available."""
    from app.modules.rag.vector_store import check_index_exists
    
    index_loaded = check_index_exists()
    
    if not index_loaded:
        return {
            "module": "rag_intelligence",
            "status": "unavailable",
            "index_loaded": False,
            "message": "FAISS index not found. RAG module requires document ingestion before use."
        }
    
    return {
        "module": "rag_intelligence",
        "status": "available",
        "index_loaded": True
    }


class RAGFeedbackRequest(BaseModel):
    answer_id: str
    vote: str  # "up" or "down"


@router.post("/feedback")
def rag_feedback(
    payload: RAGFeedbackRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Record a thumbs-up or thumbs-down for a previously returned answer."""
    fb = db.query(RAGFeedback).filter(RAGFeedback.id == payload.answer_id).first()
    if not fb:
        raise HTTPException(status_code=404, detail="Answer not found")
    if payload.vote == "up":
        fb.thumbs_up = (fb.thumbs_up or 0) + 1
    else:
        fb.thumbs_down = (fb.thumbs_down or 0) + 1
    db.add(fb)
    db.commit()
    db.refresh(fb)
    return {"status": "ok", "answer_id": fb.id}


@router.get("/low-quality-chunks")
def get_low_quality_chunks(
    threshold: float = 0.3,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Admin endpoint: aggregate feedback by source chunk and return low-quality candidates.

    A chunk is considered low-quality when thumbs_down / total_feedback > threshold.
    """
    # Admin-only access: restrict to system owners / scale tier
    try:
        if current_user.subscription_tier != SubscriptionTier.SCALE:
            raise HTTPException(status_code=403, detail="Admin access required")
    except Exception:
        raise HTTPException(status_code=403, detail="Admin access required")

    # Aggregate counts per chunk
    counts: dict[str, dict[str, int]] = {}
    rows = db.query(RAGFeedback).all()
    for r in rows:
        total = (r.thumbs_up or 0) + (r.thumbs_down or 0)
        for chunk in (r.source_chunks or []):
            if chunk not in counts:
                counts[chunk] = {"thumbs_up": 0, "thumbs_down": 0, "total": 0}
            counts[chunk]["thumbs_up"] += (r.thumbs_up or 0)
            counts[chunk]["thumbs_down"] += (r.thumbs_down or 0)
            counts[chunk]["total"] += total

    low_quality = []
    for chunk, c in counts.items():
        if c["total"] == 0:
            continue
        ratio = c["thumbs_down"] / c["total"]
        if ratio > threshold:
            low_quality.append({"chunk": chunk, "thumbs_down": c["thumbs_down"], "total": c["total"], "ratio": ratio})

    return {"threshold": threshold, "low_quality_chunks": low_quality}


@router.get("/history")
def get_rag_history(
    page: int = 1,
    page_size: int = 10,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return paginated list of the current user's past RAG queries."""
    offset = (page - 1) * page_size
    queries = (
        db.query(RagQuery)
        .filter(RagQuery.user_id == current_user.id)
        .order_by(RagQuery.created_at.desc())
        .offset(offset)
        .limit(page_size)
        .all()
    )
    return {
        "page": page,
        "page_size": page_size,
        "results": [
            {
                "id": q.id,
                "question": q.question,
                "answer_summary": q.answer_summary,
                "source_count": q.source_count,
                "created_at": q.created_at,
            }
            for q in queries
        ],
    }
