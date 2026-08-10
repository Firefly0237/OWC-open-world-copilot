export const ARCHIVE_KINDS = [
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
] as const;

export type ArchiveKind = (typeof ARCHIVE_KINDS)[number];

export interface SearchHitLike {
  ref: string;
  kind: string;
}

export interface DialogueInventoryRow {
  id: string;
  text_key?: string;
}

export interface DialogueNodePayload {
  id?: string;
  text_key?: string;
}

export interface DialogueTreePayload {
  id?: string;
  nodes?: Record<string, DialogueNodePayload>;
}

export interface ArchiveNavigationInventory {
  dialogues?: DialogueInventoryRow[];
  dialogue_tree_payloads?: Record<string, DialogueTreePayload>;
}

export interface ObjectLocation {
  path: string;
  query: Record<string, string>;
}

export interface DialogueParentLocation {
  treeId: string;
  nodeId: string;
}

export function isArchiveKind(value: unknown): value is ArchiveKind {
  return typeof value === "string" && (ARCHIVE_KINDS as readonly string[]).includes(value);
}

export function objectIdFromRef(ref: string): string {
  const separator = ref.indexOf(":");
  return separator >= 0 ? ref.slice(separator + 1) : ref;
}

export function kindFromRef(ref: string): ArchiveKind | null {
  const separator = ref.indexOf(":");
  const kind = separator >= 0 ? ref.slice(0, separator) : "";
  return isArchiveKind(kind) ? kind : null;
}

export function locateDialogueParent(
  dialogue: DialogueInventoryRow,
  payloads: Record<string, DialogueTreePayload> = {},
): DialogueParentLocation | null {
  for (const [payloadKey, tree] of Object.entries(payloads)) {
    for (const [nodeKey, node] of Object.entries(tree.nodes ?? {})) {
      const nodeId = node.id || nodeKey;
      const sameTextKey = Boolean(dialogue.text_key && node.text_key === dialogue.text_key);
      if (sameTextKey || nodeId === dialogue.id || nodeKey === dialogue.id) {
        return { treeId: tree.id || payloadKey, nodeId };
      }
    }
  }
  return null;
}

function withSearchContext(
  query: Record<string, string>,
  kind: string,
  searchQuery: string,
): Record<string, string> {
  const next: Record<string, string> = { ...query, kind };
  const q = searchQuery.trim();
  if (q) next.q = q;
  return next;
}

export function locationForObject(
  hit: SearchHitLike,
  inventory: ArchiveNavigationInventory = {},
  searchQuery = "",
): ObjectLocation {
  if (hit.kind === "quest") {
    return {
      path: "/timeline",
      query: withSearchContext({ focus: hit.ref }, hit.kind, searchQuery),
    };
  }
  if (hit.kind === "dialogue_tree") {
    return {
      path: "/dialogues",
      query: withSearchContext({ focus: hit.ref }, hit.kind, searchQuery),
    };
  }
  if (hit.kind === "dialogue") {
    const id = objectIdFromRef(hit.ref);
    const dialogue = inventory.dialogues?.find((row) => row.id === id) ?? { id };
    const parent = locateDialogueParent(dialogue, inventory.dialogue_tree_payloads);
    if (parent) {
      return {
        path: "/dialogues",
        query: withSearchContext(
          {
            focus: `dialogue_tree:${parent.treeId}`,
            node: parent.nodeId,
            dialogue: hit.ref,
          },
          hit.kind,
          searchQuery,
        ),
      };
    }
  }
  if (hit.kind === "localized_text") {
    return {
      path: "/localization",
      query: withSearchContext({ focus: hit.ref }, hit.kind, searchQuery),
    };
  }

  const archiveKind = isArchiveKind(hit.kind) ? hit.kind : kindFromRef(hit.ref) ?? "entity";
  return {
    path: "/archive",
    query: withSearchContext(
      { focus: hit.ref, page: "1" },
      archiveKind,
      searchQuery,
    ),
  };
}
