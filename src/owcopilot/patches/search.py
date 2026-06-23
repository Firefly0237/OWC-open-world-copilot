"""Monte Carlo Tree Search over repair sequences.

Fixing one audit issue can change which fixes are valid for another (drop a dangling reference and
a downstream rule clears too; the order can matter), so picking the best *sequence* of edits is a
search problem, not a one-shot choice. This plans that sequence with textbook MCTS — selection
(UCB1) → expansion → simulation → backpropagation.

The reason MCTS is practical here, when it usually is not for LLM agents, is the reward: the
consistency **audit is deterministic and fast**, so a rollout's value (how many errors a sequence
resolves) costs no model call. That free, cheap reward is exactly what MCTS needs and what normally
makes LLM-MCTS too expensive to run.

Discipline matches the rest of the project: every move is a *shadow-validated* deterministic patch
candidate (the same ones :mod:`owcopilot.patches.suggest` proposes), every move is constrained to
never increase the open-error count, and the planner only returns a *plan* — applying it stays on
the human write path. The search is seeded, so it is fully reproducible and testable.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from random import Random

from pydantic import BaseModel, Field

from ..audit.baseline import issue_fingerprint
from ..audit.context import AuditContext
from ..audit.models import Issue
from ..audit.runner import AuditRunner
from ..content.hash import content_hash
from ..content.models import ContentBundle
from .fixers import deterministic_candidates
from .models import PatchCandidate, PatchOperation
from .shadow import apply_patch_shadow

CandidateProvider = Callable[[Issue, ContentBundle], list[PatchCandidate]]

_EXPLORATION = math.sqrt(2)  # the standard UCB1 exploration constant


class RepairMove(BaseModel):
    """One step of a repair plan: a shadow-validated patch for one audit issue."""

    issue_fingerprint: str
    issue_ref: str
    rule_code: str
    patch_id: str
    ops: list[PatchOperation]
    rationale: str = ""


class RepairPlan(BaseModel):
    """The best repair sequence the search found (a proposal — it is never auto-applied)."""

    moves: list[RepairMove] = Field(default_factory=list)
    initial_open_errors: int
    final_open_errors: int
    resolved_errors: int
    iterations: int


@dataclass
class _Node:
    bundle: ContentBundle
    open_errors: int
    depth: int
    move: RepairMove | None = None
    parent: _Node | None = None
    children: list[_Node] = field(default_factory=list)
    # Lazily filled list of (move, resulting bundle, resulting open-error count).
    untried: list[tuple[RepairMove, ContentBundle, int]] | None = None
    visits: int = 0
    total_reward: float = 0.0


def plan_repairs(
    bundle: ContentBundle,
    audit_runner: AuditRunner,
    *,
    max_iterations: int = 200,
    max_depth: int = 8,
    seed: int = 0,
    exploration: float = _EXPLORATION,
    candidate_provider: CandidateProvider = deterministic_candidates,
) -> RepairPlan:
    """Search for the patch sequence that resolves the most open audit errors.

    Returns the best plan found. ``candidate_provider`` defaults to the deterministic fixers (so the
    whole search is $0 and reproducible); pass a model-backed provider to widen the move set.
    """
    return _RepairSearch(
        bundle,
        audit_runner,
        max_iterations=max(0, max_iterations),
        max_depth=max(1, max_depth),
        exploration=exploration,
        seed=seed,
        candidate_provider=candidate_provider,
    ).run()


class _RepairSearch:
    def __init__(
        self,
        bundle: ContentBundle,
        audit_runner: AuditRunner,
        *,
        max_iterations: int,
        max_depth: int,
        exploration: float,
        seed: int,
        candidate_provider: CandidateProvider,
    ) -> None:
        self.runner = audit_runner
        self.max_iterations = max_iterations
        self.max_depth = max_depth
        self.c = exploration
        self.rng = Random(seed)
        self.candidate_provider = candidate_provider
        self._error_cache: dict[str, list[Issue]] = {}
        self._move_cache: dict[str, list[tuple[RepairMove, ContentBundle, int]]] = {}
        root_errors = self._open_errors(bundle)
        self.root = _Node(bundle=bundle, open_errors=len(root_errors), depth=0)
        # Best terminal state found so far: (open_errors, plan_length, moves) — minimise errors,
        # then prefer the shorter plan. Start from "do nothing".
        self._best: tuple[int, int, list[RepairMove]] = (self.root.open_errors, 0, [])

    def run(self) -> RepairPlan:
        if self.root.open_errors > 0:
            for _ in range(self.max_iterations):
                node = self._select(self.root)
                child = self._expand(node)
                reward = self._simulate(child)
                self._backpropagate(child, reward)
        final_errors, _length, moves = self._best
        return RepairPlan(
            moves=moves,
            initial_open_errors=self.root.open_errors,
            final_open_errors=final_errors,
            resolved_errors=self.root.open_errors - final_errors,
            iterations=self.max_iterations if self.root.open_errors > 0 else 0,
        )

    # --- MCTS phases -------------------------------------------------------------------------
    def _select(self, node: _Node) -> _Node:
        while True:
            if node.untried is None:
                node.untried = self._applicable_moves(node.bundle)
            if node.open_errors == 0 or node.depth >= self.max_depth:
                return node  # terminal
            if node.untried or not node.children:
                return node  # has a move to expand, or a dead end with nothing tried
            node = self._best_uct_child(node)

    def _expand(self, node: _Node) -> _Node:
        if not node.untried:
            return node  # terminal / dead end: simulate from here
        move, child_bundle, child_errors = node.untried.pop()
        child = _Node(
            bundle=child_bundle,
            open_errors=child_errors,
            depth=node.depth + 1,
            move=move,
            parent=node,
        )
        node.children.append(child)
        return child

    def _simulate(self, node: _Node) -> float:
        """Random rollout of non-worsening moves; reward = fraction of initial errors resolved."""
        bundle = node.bundle
        errors = node.open_errors
        moves = self._path_moves(node)
        depth = node.depth
        while errors > 0 and depth < self.max_depth:
            available = self._applicable_moves(bundle)
            if not available:
                break
            move, bundle, errors = self.rng.choice(available)
            moves.append(move)
            depth += 1
        self._record(errors, moves)
        return (self.root.open_errors - errors) / self.root.open_errors

    def _backpropagate(self, node: _Node, reward: float) -> None:
        current: _Node | None = node
        while current is not None:
            current.visits += 1
            current.total_reward += reward
            current = current.parent

    def _best_uct_child(self, node: _Node) -> _Node:
        log_parent = math.log(node.visits) if node.visits > 0 else 0.0

        def uct(child: _Node) -> float:
            if child.visits == 0:
                return math.inf
            exploit = child.total_reward / child.visits
            explore = self.c * math.sqrt(log_parent / child.visits)
            return exploit + explore

        return max(node.children, key=uct)

    # --- domain hooks (the deterministic, cached reward signal) ------------------------------
    def _open_errors(self, bundle: ContentBundle) -> list[Issue]:
        key = content_hash(bundle)
        cached = self._error_cache.get(key)
        if cached is None:
            cached = self.runner.run(AuditContext.from_bundle(bundle)).open_errors
            self._error_cache[key] = cached
        return cached

    def _applicable_moves(
        self, bundle: ContentBundle
    ) -> list[tuple[RepairMove, ContentBundle, int]]:
        """Every shadow-valid, non-worsening one-step fix from this state (deduped by result)."""
        key = content_hash(bundle)
        cached = self._move_cache.get(key)
        if cached is not None:
            return cached
        current_errors = len(self._open_errors(bundle))
        moves: list[tuple[RepairMove, ContentBundle, int]] = []
        seen_states: set[str] = set()
        for issue in sorted(self._open_errors(bundle), key=issue_fingerprint):
            for candidate in self.candidate_provider(issue, bundle):
                try:
                    child = apply_patch_shadow(bundle, candidate.ops)
                except Exception:
                    continue
                child_hash = content_hash(child)
                if child_hash in seen_states:
                    continue
                child_errors = len(self._open_errors(child))
                if child_errors > current_errors:  # never make things worse
                    continue
                seen_states.add(child_hash)
                moves.append((_move_for(issue, candidate), child, child_errors))
        self._move_cache[key] = moves
        return moves

    # --- plan extraction ---------------------------------------------------------------------
    def _record(self, errors: int, moves: list[RepairMove]) -> None:
        if (errors, len(moves)) < (self._best[0], self._best[1]):
            self._best = (errors, len(moves), list(moves))

    @staticmethod
    def _path_moves(node: _Node) -> list[RepairMove]:
        moves: list[RepairMove] = []
        current: _Node | None = node
        while current is not None and current.move is not None:
            moves.append(current.move)
            current = current.parent
        moves.reverse()
        return moves


def _move_for(issue: Issue, candidate: PatchCandidate) -> RepairMove:
    fingerprint = issue.id or issue_fingerprint(issue)
    patch_id = (
        candidate.id
        or "patch_"
        + content_hash(
            {"issue": fingerprint, "ops": [op.model_dump(mode="json") for op in candidate.ops]}
        )[:16]
    )
    return RepairMove(
        issue_fingerprint=fingerprint,
        issue_ref=issue.target_ref,
        rule_code=issue.rule_code,
        patch_id=patch_id,
        ops=list(candidate.ops),
        rationale=candidate.rationale,
    )
