"""Audit execution context."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from ..content.hash import content_hash
from ..content.models import ContentBundle
from ..graph.index import ContentGraph, build_content_graph


class AuditContext(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    bundle: ContentBundle
    graph: ContentGraph
    content_hash: str

    @classmethod
    def from_bundle(cls, bundle: ContentBundle) -> AuditContext:
        return cls(
            bundle=bundle,
            graph=build_content_graph(bundle),
            content_hash=content_hash(bundle),
        )
