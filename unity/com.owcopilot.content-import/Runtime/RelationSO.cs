// 关系资产：对应 owcopilot.content.models.Relation（source/target/kind 外键）。
// A Relation ScriptableObject. Relations have no id of their own in the model (they are identified
// by source+kind+target); the importer mints a deterministic asset name from those. Raw ids stay
// authoritative; Source/Target are the resolved entity references (null = unresolved, kept for
// review — mirroring recognize/engine_data.py's honest behaviour).

using UnityEngine;

namespace OWCopilot.ContentImport
{
    public sealed class RelationSO : ContentObjectSO
    {
        public string SourceId;
        public EntitySO Source;

        public string TargetId;
        public EntitySO Target;

        public string Kind;

        // valid_from / valid_until are int|None on the wire.
        public bool HasValidFrom;
        public int ValidFrom;

        public bool HasValidUntil;
        public int ValidUntil;
    }
}
