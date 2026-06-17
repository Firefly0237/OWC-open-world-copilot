/** Shared node/edge palette for the SVG graph views (relationship graph + dialogue flow).
 * One source for kind→colour so the legend, the relationship graph and the dialogue flow can't
 * drift apart. Colours are the 星海之卷 accent family (see styles/tokens.css). */

export interface GraphNode {
  ref: string;
  x: number;
  y: number;
  kind: string;
  label: string;
  sublabel?: string;
  focus?: boolean;
  flag?: string; // "must_change" | "suggest_check" — drawn as a coloured ring
}

export interface GraphEdge {
  source: string;
  target: string;
  kind?: string;
  label?: string;
  symmetric?: boolean; // peer/undirected — drawn without an arrowhead
}

const KIND_COLOR: Record<string, string> = {
  faction: "#d9b56c",
  npc: "#8fd6e8",
  location: "#7fcaa0",
  region: "#7fcaa0",
  poi: "#7fcaa0",
  quest: "#b89cf0",
  event: "#e0a878",
  dialogue: "#c9b3e6",
  term: "#9d97ad",
  // dialogue-flow node kinds
  root: "#d9b56c",
  line: "#8fd6e8",
  end: "#9d97ad",
};

export function kindColor(kind: string): string {
  return KIND_COLOR[kind] ?? "#9d97ad";
}

// Gem gradient families (bright core → mid → deep) for the faceted-node rendering. One family per
// node kind keeps the SVG <defs> to a small fixed set rather than a gradient per node.
const KIND_FAMILY: Record<string, string> = {
  faction: "gold",
  npc: "cyan",
  location: "green",
  region: "green",
  poi: "green",
  quest: "violet",
  event: "amber",
  dialogue: "violet",
  term: "muted",
  root: "gold",
  line: "cyan",
  end: "muted",
};

export function kindFamily(kind: string): string {
  return KIND_FAMILY[kind] ?? "muted";
}

// A tiny per-family type glyph drawn at a node's centre, in unit coords (about [-1,1]); the renderer
// scales it. Lets a circular "star位" node still tell faction from npc from location at a glance.
const FAMILY_GLYPH: Record<string, string> = {
  gold: "M0,-1 L0.24,-0.24 L1,0 L0.24,0.24 L0,1 L-0.24,0.24 L-1,0 L-0.24,-0.24 Z", // 8-point star
  cyan: "M0,0 m-0.62,0 a0.62,0.62 0 1,0 1.24,0 a0.62,0.62 0 1,0 -1.24,0 Z", // disc
  green: "M0,-0.95 L0.85,0.7 L-0.85,0.7 Z", // pin / triangle
  violet: "M0,-1 L0.66,0 L0,1 L-0.66,0 Z", // diamond
  amber:
    "M-0.16,-1 L0.16,-1 L0.16,-0.16 L1,-0.16 L1,0.16 L0.16,0.16 L0.16,1 L-0.16,1 L-0.16,0.16 L-1,0.16 L-1,-0.16 L-0.16,-0.16 Z", // spark/cross
  muted: "M-0.78,-0.78 L0.78,-0.78 L0.78,0.78 L-0.78,0.78 Z", // tablet
};

export function kindGlyph(kind: string): string {
  return FAMILY_GLYPH[kindFamily(kind)] ?? FAMILY_GLYPH.muted;
}

export const GEM_FAMILIES: Record<string, [string, string, string]> = {
  gold: ["#fff3d4", "#f0d28a", "#a8853f"],
  cyan: ["#e6fbff", "#8fd6e8", "#3f7f94"],
  green: ["#e7fbef", "#7fcaa0", "#3a7256"],
  violet: ["#f3edff", "#b89cf0", "#5d4f8a"],
  amber: ["#ffe9d2", "#e0a878", "#9a6238"],
  muted: ["#cfcadb", "#9d97ad", "#5a5570"],
};

export const FLAG_COLOR: Record<string, string> = {
  must_change: "#e0705a",
  suggest_check: "#e0a878",
};

// ---- edge categories: so 所属(membership) and 人际(interpersonal) and 邦交/地理/叙事 read distinctly,
// mirroring the relation-kind catalog (content/relation_kinds.py). Drives link colour/arrow/flow. ----
export type EdgeCat = "affiliation" | "interpersonal" | "alliance" | "geography" | "narrative" | "other";

const EDGE_KIND_CAT: Record<string, EdgeCat> = {
  // 所属 · 隶属 (directed hierarchy)
  member_of: "affiliation",
  vassal_of: "affiliation",
  controls: "affiliation",
  funds: "affiliation",
  controlling_faction: "affiliation",
  // 阵营 · 邦交 (faction ↔ faction)
  ally_of: "alliance",
  enemy_of: "alliance",
  rival_of: "alliance",
  at_war_with: "alliance",
  trades_with: "alliance",
  // 人际 · 关系
  kin_of: "interpersonal",
  friend_of: "interpersonal",
  nemesis_of: "interpersonal",
  lover_of: "interpersonal",
  companion_of: "interpersonal",
  knows: "interpersonal",
  mentor_of: "interpersonal",
  superior_of: "interpersonal",
  employs: "interpersonal",
  owes: "interpersonal",
  // 地理 · 位置
  located_in: "geography",
  poi_region: "geography",
  borders: "geography",
  leads_to: "geography",
  // 叙事 · 牵涉
  involves: "narrative",
  references: "narrative",
  triggers: "narrative",
  requires: "narrative",
  giver_npc: "narrative",
};

export function edgeCat(kind: string): EdgeCat {
  return EDGE_KIND_CAT[kind] ?? "other";
}

export interface EdgeCatMeta {
  color: string;
  label: string;
  arrow: boolean;
  flow: boolean;
}
export const EDGE_CAT_META: Record<EdgeCat, EdgeCatMeta> = {
  affiliation: { color: "#f0d28a", label: "所属 · 隶属", arrow: true, flow: false },
  alliance: { color: "#b89cf0", label: "阵营 · 邦交", arrow: false, flow: true },
  interpersonal: { color: "#8fd6e8", label: "人际 · 关系", arrow: false, flow: true },
  geography: { color: "#7fcaa0", label: "地理 · 位置", arrow: true, flow: false },
  narrative: { color: "#e0a878", label: "叙事 · 牵涉", arrow: true, flow: false },
  other: { color: "#6a7298", label: "其他", arrow: false, flow: false },
};

/** Legend rows shown above a graph: [kind, label]. */
export const GRAPH_LEGEND: ReadonlyArray<readonly [string, string]> = [
  ["faction", "阵营"],
  ["npc", "角色"],
  ["location", "地点"],
  ["quest", "任务"],
  ["event", "事件"],
];
