# 审计执行上下文。
"""Audit execution context."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict

from ..content.hash import content_hash
from ..content.models import ContentBundle
from ..graph.index import ContentGraph, build_content_graph


class AuditContext(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    bundle: ContentBundle
    graph: ContentGraph
    content_hash: str
    #: 这份内容在磁盘上的根目录。给出时，执行器会给每条问题补上"文件 + 行号 + 节点 id"
    #: （行业报告 §15.9 的六要素）。**只有当 ``bundle`` 就是这个目录里的内容时才给**：候选
    #: bundle（打补丁后的、合并后的、请求体里直接传进来的）在磁盘上并不存在，拿磁盘文件去标
    #: 行号只会指错地方，所以那些路径一律留空。
    content_root: Path | None = None

    @classmethod
    def from_bundle(
        cls, bundle: ContentBundle, *, content_root: Path | str | None = None
    ) -> AuditContext:
        return cls(
            bundle=bundle,
            graph=build_content_graph(bundle),
            content_hash=content_hash(bundle),
            content_root=Path(content_root) if content_root is not None else None,
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
