"""Document loader for ingesting regulatory PDFs from S3 or local disk."""

import os
import hashlib
from langchain_community.document_loaders import S3DirectoryLoader, PyPDFLoader
from langchain.text_splitter import RecursiveCharacterTextSplitter
from app.core.config import settings
from typing import List, Tuple, Dict, Any


def load_documents_from_s3():
    """Load documents from the configured S3 bucket."""
    bucket = settings.S3_BUCKET_NAME
    if not bucket:
        raise ValueError("S3_BUCKET_NAME is not set in .env")
    loader = S3DirectoryLoader(bucket, prefix="docs/")
    documents = loader.load()
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=settings.RAG_CHUNK_SIZE,
        chunk_overlap=settings.RAG_CHUNK_OVERLAP,
    )
    return splitter.split_documents(documents)


def load_documents_from_paths(file_paths: list[str]) -> List[Any]:
    """Load documents from a list of local PDF file paths."""
    documents = []
    for path in file_paths:
        loader = PyPDFLoader(path)
        documents.extend(loader.load())
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=settings.RAG_CHUNK_SIZE,
        chunk_overlap=settings.RAG_CHUNK_OVERLAP,
    )
    return splitter.split_documents(documents)


def load_documents_from_paths_with_metadata(
    file_paths: list[str],
) -> Tuple[List[Any], Dict[str, Any]]:
    """
    Load documents from a list of local PDF file paths with detailed metadata.
    
    Returns:
        Tuple of (chunks, metadata_dict) where:
        - chunks: List of LangChain Document objects
        - metadata_dict: Dict mapping filename -> {chunks, page_ranges, content_hashes}
    """
    documents = []
    metadata_dict = {}
    
    for file_path in file_paths:
        loader = PyPDFLoader(file_path)
        file_documents = loader.load()
        
        # Track metadata for this file
        filename = os.path.basename(file_path)
        file_chunks_info = {
            "filename": filename,
            "file_path": file_path,
            "total_documents": len(file_documents),
            "chunks": [],
        }
        
        documents.extend(file_documents)
    
    # Split all documents into chunks and attach source metadata
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=settings.RAG_CHUNK_SIZE,
        chunk_overlap=settings.RAG_CHUNK_OVERLAP,
    )
    
    chunks = splitter.split_documents(documents)
    
    # Build metadata for each chunk
    chunk_index = 0
    for chunk in chunks:
        source = chunk.metadata.get("source", "unknown")
        filename = os.path.basename(source)
        
        # Initialize metadata for this file if needed
        if filename not in metadata_dict:
            metadata_dict[filename] = {
                "filename": filename,
                "file_path": source,
                "chunks": [],
                "total_chunks": 0,
                "page_ranges": set(),
            }
        
        # Calculate content hash
        content_hash = hashlib.sha256(chunk.page_content.encode()).hexdigest()
        
        # Extract page information
        page = chunk.metadata.get("page", 0)
        
        chunk_info = {
            "chunk_index": chunk_index,
            "content_hash": content_hash,
            "page": page,
            "content_length": len(chunk.page_content),
        }
        
        metadata_dict[filename]["chunks"].append(chunk_info)
        metadata_dict[filename]["page_ranges"].add(page)
        chunk_index += 1
    
    # Convert page_ranges sets to sorted lists
    for filename in metadata_dict:
        page_ranges = sorted(metadata_dict[filename]["page_ranges"])
        metadata_dict[filename]["page_ranges"] = page_ranges
        metadata_dict[filename]["total_chunks"] = len(
            metadata_dict[filename]["chunks"]
        )
    
    return chunks, metadata_dict
