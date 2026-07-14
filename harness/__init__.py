"""Provenance-first corpus collection into portable Markdown records.

The library exposes an auditable record model and explicit source adapters. It does
not grant permission to collect content or bypass source access controls; operators
remain responsible for source terms, licenses, privacy, and applicable law.
"""

from .acquisition import CollectionResult, CollectionSpec, collect
from .base import BaseScraper, CorpusItem, WriteResult, write_corpus_item

__version__ = "0.2.0.dev0"
__all__ = [
    "BaseScraper",
    "CollectionResult",
    "CollectionSpec",
    "CorpusItem",
    "WriteResult",
    "collect",
    "write_corpus_item",
    "__version__",
]
