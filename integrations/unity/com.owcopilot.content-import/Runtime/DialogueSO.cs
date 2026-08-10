// 对白引用资产：对应 owcopilot.content.models.DialogueRef。
// A DialogueRef ScriptableObject: a localizable line keyed by text_key, optionally tied to a
// speaker entity and a quest. Both are stored as raw ids (authoritative) plus resolved object
// references wired by the two-pass importer.

using UnityEngine;

namespace OWCopilot.ContentImport
{
    public sealed class DialogueSO : ContentObjectSO
    {
        public string TextKey;

        [Tooltip("Speaker entity id (raw, authoritative).")]
        public string SpeakerId;

        [Tooltip("Resolved speaker entity (null when SpeakerId is unknown — kept for review).")]
        public EntitySO Speaker;

        [Tooltip("Owning quest id (raw, authoritative).")]
        public string QuestId;

        [Tooltip("Resolved owning quest (null when QuestId is unknown — kept for review).")]
        public QuestSO Quest;

        [TextArea(2, 6)]
        public string Text;

        public string Locale;

        // ui_max_len is int|None on the wire; Unity can't serialize int?, so flag presence.
        public bool HasUiMaxLen;
        public int UiMaxLen;
    }
}
