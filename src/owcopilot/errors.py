# 项目异常根基类(D-9):给散落各处、各自挂 stdlib 父类的领域异常一个统一的公共根,
#   让调用方能 `except OWCopilotError` 一次兜住"本项目自己抛的错",并按类型分层处置
#   (技能层 :mod:`owcopilot.skills.errors` 的 classify_error 就据此按 isinstance 归类)。
"""Project-wide exception root.

Historically every OWCopilot exception hung directly off a stdlib base (``ValueError`` /
``RuntimeError`` / ``Exception``), so a caller had no single type to catch "one of our own
errors" apart from an unexpected bug, and no typed spine to branch on. :class:`OWCopilotError`
is that spine: the project's domain exceptions inherit from it (keeping their original stdlib
base as a *secondary* base, so every existing ``except ValueError`` / ``except RuntimeError``
site keeps working byte-for-byte — this only *adds* a common ancestor, it never changes an
exception's existing ``isinstance`` relationships).

The module has **zero** intra-package imports so any module can depend on it without an import
cycle.
"""

from __future__ import annotations


class OWCopilotError(Exception):
    """Common ancestor of every exception OWCopilot raises on purpose.

    Subclasses keep whatever stdlib base they already had (``ValueError`` etc.) *in addition* to
    this root, so catching ``OWCopilotError`` selects "a deliberate OWCopilot failure" without
    disturbing existing stdlib-typed handlers.
    """
