// 来源归属词表：素材/文稿两个入口共用一份，别让两页各写一套拼写。
// Rights-status vocabulary for both intake doors. Mirrors owcopilot.inspiration.models.Provenance.

export type Provenance = "own_ip" | "licensed" | "third_party_reference";

export interface ProvenanceOption {
  value: Provenance;
  label: string;
  hint: string;
}

export const PROVENANCE_OPTIONS: ProvenanceOption[] = [
  { value: "own_ip", label: "自有", hint: "自己写的稿子 / 本项目自有的设定" },
  { value: "licensed", label: "已授权", hint: "已获授权，可用于本项目" },
  { value: "third_party_reference", label: "第三方参考", hint: "别人的作品，只能当参考素材" },
];

// 提炼门只收自有 / 已授权：产物会进正典草稿。第三方材料走灵感库。
export const CANON_INTAKE_BLOCKED: Provenance = "third_party_reference";

export const CANON_INTAKE_HINT =
  "第三方参考材料请走「灵感库」：提炼的产物会进正典草稿，只收自有或已授权的材料。" +
  "灵感库与正典物理隔离，只在生成时被检索取灵感，不会写进设定。";
