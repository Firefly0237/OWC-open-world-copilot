<script setup lang="ts">
import { ref } from "vue";
import OwBadge from "../../components/ow/OwBadge.vue";
import OwButton from "../../components/ow/OwButton.vue";
import OwEmpty from "../../components/ow/OwEmpty.vue";
import OwEntityPicker from "../../components/ow/OwEntityPicker.vue";
import OwInput from "../../components/ow/OwInput.vue";
import OwModal from "../../components/ow/OwModal.vue";
import OwPane from "../../components/ow/OwPane.vue";
import OwSelect from "../../components/ow/OwSelect.vue";
import OwSkeleton from "../../components/ow/OwSkeleton.vue";
import OwStat from "../../components/ow/OwStat.vue";
import OwStepper from "../../components/ow/OwStepper.vue";
import OwSwitch from "../../components/ow/OwSwitch.vue";
import OwTabs from "../../components/ow/OwTabs.vue";
import OwTag from "../../components/ow/OwTag.vue";
import OwTextarea from "../../components/ow/OwTextarea.vue";
import OwToastHost from "../../components/ow/OwToastHost.vue";
import { owToast } from "../../components/ow/owToast";

const text = ref("");
const longText = ref("");
const flag = ref(true);
const tab = ref("archive");
const choice = ref("region");
const entity = ref("");
const step = ref(1);
const modalOpen = ref(false);
const dangerOpen = ref(false);

const tabItems = [
  { key: "archive", label: "设定档案" },
  { key: "characters", label: "人物" },
  { key: "graph", label: "关系图谱" },
  { key: "timeline", label: "时间线" },
  { key: "analytics", label: "世界分析", badge: 3 },
];
const selectOptions = [
  { value: "region", label: "雾铃群岛", hint: "区域" },
  { value: "faction", label: "潮声会", hint: "阵营" },
  { value: "character", label: "白盐", hint: "人物" },
];
const entityOptions = [
  { id: "region.mist", label: "雾铃群岛", type: "区域" },
  { id: "region.saltflat", label: "白盐滩", type: "区域" },
  { id: "faction.tide", label: "潮声会", type: "阵营" },
  { id: "faction.bell", label: "雾铃守", type: "阵营" },
  { id: "char.baiyan", label: "白盐", type: "人物" },
  { id: "char.luoyi", label: "洛一", type: "人物" },
  { id: "quest.salt", label: "盐税之夜", type: "任务" },
];
</script>

