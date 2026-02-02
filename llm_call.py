

import requests
import json
from text_vector.text_vector import text_to_vector, retrieve_text_from_indices
from save_vector.save_vector import load_faiss_index
from calling_llm.llm_call_module import get_response_llm
from find_similar_vector.find_similar_vectors import find_similar_vectors
import faiss
import numpy as np
import os

user = "what is ai and how to use it in daily life?"
model = "xiaomi/mimo-v2-flash:free"

# Convert user query to vector
query_vector = text_to_vector(user)

# Search for similar vectors in the existing FAISS index
index_path = "faiss_index/index.faiss"
similar_results = find_similar_vectors(query_vector, index_path, top_k=3)

if similar_results:

    original_texts = retrieve_text_from_indices(similar_results['indices'])
    
    if original_texts:
        for i, text in enumerate(original_texts):
            print(f"\nChunk {i+1} (Index: {similar_results['indices'][i]}, Distance: {similar_results['distances'][i]:.4f}):")
            print(f"{text[:200]}..." if len(text) > 200 else text)
else:
    print("No similar vectors found or index not available.")
    original_texts = []

context = " ".join(original_texts) if original_texts else "No context available."
messages = [
    {"role": "system", "content": f"You are a helpful assistant. Use the following context to answer questions: {context}"},
    {"role": "user", "content": user}
]

message = get_response_llm(messages, model)
print(f"\nFinal message: {message}")