import os
from pdf_text.pdf_text import extract_text_from_pdf
from text_vector.text_vector import text_to_vector, chunk_text_for_vectors
from save_vector.save_vector import save_vectors_to_faiss
import pickle

def import_pdf(filename):
    """
    Import a PDF file from the docs folder
    
    Args:
        filename (str): Name of the PDF file to import
    
    Returns:
        str: Extracted text from the PDF
    """
    docs_folder = "docs"
    file_path = os.path.join(docs_folder, filename)
    
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"PDF file not found: {file_path}")
    print("Importing successful")
    print("converting.....")
    text = extract_text_from_pdf(file_path)
    
    return text

text = import_pdf("2019BurkovTheHundred-pageMachineLearning.pdf")
print("pdf converted to text successfully")

# Chunk the text for better vector representation
text_chunks = chunk_text_for_vectors(text)
print(f"Text chunked into {len(text_chunks)} chunks")

# Convert text chunks to vectors
vectors = text_to_vector(text_chunks)
print("text converted to vectors successfully")

# Save vectors to FAISS index
metadata_path = "faiss_index/metadata.pkl"
saving_vector = save_vectors_to_faiss(vectors, "faiss_index/index.faiss", metadata_path, text_chunks)
print("vectors saved to faiss successfully")

# Also save text chunks as metadata for later retrieval
os.makedirs("faiss_index", exist_ok=True)
with open(metadata_path, 'wb') as f:
    pickle.dump(text_chunks, f)
print("metadata saved successfully")
