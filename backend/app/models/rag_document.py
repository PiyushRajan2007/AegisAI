"""RAG Document model for tracking uploaded regulatory PDFs."""

from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Float
from sqlalchemy.orm import relationship
from datetime import datetime
from app.core.database import Base


class RagDocument(Base):
    """Represents an uploaded regulatory document for RAG ingestion."""
    
    __tablename__ = "rag_documents"

    id = Column(Integer, primary_key=True, index=True)
    
    # File metadata
    filename = Column(String(255), nullable=False)
    file_path = Column(String(500), nullable=False)
    file_size_bytes = Column(Integer, nullable=False)
    
    # Document info
    title = Column(String(255))  # User-provided title
    description = Column(Text)   # User-provided description
    
    # Vector index metadata
    chunks_count = Column(Integer, default=0)  # Number of text chunks created
    embedding_model = Column(String(100))      # Which embedding model was used
    
    # Source tracking
    document_type = Column(String(50))  # e.g., "EU_AI_ACT", "GDPR", "ISO_42001", "CUSTOM"
    source_url = Column(String(500))    # Optional: URL where document came from
    
    # Ownership and timestamps
    uploaded_by_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Status tracking
    is_indexed = Column(Integer, default=False)  # Whether chunks are in FAISS
    index_version = Column(String(20), default="1.0")  # FAISS index version
    
    # Relationships
    uploaded_by = relationship("User", back_populates="rag_documents")


class RagDocumentChunk(Base):
    """Represents individual text chunks extracted from a RagDocument."""
    
    __tablename__ = "rag_document_chunks"

    id = Column(Integer, primary_key=True, index=True)
    
    # Reference to parent document
    document_id = Column(Integer, ForeignKey("rag_documents.id"), nullable=False)
    
    # Chunk content and metadata
    chunk_index = Column(Integer, nullable=False)  # Position in sequence
    content = Column(Text, nullable=False)
    content_hash = Column(String(64))  # SHA-256 hash for deduplication
    
    # Vector index reference
    faiss_id = Column(String(100))  # ID in FAISS index
    embedding_vector_dim = Column(Integer)  # Dimension of embedding
    
    # Metadata
    start_page = Column(Integer)  # Original page number in PDF
    end_page = Column(Integer)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    
    # Relationship
    document = relationship("RagDocument")
