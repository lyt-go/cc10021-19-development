/** 纯前端数据层的公共类型：与全栈版后端返回的结构保持一致，换回后端时页面不用改。 */

export type EntryRow = {
  id: number
  status: string
  pending: boolean
  abnormal: boolean
  [field: string]: string | number | boolean
}

export type ModuleMeta = {
  key: string
  name: string
  entity: string
  desc: string
  fields: string[]
  statuses: string[]
  actions: string[]
  actionTargets: Record<string, string>
  metrics: string[]
}

export type PageResult = {
  items: EntryRow[]
  total: number
  page: number
  size: number
}

export type ActionResult = {
  ok: boolean
  message: string
}

export type OverviewResult = {
  cards: { label: string; value: number }[]
  modules: { name: string; created: number; pending: number; abnormal: number }[]
}

/** 交接包关系视图：范围内带字段/转换语句，范围外只留边界并标记证据欠缺。 */
export type HandoffRelation = {
  关系编号: string
  上游表名: string
  下游表名: string
  上游库名: string
  下游库名: string
  是否在交接范围: boolean
  来源字段: string
  目标字段: string
  转换逻辑: string
  证据状态?: string
  边界说明: string
}

export type HandoffImpact = {
  关系编号: string
  方向: string
  对端表名: string
  对端库名: string
  匹配字段: string[]
  结论: string
  说明: string
  需补采: boolean
}

export type HandoffApprovalItem = {
  关系编号: string
  方向: string
  对端表名: string
  对端库名?: string
  待改字段?: string[]
  结论: string
}

export type HandoffPackage = {
  包编号: string
  格式版本: string
  生成时间: string
  范围指纹: string
  是否续做: boolean
  已完成步骤: string[]
  本轮执行步骤: string[]
  中断遗留边界数: number
  交接库范围: string[]
  事实表: string
  待删字段: string[]
  事实表所属库: string
  事实表在交接范围: boolean
  关系中转: HandoffRelation[]
  关系边界: HandoffRelation[]
  删除影响: HandoffImpact[]
  审批清单: {
    可处理对象: HandoffApprovalItem[]
    需补采边界: HandoffApprovalItem[]
    确认无依赖: HandoffApprovalItem[]
  }
  统计: Record<string, number>
  问题清单: string[]
}

export type FieldDeletionResult = {
  变更编号: string
  包编号: string
  生成时间: string
  是否续做: boolean
  影响清单: HandoffApprovalItem[]
  需补采的边界: HandoffApprovalItem[]
  确认无依赖: HandoffApprovalItem[]
  同步复核清单条数: number
  交接包问题: string[]
  下载文件名: string
}
