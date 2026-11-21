import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type {
  ActionResult,
  EntryRow,
  FieldDeletionResult,
  HandoffPackage,
  OverviewResult,
  PageResult,
} from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

async function readError(response: Response, fallback: string): Promise<Error> {
  try {
    const payload = await response.json()
    return new Error(String(payload?.detail || fallback))
  } catch {
    return new Error(fallback)
  }
}

export async function createHandoffPackage(
  table: string,
  databases: string[],
  deletedFields: string[],
  packageId?: string,
): Promise<HandoffPackage> {
  const response = await request('/api/handoff/packages', {
    method: 'POST',
    body: JSON.stringify({
      table,
      databases,
      deleted_fields: deletedFields,
      package_id: packageId ?? null,
    }),
  })
  if (!response.ok) throw await readError(response, '血缘交接包生成失败')
  return (await response.json()) as HandoffPackage
}

export async function loadLatestHandoffPackage(table?: string): Promise<HandoffPackage | null> {
  const query = table ? `?${new URLSearchParams({ table }).toString()}` : ''
  const response = await request(`/api/handoff/packages/latest${query}`)
  if (response.status === 404) return null
  if (!response.ok) throw new Error('血缘交接包读取失败')
  return (await response.json()) as HandoffPackage
}

export async function submitFieldDeletion(
  table: string,
  databases: string[],
  deletedFields: string[],
): Promise<FieldDeletionResult> {
  const response = await request('/api/handoff/field-deletions', {
    method: 'POST',
    body: JSON.stringify({ table, databases, deleted_fields: deletedFields }),
  })
  if (!response.ok) throw await readError(response, '字段删除影响核对失败')
  return (await response.json()) as FieldDeletionResult
}

/** 下载交接包：JSON 为可回放材料，CSV 便于对照关系与影响清单。 */
export async function downloadHandoffPackage(packageId: string, format: 'json' | 'csv'): Promise<void> {
  const suffix = format === 'csv' ? '/download.csv' : '/download'
  const response = await request(`/api/handoff/packages/${encodeURIComponent(packageId)}${suffix}`)
  if (!response.ok) throw new Error('交接包下载失败')
  const blob = await response.blob()
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${packageId}.${format}`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}
