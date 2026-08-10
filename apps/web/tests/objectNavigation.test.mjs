import assert from "node:assert/strict";
import test from "node:test";

import {
  ARCHIVE_KINDS,
  locateDialogueParent,
  locationForObject,
} from "../src/objectNavigation.ts";

const archiveKinds = [
  "entity",
  "region",
  "poi",
  "term",
  "relation",
  "style_guide",
  "quest_event_ref",
  "style_rubric",
];

test("every backend archive kind has a stable deep-link destination", () => {
  assert.deepEqual(ARCHIVE_KINDS, [
    "entity",
    "quest",
    "region",
    "poi",
    "term",
    "dialogue",
    "dialogue_tree",
    "relation",
    "style_guide",
    "quest_event_ref",
    "style_rubric",
  ]);

  for (const kind of archiveKinds) {
    assert.deepEqual(locationForObject({ kind, ref: `${kind}:sample` }, {}, "needle"), {
      path: "/archive",
      query: { focus: `${kind}:sample`, page: "1", kind, q: "needle" },
    });
  }

  assert.deepEqual(locationForObject({ kind: "quest", ref: "quest:q-1" }), {
    path: "/timeline",
    query: { focus: "quest:q-1", kind: "quest" },
  });
  assert.deepEqual(locationForObject({ kind: "dialogue_tree", ref: "dialogue_tree:t-1" }), {
    path: "/dialogues",
    query: { focus: "dialogue_tree:t-1", kind: "dialogue_tree" },
  });
  assert.deepEqual(locationForObject({ kind: "localized_text", ref: "localized_text:k-1" }), {
    path: "/localization",
    query: { focus: "localized_text:k-1", kind: "localized_text" },
  });
});

test("dialogue deep links resolve their parent tree and exact node", () => {
  const inventory = {
    dialogues: [{ id: "line-1", text_key: "dialogue.guard.opening" }],
    dialogue_tree_payloads: {
      gate: {
        id: "gate-tree",
        nodes: {
          opening: { id: "opening", text_key: "dialogue.guard.opening" },
        },
      },
    },
  };

  assert.deepEqual(locateDialogueParent(inventory.dialogues[0], inventory.dialogue_tree_payloads), {
    treeId: "gate-tree",
    nodeId: "opening",
  });
  assert.deepEqual(locationForObject({ kind: "dialogue", ref: "dialogue:line-1" }, inventory, "guard"), {
    path: "/dialogues",
    query: {
      focus: "dialogue_tree:gate-tree",
      node: "opening",
      dialogue: "dialogue:line-1",
      kind: "dialogue",
      q: "guard",
    },
  });
});

test("dialogue parent matching supports node ids and falls back to archive", () => {
  assert.deepEqual(
    locateDialogueParent(
      { id: "line-by-id" },
      { tree: { nodes: { key: { id: "line-by-id" } } } },
    ),
    { treeId: "tree", nodeId: "line-by-id" },
  );

  assert.deepEqual(locationForObject({ kind: "dialogue", ref: "dialogue:orphan" }), {
    path: "/archive",
    query: { focus: "dialogue:orphan", page: "1", kind: "dialogue" },
  });
});
