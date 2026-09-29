from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from langchain_community.document_loaders import CSVLoader, PyPDFLoader
from langchain_community.vectorstores import FAISS
from langchain_core.documents import Document
from langchain_core.tools import BaseTool
from langchain_core.tools.retriever import create_retriever_tool
from langchain_text_splitters import RecursiveCharacterTextSplitter

from .providers import build_embeddings_client, load_config

_PROJECT_ROOT = Path(__file__).parent.parent
_CSV_SCHEMA_DIR = _PROJECT_ROOT / "sample_data" / "csv_schemas"
_PRICING_DOCS_DIR = _PROJECT_ROOT / "sample_data" / "pricing_docs"


def load_documents() -> List[Document]:
    """Load the 5 CSV column-schema files and the AWS pricing PDFs as
    LangChain Documents, tagging each with its source file name for citation."""
    docs: List[Document] = []

    for csv_file in sorted(_CSV_SCHEMA_DIR.glob("*.csv")):
        loaded = CSVLoader(file_path=str(csv_file)).load()
        for doc in loaded:
            doc.metadata["source"] = csv_file.name
        docs.extend(loaded)

    for pdf_file in sorted(_PRICING_DOCS_DIR.glob("*.pdf")):
        loaded = PyPDFLoader(str(pdf_file)).load()
        for doc in loaded:
            doc.metadata["source"] = pdf_file.name
        docs.extend(loaded)

    return docs


def chunk_documents(docs: List[Document]) -> List[Document]:
    splitter = RecursiveCharacterTextSplitter(chunk_size=500, chunk_overlap=50)
    return splitter.split_documents(docs)


def build_vectorstore(config: Optional[Dict[str, Any]] = None) -> FAISS:
    config = config or load_config()
    chunks = chunk_documents(load_documents())
    embeddings = build_embeddings_client(config)
    return FAISS.from_documents(chunks, embeddings)


def build_retriever_tool(config: Optional[Dict[str, Any]] = None) -> BaseTool:
    vectorstore = build_vectorstore(config)
    retriever = vectorstore.as_retriever(search_kwargs={"k": 3})
    return create_retriever_tool(
        retriever,
        name="search_cost_docs",
        description=(
            "Search AWS cost CSV column-schema descriptions and AWS pricing "
            "reference notes. Use this to find what a CSV column means (for "
            "example, UsageType in the daily_spend schema), or the expected "
            "price for a service, instance type, or storage class."
        ),
    )
