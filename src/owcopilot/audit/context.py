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

    def has_object(self, object_id: str) -> bool:
        """Whether ``object_id`` names any referenceable canon object. Terms are included: the graph
        index makes them nodes and the editor can wire relations to them, so a relation endpoint
        that names a term is valid, not a dangling reference."""
        return (
            object_id in self.bundle.entities
            or object_id in self.bundle.pois
            or object_id in self.bundle.regions
            or object_id in self.bundle.quests
            or object_id in self.bundle.terms
        )
