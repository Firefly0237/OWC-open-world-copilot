"""Preset world-seed briefs for one-click genesis (pure data, no copyrighted text)."""

from __future__ import annotations

from typing import Any

GENESIS_TEMPLATES: dict[str, dict[str, Any]] = {
    "武侠江湖": {
        "idea": "王朝末年，漕运断绝，七大门派围绕一部失传剑谱与漕帮控制权明争暗斗，"
        "玩家是被逐出师门的镖师，要在江湖道义与生存之间做选择。",
        "game_genre": "开放世界武侠 RPG",
        "world_styles": ["武侠", "历史架空"],
        "tone": "苍凉、克制、恩怨分明",
        "era": "古代王朝末期",
        "player_fantasy": "被逐出师门的镖师",
        "core_conflict": "剑谱争夺与漕运命脉之争",
        "notes": "重视门派关系网与师承脉络；台词风格半文半白。",
    },
    "克苏鲁迷雾": {
        "idea": "二十世纪初的港口小城连续出现集体梦游，调查记者发现教会、渔业公会与"
        "大学考古系都在隐瞒同一件深海来物。",
        "game_genre": "叙事调查冒险",
        "world_styles": ["黑暗奇幻"],
        "tone": "压抑、悬疑、理智边缘",
        "era": "1920 年代",
        "player_fantasy": "执拗的地方报记者",
        "core_conflict": "真相会摧毁理智，沉默会摧毁小城",
        "notes": "线索网状交叉；NPC 各自只知碎片；避免直接展示怪物。",
    },
    "废土余生": {
        "idea": "大停摆五十年后，绿洲水塔由三股势力轮流掌管，今年轮值的商队联盟"
        "突然封锁了水配额，玩家是水塔维修工，握有旧时代的检修密钥。",
        "game_genre": "生存建造 + 阵营声望",
        "world_styles": ["废土", "科幻"],
        "tone": "粗粝、黑色幽默、务实",
        "era": "近未来废土",
        "player_fantasy": "掌握旧密钥的维修工",
        "core_conflict": "水资源配给与旧科技归属",
        "notes": "物资稀缺感要落在任务奖励数值上；俚语多。",
    },
    "校园异能": {
        "idea": "一所滨海高中在午夜会与「镜面校园」重叠，学生会以社团为单位划分"
        "守夜班次，玩家是转学生，发现自己的影子比别人晚三秒。",
        "game_genre": "都市异能 + 社团经营",
        "world_styles": ["魔幻"],
        "tone": "青春、神秘、温暖中带刺",
        "era": "现代",
        "player_fantasy": "影子异常的转学生",
        "core_conflict": "镜面校园的扩张与社团间的守夜权之争",
        "notes": "时间表驱动叙事（白天/午夜双线）；称呼用学长学姐体系。",
    },
    "西方奇幻": {
        "idea": "龙息冷却的百年后，边境侯国靠拍卖龙骨维持财政，新一代圣堂骑士却在"
        "龙冢里听见了心跳，玩家是负责清点龙骨的书记官。",
        "game_genre": "开放世界奇幻 RPG",
        "world_styles": ["魔幻", "黑暗奇幻"],
        "tone": "史诗、肃穆、暗流涌动",
        "era": "中世纪奇幻",
        "player_fantasy": "卷入阴谋的龙冢书记官",
        "core_conflict": "龙骨经济与龙族复苏的双重危机",
        "notes": "纹章与头衔体系要成体系；地名带古语词根。",
    },
    "星海歌剧": {
        "idea": "跃迁航道被未知力量逐条熄灭，三大殖民舰团被迫在最后一条航道上共享"
        "灯塔站，玩家是灯塔站的新任调度员，掌握着放行顺序。",
        "game_genre": "太空歌剧 + 站点管理",
        "world_styles": ["科幻"],
        "tone": "宏大、孤独、外交辞令下的紧张",
        "era": "远未来星际",
        "player_fantasy": "灯塔站调度员",
        "core_conflict": "航道熄灭真相与舰团放行次序的政治",
        "notes": "用航道/灯塔/舰团的术语表统一叙事；广播体公告文本。",
    },
}
