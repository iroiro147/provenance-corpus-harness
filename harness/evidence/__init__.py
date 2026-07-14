"""Local, provenance-preserving evidence indexing and retrieval."""

from .build import IndexBuildResult, build_evidence_index
from .query import QueryResult, query_evidence_index
from .schema import EvidenceCitation, RightsDeclaration
from .verify import VerifyResult, verify_evidence_index

__all__ = [
    "EvidenceCitation",
    "IndexBuildResult",
    "QueryResult",
    "RightsDeclaration",
    "VerifyResult",
    "build_evidence_index",
    "query_evidence_index",
    "verify_evidence_index",
]
