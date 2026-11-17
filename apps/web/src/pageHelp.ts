export interface PageHelp { title: string; tips: string[] }
export const PAGE_HELP: Record<string, PageHelp> = {
  "/creator": { title: "剧情创作", tips: ["填写创作要求，按需选择资料和参与方式。", "局部修改先生成独立建议，查看实际差异后再应用。", "当前作品修改与长期方法采用分别决定。"] },
  "/evolution": { title: "流程诊断", tips: ["查看实际工作图、输入来源和执行记录。", "试用仅限已许可任务，不自动成为默认方法。"] },
  "/worlds": { title: "工作区", tips: ["新工作区为空，可导入自己的内容。", "切换工作区不会合并任务或许可。"] },
  "/settings": { title: "设置", tips: ["配置模型服务并检查连接。", "密钥仅用于本地连接，不应写进正文或导出文件。"] },
};
