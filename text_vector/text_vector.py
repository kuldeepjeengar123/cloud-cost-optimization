from sentence_transformers import SentenceTransformer
import numpy as np
import pickle
import os
from typing import List, Union, Optional

def text_to_vector(text: Union[str, List[str]], model_name: str = 'all-MiniLM-L6-v2') -> np.ndarray:
    """
    Convert text to vector using sentence transformer.
    
    Args:
        text: Single string or list of strings to convert
        model_name: Name of the sentence transformer model to use
        
    Returns:
        numpy array of embeddings
    """
    print("vector process started.......")
    model = SentenceTransformer(model_name)
    embeddings = model.encode(text)
    return embeddings

def chunk_text_for_vectors(text: str, chunk_size: int = 500, overlap: int = 50) -> List[str]:
    """
    Split text into chunks for better vector representation.
    
    Args:
        text: Input text to chunk
        chunk_size: Size of each chunk
        overlap: Overlap between chunks
        
    Returns:
        List of text chunks
    """
    chunks = []
    words = text.split()
    
    for i in range(0, len(words), chunk_size - overlap):
        chunk = ' '.join(words[i:i + chunk_size])
        chunks.append(chunk)
        
    return chunks

def retrieve_text_from_indices(indices: List[int], metadata_path: str = "faiss_index/metadata.pkl") -> List[str]:
    """
    Retrieve original text chunks using indices from FAISS search.
    
    Args:
        indices: List of indices from FAISS search
        metadata_path: Path to the metadata file containing original text chunks
        
    Returns:
        List of original text chunks
    """
    if not os.path.exists(metadata_path):
        print(f"Metadata file not found at {metadata_path}")
        return []
    
    try:
        with open(metadata_path, 'rb') as f:
            metadata = pickle.load(f)
        
        # Retrieve text chunks using indices
        retrieved_chunks = []
        for idx in indices:
            if idx < len(metadata):
                retrieved_chunks.append(metadata[idx])
            else:
                retrieved_chunks.append(f"Index {idx} out of range")
        
        return retrieved_chunks
    
    except Exception as e:
        print(f"Error loading metadata: {e}")
        return []