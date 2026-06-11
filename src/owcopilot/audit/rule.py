"""Audit rule protocol."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from .context import AuditContext
from .models import Category, Issue, Severity


class Rule(Protocol):
    code: str
    severity: Severity
    category: Category

    def check(self, ctx: AuditContext) -> Iterable[Issue]: ...
