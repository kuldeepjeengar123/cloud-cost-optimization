import faiss
import numpy as np
import pickle
import os

def save_vectors_to_faiss(vectors, index_path, metadata_path=None, metadata=None):
    """
    Save vectors to FAISS index
    
    Args:
        vectors: numpy array of vectors to save
        index_path: path to save the FAISS index file
        metadata_path: optional path to save metadata
        metadata: optional metadata associated with vectors
    """
    print("saving in faiss.......")
    # Ensure vectors is numpy array
    if not isinstance(vectors, np.ndarray):
        vectors = np.array(vectors)
    
    # Handle 1D vector (single embedding) by reshaping to 2D
    if vectors.ndim == 1:
        vectors = vectors.reshape(1, -1)
    
    # Get dimension of vectors
    dimension = vectors.shape[1]
    
    # Create FAISS index (using L2 distance)
    index = faiss.IndexFlatL2(dimension)
    
    # Add vectors to index
    index.add(vectors.astype(np.float32))
    
    # Create directory if it doesn't exist
    os.makedirs(os.path.dirname(index_path), exist_ok=True)
    
    # Save the index
    faiss.write_index(index, index_path)
    
    # Save metadata if provided
    if metadata is not None and metadata_path is not None:
        with open(metadata_path, 'wb') as f:
            pickle.dump(metadata, f)
    
    print(f"Saved {len(vectors)} vectors to {index_path}")

def load_faiss_index(index_path, metadata_path=None):
    """
    Load FAISS index and optional metadata
    
    Args:
        index_path: path to the FAISS index file
        metadata_path: optional path to metadata file
        
    Returns:
        index: loaded FAISS index
        metadata: loaded metadata (if provided)
    """
    # Load the index
    index = faiss.read_index(index_path)
    
    metadata = None
    if metadata_path and os.path.exists(metadata_path):
        with open(metadata_path, 'rb') as f:
            metadata = pickle.load(f)
    
    return index, metadata