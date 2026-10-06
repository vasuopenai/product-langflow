"""Open Food Facts -> canonical product records for natural-language search."""

from .normalize import normalize, to_flat, to_pinecone_metadata
from .query import QuerySpec, QUERY_SPEC_SCHEMA, to_sql, to_pinecone_filter

__all__ = [
    "normalize", "to_flat", "to_pinecone_metadata",
    "QuerySpec", "QUERY_SPEC_SCHEMA", "to_sql", "to_pinecone_filter",
]
