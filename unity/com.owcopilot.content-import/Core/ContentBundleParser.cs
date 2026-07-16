// content_bundle.json 的解析入口（Newtonsoft）：失败给引导式异常，不抛裸报错。
// Parse content_bundle.json into DTOs. Guarded, guided errors (never a raw stack trace at the UI):
// on malformed JSON we raise ContentBundleParseException with a next-step hint. Engine-free so it is
// unit-testable off-engine.

using System;
using Newtonsoft.Json;

namespace OWCopilot.ContentImport.Core
{
    /// <summary>Thrown when the bundle text is empty or not valid content_bundle.json.</summary>
    public sealed class ContentBundleParseException : Exception
    {
        public ContentBundleParseException(string message, Exception inner = null)
            : base(message, inner)
        {
        }
    }

    public static class ContentBundleParser
    {
        private static readonly JsonSerializerSettings Settings = new JsonSerializerSettings
        {
            // Unknown wire fields are tolerated (the bundle carries more than the importer models);
            // do NOT throw on them so new content fields never break an older importer.
            MissingMemberHandling = MissingMemberHandling.Ignore,
        };

        public static ContentBundleDto Parse(string json)
        {
            if (string.IsNullOrWhiteSpace(json))
            {
                throw new ContentBundleParseException(
                    "内容包 JSON 为空。请确认已由 OWCopilot 导出 content_bundle.json，且选中的是该文件。");
            }

            ContentBundleDto dto;
            try
            {
                dto = JsonConvert.DeserializeObject<ContentBundleDto>(json, Settings);
            }
            catch (JsonException ex)
            {
                throw new ContentBundleParseException(
                    "content_bundle.json 解析失败：" + ex.Message +
                    "。请确认文件由 OWCopilot 导出且未被手工破坏。", ex);
            }

            if (dto == null)
            {
                throw new ContentBundleParseException(
                    "content_bundle.json 解析结果为空（顶层不是对象）。请确认这是有效的内容包文件。");
            }

            return dto;
        }
    }
}
