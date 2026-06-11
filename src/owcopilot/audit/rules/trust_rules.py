"""Trust and provenance audit rules."""

from __future__ import annotations

from collections.abc import Iterable

from ...trust import iter_provenance_records
from ..context import AuditContext
from ..models import Category, Evidence, Issue, Severity


class UnreviewedAIContentRule:
    code = "UNREVIEWED_AI_CONTENT"
    severity = Severity.WARNING
    category = Category.TRUST

    def check(self, ctx: AuditContext) -> Iterable[Issue]:
        for record in iter_provenance_records(ctx.bundle):
            if record.origin == "human" or record.review_status == "approved":
                continue
            yield Issue(
                rule_code=self.code,
                severity=self.severity,
                category=self.category,
                target_ref=record.ref,
                message=(
                    f"{record.ref} was created by {record.origin.value} and is still "
                    f"{record.review_status.value}"
                ),
                evidence=[
                    Evidence(
                        kind="provenance",
                        target_ref=record.ref,
                        data={
                            "origin": record.origin.value,
                            "review_status": record.review_status.value,
                            "source_path": record.source_path,
                        },
                    )
                ],
            )
