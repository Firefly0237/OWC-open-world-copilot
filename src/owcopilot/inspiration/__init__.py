"""Inspiration reference library.

Reference materials live beside a content project but outside the canonical content bundle.
They are searchable for creative generation, never mixed into lore QA by default.
"""

from .models import ReferenceChunk, ReferenceIngestResult, ReferenceSource
from .retrieval import ReferenceContextBuilder
from .store import ReferenceStore, decode_reference_bytes

__all__ = [
    "ReferenceChunk",
    "ReferenceContextBuilder",
    "ReferenceIngestResult",
    "ReferenceSource",
    "ReferenceStore",
    "decode_reference_bytes",
]
