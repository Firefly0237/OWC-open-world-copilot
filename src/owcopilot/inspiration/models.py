"""Models for user-provided inspiration references."""

from __future__ import annotations

from enum import Enum
from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator

# --- 素材"许可用途"词表（B3）------------------------------------------------------------------
# allowed_uses used to be a decorative label nobody read. It is now a retrieval filter: a reference
# is only recalled for a use its uploader permitted. One definition, shared by every consumer, so
# genesis/expansion and the draft chains cannot drift into two spellings of the same use.
USE_INSPIRATION = "inspiration"  # 创世/扩写取灵感（worldgen）
USE_DRAFTING = "drafting"  # 任务/人设/对白等草稿链路
REFERENCE_USES: frozenset[str] = frozenset({USE_INSPIRATION, USE_DRAFTING})


class Provenance(str, Enum):
    """入口来源归属（B2）：这份材料的权利状态，由收录它的人声明。

    This is a *declaration* gate, not a detector — it records what the human said about a
    document's rights status so the canon intake can refuse third-party material outright.
    Detecting actual reuse of reference text is a separate, deterministic check.
    """

    OWN_IP = "own_ip"  # 自有素材/自己写的稿子
    LICENSED = "licensed"  # 已获授权可用于本项目
    THIRD_PARTY_REFERENCE = "third_party_reference"  # 第三方作品，只能当参考


PROVENANCE_LABELS: dict[Provenance, str] = {
    Provenance.OWN_IP: "自有",
    Provenance.LICENSED: "已授权",
    Provenance.THIRD_PARTY_REFERENCE: "第三方参考",
}


def parse_provenance(value: str | Provenance) -> Provenance:
    """Normalise a caller-supplied provenance string, with a Chinese hint when it is unknown."""
    if isinstance(value, Provenance):
        return value
    try:
        return Provenance(value)
    except ValueError:
        known = "、".join(f"{item.value}（{PROVENANCE_LABELS[item]}）" for item in Provenance)
        raise ValueError(f"未知的来源归属「{value}」。可用值：{known}。") from None


def ensure_canon_intake_allowed(value: str | Provenance) -> Provenance:
    """Gate the canon intake (manuscript extraction): third-party material is refused there.

    Extraction turns a document into a canon draft, so it may only take material the project owns
    or has licensed. Third-party books belong in the inspiration library, which is physically
    separate from canon and is only ever read as retrieval grounding.
    """
    provenance = parse_provenance(value)
    if provenance is Provenance.THIRD_PARTY_REFERENCE:
        raise ValueError(
            "第三方参考材料请走素材库：文稿提炼的产物会进正典草稿，只收自有或已授权的材料。"
            "请到「灵感库」收录这份材料——素材库与正典物理隔离，只在生成时被检索取灵感，"
            "不会被写进设定。若这份材料确为自有或已获授权，请把来源归属改成 own_ip 或 licensed "
            "后重试。"
        )
    return provenance


class ReferenceSource(BaseModel):
    id: str
    title: str
    source_type: str = "uploaded_file"
    original_filename: str | None = None
    allowed_uses: list[str] = Field(default_factory=lambda: [USE_INSPIRATION])
    # Who owns this material. Defaults to the most conservative value so a reference stored before
    # this field existed (and any caller that stays silent) is treated as third-party.
    provenance: Provenance = Provenance.THIRD_PARTY_REFERENCE
    text_hash: str
    created_at: str
    # Detected at ingest so a whole book in any language is handled without asking the user.
    # Optional with defaults so references stored before this field still load.
    language: str = ""  # human label of the dominant language, e.g. "中文"
    languages: list[str] = Field(default_factory=list)  # all significant language labels
    char_count: int = 0
    chunk_count: int = 0
    metadata: dict[str, str] = Field(default_factory=dict)
    quarantine_status: Literal["clear", "quarantined", "approved"] = "clear"
    quarantine_reason: str = ""
    quarantine_approved_at: str | None = None

    @model_validator(mode="after")
    def quarantine_legacy_injection_flags(self) -> Self:
        """Fail closed for flagged records written before explicit quarantine fields existed."""
        if (
            self.metadata.get("injection_flagged") == "true"
            and self.quarantine_status == "clear"
        ):
            self.quarantine_status = "quarantined"
            self.quarantine_reason = self.quarantine_reason or "prompt_injection_detected"
        return self


class ReferenceChunk(BaseModel):
    id: str
    source_id: str
    chunk_index: int
    title: str
    body: str
    metadata: dict[str, str] = Field(default_factory=dict)


class ReferenceIngestResult(BaseModel):
    source: ReferenceSource
    chunks: list[ReferenceChunk]
    indexed_count: int
    # Chunks whose text matched a prompt-injection pattern. Uploaded references are untrusted and
    # reach generation prompts via grounding (OWASP LLM01 indirect injection), so we surface them
    # for the human's risk call rather than silently feeding them into a prompt.
    injection_flagged_chunks: list[str] = Field(default_factory=list)
