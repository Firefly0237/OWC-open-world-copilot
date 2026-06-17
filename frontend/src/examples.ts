/** Rotating input examples. Each pool has several varied, deliberately cross-genre entries so the
 * placeholder never nudges the user toward one theme; `example(key)` is called once at component
 * setup, so a fresh example shows on every page load/refresh. Fixed guidance copy is NOT sourced
 * from here — only the "for example…" placeholders are. */

const POOLS: Record<string, string[]> = {
  genesisIdea: [
    "一座建在巨兽脊背上的迁徙城邦，方向之争撕裂了掌舵的家族。",
    "末日后用声音导航的雾海群岛，谁掌握航路谁就掌握粮食。",
    "魔法被课以重税的官僚帝国，无证施法者在地下结社。",
    "两条时间线交错的边境小镇，居民同时记得两种过去。",
    "沙暴吞没旧世界后，靠贩卖记忆维生的绿洲商队。",
    "深空殖民船世代沉睡，醒来的守舱人发现航向早已被人篡改。",
  ],
  questBrief: [
    "护送一批易碎货物穿过敌对地界，途中要决定是否相信一名可疑向导。",
    "调查矿镇接连失踪的工人，线索指向掌权阵营不愿曝光的秘密。",
    "在两个结盟在即的家族间传话，玩家的措辞会左右联姻成败。",
    "潜入节庆混入贵族宅邸，取回一封会引发战争的信。",
    "帮一位老兵找回被典当的遗物，却发现它牵连一桩旧案。",
  ],
  expandAngle: [
    "把这片区域的走私网络铺开：谁在暗中输送、谁在睁只眼闭只眼。",
    "深化这个阵营的内部裂痕，让玩家撞见一场即将摊牌的派系倾轧。",
    "顺着这条主线长出几条岔路，每条都逼玩家在两派之间重新站队。",
    "补全这片区域的日常肌理：市集、信仰、底层与权贵的摩擦。",
  ],
  characterConcept: [
    "以记忆为筹码的掮客，每做成一笔交易就永远失去一段过去。",
    "退役的边境向导，背着说不出口的内疚重新上路。",
    "笃信秩序的年轻执法官，开始怀疑自己维护的律法。",
    "走私者出身的酒馆老板，谁的情报都收，谁的立场都不选。",
    "被流放的占卜师，预言屡屡应验却没人愿意相信。",
  ],
  askQuery: [
    "这个世界的主要阵营之间是什么关系？",
    "谁控制着核心资源，他们想要什么？",
    "哪些角色和这条主线冲突直接相关？",
    "这个地点发生过什么重要事件？",
  ],
  sweepTheme: [
    "与某个主题相关的内容（如某类禁忌元素）",
    "提及某个已弃用角色的所有条目",
    "涉及某条需要统一口径的设定的内容",
    "与某个敏感地名相关的描述",
  ],
  referenceMaterial: [
    "一段你欣赏的世界观文字、设定风格样本或灵感笔记。",
    "想借鉴节奏与氛围的章节片段。",
    "某种叙事结构的参考提纲。",
  ],
  dialogueBrief: [
    "两人就是否揭发一桩秘密争执，各自有不能退让的理由。",
    "一次试探性的结盟谈判，话里有话。",
    "久别重逢的旧识，旧账未清。",
  ],
};

export function example(key: keyof typeof POOLS | string): string {
  const pool = POOLS[key];
  if (!pool || pool.length === 0) return "";
  return pool[Math.floor(Math.random() * pool.length)];
}
