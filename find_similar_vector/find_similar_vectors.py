import numpy as np
import faiss
import os   

def find_similar_vectors(
    query_vector: np.ndarray,
    index_path: str,
    top_k: int = 5
):
    """
    Search FAISS index and return similar vectors
    """
    # Load the FAISS index
    if not os.path.exists(index_path):
        print(f"FAISS index not found at {index_path}")
        return None
    
    index = faiss.read_index(index_path)
    
    # Ensure query_vector is 2D and float32
    if query_vector.ndim == 1:
        query_vector = query_vector.reshape(1, -1)
    query_vector = query_vector.astype("float32")
    
    # Search for similar vectors
    scores, indices = index.search(query_vector, top_k)
    
    return {
        "scores": scores[0].tolist(),
        "indices": indices[0].tolist(),
        "distances": scores[0].tolist()
    }