<template>
  <div class="ow2 gallery">
    <header class="g-head">
      <span class="ow2-overline">ASTRAL ARCHIVE · V2 COMPONENTS</span>
      <h1 class="g-title">云上智库 · 组件陈列页</h1>
      <p class="g-sub">P1 验收场：15 件组件的常态 / 交互 / 禁用 / 忙态全矩阵。规范见 docs/前端重设计方案.md §7。</p>
    </header>

    <OwPane ornament>
      <h2 class="g-sec">OwButton 按钮</h2>
      <div class="g-row">
        <OwButton variant="primary">运行校勘</OwButton>
        <OwButton variant="secondary">预演（不写入）</OwButton>
        <OwButton variant="ghost">刷新</OwButton>
        <OwButton variant="danger">删除世界</OwButton>
        <OwButton variant="primary" loading>生成中</OwButton>
        <OwButton variant="secondary" disabled>不可用</OwButton>
      </div>
    </OwPane>

    <OwPane>
      <h2 class="g-sec">OwInput / OwTextarea / OwSwitch 输入</h2>
      <div class="g-grid2">
        <label class="g-field"><span>标题 <OwBadge tone="cinnabar">必填</OwBadge></span>
          <OwInput v-model="text" placeholder="例如：雾铃群岛设定稿 v3" :maxlength="80" /></label>
        <label class="g-field"><span>接地与参考</span>
          <span class="g-inline"><OwSwitch v-model="flag" /> {{ flag ? "已开启" : "已关闭" }}</span></label>
        <label class="g-field wide"><span>核心想法（字数环 ≥90% 变朱砂）</span>
          <OwTextarea v-model="longText" :maxlength="4000" placeholder="一句话说出你的世界…" autosize /></label>
      </div>
    </OwPane>

    <OwPane>
      <h2 class="g-sec">OwSelect 下拉 / OwEntityPicker 实体选择器</h2>
      <div class="g-grid2">
        <label class="g-field"><span>类型筛选（↑↓ 键导航 · Enter 选定 · Esc 关闭）</span>
          <OwSelect v-model="choice" :options="selectOptions" /></label>
        <label class="g-field"><span>焦点实体（搜索 + 分组 + 最近使用）</span>
          <OwEntityPicker v-model="entity" :options="entityOptions" :recent-ids="['char.baiyan', 'faction.tide']" /></label>
      </div>
      <p class="g-note">当前选中：{{ choice }} / {{ entity || "（未选）" }}</p>
    </OwPane>

    <OwPane>
      <h2 class="g-sec">OwTabs 分库标签</h2>
      <OwTabs v-model="tab" :items="tabItems" />
      <p class="g-note">激活分库：{{ tab }}</p>
    </OwPane>

    <OwPane>
      <h2 class="g-sec">OwBadge / OwTag 徽记</h2>
      <div class="g-row">
        <OwBadge tone="gold">人工</OwBadge><OwBadge tone="violet">AI 产物</OwBadge>
        <OwBadge tone="green">已采纳</OwBadge><OwBadge tone="amber">警告</OwBadge>
        <OwBadge tone="cinnabar">错误</OwBadge><OwBadge tone="sky">引用</OwBadge>
      </div>
      <div class="g-row">
        <OwTag tone="gold">阵营</OwTag><OwTag tone="sky">区域</OwTag>
        <OwTag removable @remove="owToast('已移除标签', 'info')">可移除标签</OwTag>
      </div>
    </OwPane>

    <OwPane>
      <h2 class="g-sec">OwStat 指标块</h2>
      <div class="g-grid4">
        <OwStat label="阵营" :value="6" hint="▲ 本周 +1" />
        <OwStat label="人物" :value="24" hint="就绪 19" />
        <OwStat label="任务" :value="31" hint="3 条欠发育" hint-tone="amber" />
        <OwStat label="待审" :value="3" hint="需要处理" hint-tone="cinnabar" to="/v2/gallery" />
      </div>
    </OwPane>

    <OwPane>
      <h2 class="g-sec">OwStepper 步骤条</h2>
      <OwStepper :steps="['选文件', '预演', '写入']" :current="step" />
      <div class="g-row" style="margin-top: 14px">
        <OwButton variant="ghost" @click="step = Math.max(0, step - 1)">上一步</OwButton>
        <OwButton variant="secondary" @click="step = Math.min(2, step + 1)">下一步</OwButton>
      </div>
    </OwPane>

    <OwPane>
      <h2 class="g-sec">OwSkeleton / OwEmpty 加载与空态</h2>
      <div class="g-grid2">
        <OwSkeleton :lines="4" />
        <OwEmpty compact title="还没有对话树" hint="去创作区生成一棵对话树，这里就能编辑它的分支流程。" cta="前往创作" @action="owToast('跳转示例', 'info')" />
      </div>
    </OwPane>

    <OwPane>
      <h2 class="g-sec">OwModal / OwToast 反馈</h2>
      <div class="g-row">
        <OwButton variant="secondary" @click="modalOpen = true">打开确认框</OwButton>
        <OwButton variant="danger" @click="dangerOpen = true">危险二次确认</OwButton>
        <OwButton variant="ghost" @click="owToast('世界包导出完成', 'success')">成功提示</OwButton>
        <OwButton variant="ghost" @click="owToast('连接失败，请检查服务', 'error')">错误提示</OwButton>
      </div>
    </OwPane>

    <OwModal :open="modalOpen" title="应用这条修复？" @confirm="modalOpen = false; owToast('修复已应用', 'success')" @cancel="modalOpen = false">
      应用前会先影子验证：在副本上应用并重新审计，若引入新错误则自动丢弃。
    </OwModal>
    <OwModal :open="dangerOpen" title="删除世界「雾铃群岛」？" danger confirm-text="永久删除" @confirm="dangerOpen = false; owToast('已删除（示例）', 'error')" @cancel="dangerOpen = false">
      世界包、设定与变更史都会被删除，此操作不可撤销。建议先导出世界包备份。
    </OwModal>
    <OwToastHost />
  </div>
</template>

<style scoped>
.gallery {
  min-height: 100vh;
  background: linear-gradient(175deg, #eef3f4 0%, var(--ow2-paper) 34%, #f3ecdc 100%);
  padding: 34px clamp(20px, 5vw, 56px) 60px;
  display: flex;
  flex-direction: column;
  gap: 18px;
}
.g-head { margin-bottom: 6px; }
.g-title { font-family: var(--ow2-serif); font-size: 26px; font-weight: 700; color: var(--ow2-ink); margin: 4px 0 4px; }
.g-sub { color: var(--ow2-ink-soft); font-size: 12.5px; margin: 0; }
.g-sec { font-family: var(--ow2-serif); font-size: 15px; color: var(--ow2-gold-deep); margin: 0 0 12px; }
.g-row { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; margin-bottom: 8px; }
.g-row:last-child { margin-bottom: 0; }
.g-grid2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 14px; }
.g-grid4 { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px; }
.g-field { display: flex; flex-direction: column; gap: 6px; font-size: 11.5px; color: var(--ow2-ink-soft); }
.g-field.wide { grid-column: 1 / -1; }
.g-inline { display: inline-flex; align-items: center; gap: 9px; font-size: 12.5px; color: var(--ow2-ink); }
.g-note { font-size: 11px; color: var(--ow2-muted); margin: 10px 0 0; }
</style>
