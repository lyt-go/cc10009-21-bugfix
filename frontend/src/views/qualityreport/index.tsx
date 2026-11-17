import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/qualityreport'
const columns = [
  '报告编号',
  '作业编号',
  '提取器编号',
  '作业版本',
  '总URL数',
  '成功URL数',
  '失败URL数',
  '内容完整率',
  '字段覆盖率',
  '质量结论',
  '报告生成时间',
]

type Row = Record<string, string | number | null | undefined | object>
type SnapshotRow = Record<string, string | number | null | undefined | object>

function asText(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—'
  return String(value)
}

function asRate(value: unknown): string {
  if (typeof value !== 'number' || Number.isNaN(value)) return '—'
  return `${(value * 100).toFixed(1)}%`
}

function requestId(): string {
  const rand = typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`
  return `fe-${rand}`
}

const emptyCreateForm = {
  报告编号: '',
  作业编号: '',
  提取器编号: '',
  总URL数: '',
  成功URL数: '',
  失败URL数: '',
  内容完整率: '',
  字段覆盖率: '',
}

const emptyIngestForm = {
  作业编号: '',
  作业版本: '1',
  URL: '',
  抓取结果: '成功',
  失败原因码: '',
}

export default function QualityreportView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [infoMessage, setInfoMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [busyId, setBusyId] = useState<string | null>(null)
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [detailVersion, setDetailVersion] = useState(0)
  const [detail, setDetail] = useState<Row | null>(null)
  const [snapshotDetail, setSnapshotDetail] = useState<SnapshotRow | null>(null)
  const [checklist, setChecklist] = useState<SnapshotRow[]>([])
  const [createForm, setCreateForm] = useState({ ...emptyCreateForm })
  const [ingestForm, setIngestForm] = useState({ ...emptyIngestForm })

  const reload = useCallback(async () => {
    setErrorMessage('')
    const params = new URLSearchParams()
    if (filters['报告编号']) params.set('keyword', filters['报告编号'])
    if (filters['作业编号']) params.set('job_no', filters['作业编号'])
    if (filters['质量结论']) params.set('conclusion', filters['质量结论'])
    try {
      const response = await request(`${ENDPOINT}?${params.toString()}`)
      if (!response.ok) throw new Error('质量报告列表读取失败')
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '采集质量列表读取失败')
    }
  }, [filters])

  const reloadChecklist = useCallback(async () => {
    try {
      const response = await request(`${ENDPOINT}/checklist`)
      if (!response.ok) return
      const payload = await response.json()
      setChecklist(payload.items ?? [])
    } catch {
      // 清单是辅助复核数据，失败时保留上一次结果，不打扰主流程
    }
  }, [])

  useEffect(() => {
    void reload()
    void reloadChecklist()
  }, [reload, reloadChecklist])

  // 切换选中报告或动作执行后重新拉取详情，并清空上一条报告残留的快照复核数据。
  useEffect(() => {
    setSnapshotDetail(null)
    if (selectedId === null) {
      setDetail(null)
      return
    }
    let cancelled = false
    request(`${ENDPOINT}/${selectedId}`)
      .then(async (response) => {
        if (!response.ok) throw new Error('报告详情读取失败')
        const payload = await response.json()
        if (!cancelled) setDetail(payload)
      })
      .catch((error: unknown) => {
        if (!cancelled) setErrorMessage(error instanceof Error ? error.message : '报告详情读取失败')
      })
    return () => {
      cancelled = true
    }
  }, [selectedId, detailVersion])

  // 指标全部由服务端数据计算，切换页面/筛选后不会残留上一次的数字。
  const stats = useMemo(() => {
    const measurable = rows.filter((row) => Number(row['总URL数']) > 0)
    const avgSuccess = measurable.length
      ? measurable.reduce((sum, row) => sum + Number(row['成功URL数']) / Number(row['总URL数']), 0) / measurable.length
      : 0
    const coverages = measurable
      .map((row) => row['字段覆盖率'])
      .filter((value): value is number => typeof value === 'number')
    const avgCoverage = coverages.length
      ? coverages.reduce((sum, value) => sum + value, 0) / coverages.length
      : 0
    return [
      { label: '质量报告总数', value: String(rows.length) },
      { label: '已确认/归档报告', value: String(rows.filter((r) => ['已确认', '已归档'].includes(String(r.status))).length) },
      { label: '平均成功率', value: `${(avgSuccess * 100).toFixed(1)}%` },
      { label: '平均字段覆盖率', value: `${(avgCoverage * 100).toFixed(1)}%` },
    ]
  }, [rows])

  async function runAction(action: string, row: Row) {
    const key = `${row.id}-${action}`
    if (busyId) return
    setErrorMessage('')
    setInfoMessage('')
    setBusyId(key)
    try {
      const response = await request(`${ENDPOINT}/${String(row.id)}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: { action, request_id: requestId() } }),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) throw new Error(payload.message ?? '采集质量动作未生效')
      setInfoMessage(payload.message)
      await reload()
      await reloadChecklist()
      setDetailVersion((v) => v + 1) // 动作后同步刷新已打开的报告详情与快照
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '采集质量操作失败')
    } finally {
      setBusyId(null)
    }
  }

  async function openSnapshot(version: number) {
    if (selectedId === null) return
    setSnapshotDetail(null)
    try {
      const response = await request(`${ENDPOINT}/${selectedId}/snapshots/${version}`)
      if (!response.ok) throw new Error('快照读取失败')
      setSnapshotDetail(await response.json())
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '快照读取失败')
    }
  }

  async function submitCreate(event: React.FormEvent) {
    event.preventDefault()
    setErrorMessage('')
    setInfoMessage('')
    const values: Record<string, string | number> = {}
    for (const [key, value] of Object.entries(createForm)) {
      if (value.trim() === '') continue
      values[key] = ['总URL数', '成功URL数', '失败URL数'].includes(key)
        ? Number(value)
        : ['内容完整率', '字段覆盖率'].includes(key)
          ? Number(value)
          : value
    }
    try {
      const response = await request(ENDPOINT, { method: 'POST', body: JSON.stringify({ values }) })
      const payload = await response.json()
      if (!response.ok || !payload.ok) throw new Error(payload.message ?? '登记失败')
      setInfoMessage(payload.message)
      setCreateForm({ ...emptyCreateForm })
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '登记失败')
    }
  }

  async function submitIngest(event: React.FormEvent) {
    event.preventDefault()
    setErrorMessage('')
    setInfoMessage('')
    try {
      const response = await request(`${ENDPOINT}/url-results`, {
        method: 'POST',
        body: JSON.stringify({
          values: {
            作业编号: ingestForm.作业编号,
            作业版本: Number(ingestForm.作业版本),
            URL: ingestForm.URL,
            抓取结果: ingestForm.抓取结果,
            失败原因码: ingestForm.抓取结果 === '失败' ? ingestForm.失败原因码 || null : null,
          },
        }),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) throw new Error(payload.message ?? '抓取结果登记失败')
      setInfoMessage(payload.message)
      setIngestForm({ ...emptyIngestForm })
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '抓取结果登记失败')
    }
  }

  const actionVisible = (action: string, row: Row): boolean => {
    const s = String(row.status)
    if (action === '生成报告') {
      // 真正在途的生成中不可重复发起；无在途请求的陈旧生成中允许接管重试。
      if (s === '生成中') return row['进行中请求'] === null || row['进行中请求'] === undefined
      return s !== '已确认' && s !== '已归档'
    }
    if (action === '确认报告') return s === '已生成'
    if (action === '归档报告') return s === '已确认'
    return true
  }

  const snapshotList = Array.isArray(detail?.快照) ? (detail!.快照 as SnapshotRow[]) : []
  const conflicts = Array.isArray(detail?.版本冲突) ? (detail!.版本冲突 as Row[]) : []
  const writebackLedger = Array.isArray(detail?.回写流水) ? (detail!.回写流水 as Row[]) : []
  const snapshotUrls = Array.isArray(snapshotDetail?.来源URL结果)
    ? (snapshotDetail!.来源URL结果 as Row[])
    : []
  const driftNotes = Array.isArray(snapshotDetail?.漂移复核)
    ? (snapshotDetail!.漂移复核 as string[])
    : []

  return (
    <section className="page" data-module="qualityreport">
      <header className="page-head">
        <div>
          <h2>采集质量管理</h2>
          <p className="page-desc">
            读取作业 URL 抓取结果与字段提取覆盖率，校验数量关系后冻结指标，生成质量结论与作业质量清单。
          </p>
        </div>
        <div className="page-actions">
          <a className="btn" href={`${ENDPOINT}/export`} target="_blank" rel="noreferrer">
            导出采集质量清单
          </a>
          <a className="btn" href={`${ENDPOINT}/checklist`} target="_blank" rel="noreferrer">
            打开作业质量清单（可书签）
          </a>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {['报告编号', '作业编号', '质量结论'].map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>状态</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)} className={selectedId === row.id ? 'row-selected' : ''}>
              <td>{asText(row['报告编号'])}</td>
              <td>{asText(row['作业编号'])}</td>
              <td>{asText(row['提取器编号'])}</td>
              <td>{row['作业版本'] === null || row['作业版本'] === undefined ? '—' : `v${asText(row['作业版本'])}`}</td>
              <td>{asText(row['总URL数'])}</td>
              <td>{asText(row['成功URL数'])}</td>
              <td>{asText(row['失败URL数'])}</td>
              <td>{asRate(row['内容完整率'] as number)}</td>
              <td>{asRate(row['字段覆盖率'] as number)}</td>
              <td><span className={`badge badge-${String(row['质量结论'])}`}>{asText(row['质量结论'])}</span></td>
              <td>{asText(row['报告生成时间'])}</td>
              <td><span className={`status-tag status-${String(row.status)}`}>{asText(row.status)}</span></td>
              <td className="row-actions">
                {['生成报告', '确认报告', '归档报告']
                  .filter((action) => actionVisible(action, row))
                  .map((action) => (
                    <button
                      key={action}
                      className="link"
                      type="button"
                      disabled={busyId !== null}
                      onClick={() => void runAction(action, row)}
                    >
                      {busyId === `${row.id}-${action}` ? `${action}…` : action}
                    </button>
                  ))}
                <button className="link" type="button" onClick={() => setSelectedId(Number(row.id))}>
                  快照复核
                </button>
                {row['当前快照版本'] !== null && row['当前快照版本'] !== undefined && (
                  <>
                    <a
                      className="link"
                      href={`${ENDPOINT}/${String(row.id)}/conclusion?format=txt`}
                    >
                      下载结论
                    </a>
                    <a
                      className="link"
                      href={`${ENDPOINT}/${String(row.id)}/conclusion`}
                      target="_blank"
                      rel="noreferrer"
                    >
                      书签复核
                    </a>
                  </>
                )}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 2}>暂无采集质量数据，可先在下方登记质量报告</td>
            </tr>
          )}
        </tbody>
      </table>

      <div className="form-grid">
        <form className="panel" onSubmit={submitCreate}>
          <h3>登记质量报告（统计字段随表单持久化落库）</h3>
          <div className="panel-fields">
            {Object.keys(emptyCreateForm).map((field) => (
              <label key={field} className="filter-item">
                <span>{field}</span>
                <input
                  value={createForm[field as keyof typeof createForm]}
                  onChange={(event) => setCreateForm((prev) => ({ ...prev, [field]: event.target.value }))}
                />
              </label>
            ))}
          </div>
          <button className="btn primary" type="submit">登记质量报告</button>
        </form>

        <form className="panel" onSubmit={submitIngest}>
          <h3>登记作业 URL 抓取结果（新一轮抓取/失败 URL 更正入口，幂等）</h3>
          <div className="panel-fields">
            <label className="filter-item">
              <span>作业编号</span>
              <input value={ingestForm.作业编号} onChange={(e) => setIngestForm((p) => ({ ...p, 作业编号: e.target.value }))} />
            </label>
            <label className="filter-item">
              <span>作业版本</span>
              <input type="number" value={ingestForm.作业版本} onChange={(e) => setIngestForm((p) => ({ ...p, 作业版本: e.target.value }))} />
            </label>
            <label className="filter-item">
              <span>URL</span>
              <input value={ingestForm.URL} onChange={(e) => setIngestForm((p) => ({ ...p, URL: e.target.value }))} />
            </label>
            <label className="filter-item">
              <span>抓取结果</span>
              <select value={ingestForm.抓取结果} onChange={(e) => setIngestForm((p) => ({ ...p, 抓取结果: e.target.value }))}>
                <option value="成功">成功</option>
                <option value="失败">失败</option>
              </select>
            </label>
            <label className="filter-item">
              <span>失败原因码</span>
              <input value={ingestForm.失败原因码} onChange={(e) => setIngestForm((p) => ({ ...p, 失败原因码: e.target.value }))} />
            </label>
          </div>
          <button className="btn primary" type="submit">登记抓取结果</button>
        </form>
      </div>

      {selectedId !== null && detail && (
        <section className="panel detail-panel">
          <div className="panel-head">
            <h3>报告详情与快照复核：{asText(detail['报告编号'])}（{asText(detail.status)}）</h3>
            <button className="btn ghost" type="button" onClick={() => setSelectedId(null)}>关闭详情</button>
          </div>
          <p className="panel-meta">
            作业 {asText(detail['作业编号'])} · 提取器 {asText(detail['提取器编号'])} ·
            当前快照 v{asText(detail['当前快照版本'])} · 结论 {asText(detail['质量结论'])} ·
            生成状态 {asText(detail['生成状态'])}
          </p>

          <h4>冻结快照（对外呈现内容均可从快照复核）</h4>
          <table className="data-table sub-table">
            <thead>
              <tr>
                <th>快照版本</th><th>作业版本</th><th>提取器版本</th><th>总/成功/失败</th>
                <th>内容完整率</th><th>字段覆盖率</th><th>质量结论</th><th>确认状态</th><th>操作</th>
              </tr>
            </thead>
            <tbody>
              {snapshotList.map((snap) => (
                <tr key={String(snap['快照版本'])}>
                  <td>v{asText(snap['快照版本'])}</td>
                  <td>v{asText(snap['作业版本'])}</td>
                  <td>v{asText(snap['提取器版本'])}</td>
                  <td>{asText(snap['总URL数'])}/{asText(snap['成功URL数'])}/{asText(snap['失败URL数'])}</td>
                  <td>{asRate(snap['内容完整率'] as number)}</td>
                  <td>{asRate(snap['字段覆盖率'] as number)}</td>
                  <td>{asText(snap['质量结论'])}</td>
                  <td>{asText(snap['确认状态'])}{snap['冻结'] ? '（已冻结）' : ''}</td>
                  <td>
                    <button className="link" type="button" onClick={() => void openSnapshot(Number(snap['快照版本']))}>
                      复核来源与漂移
                    </button>
                  </td>
                </tr>
              ))}
              {snapshotList.length === 0 && (
                <tr><td className="empty-state" colSpan={9}>尚未生成任何快照</td></tr>
              )}
            </tbody>
          </table>

          {snapshotDetail && (
            <div className="snapshot-detail">
              <h4>快照 v{asText(snapshotDetail['快照版本'])} 复核明细</h4>
              <p className="panel-meta">数量校验：{asText((snapshotDetail['校验'] as string[])?.join('；'))}</p>
              {driftNotes.length > 0 ? (
                <ul className="drift-list warn">
                  {driftNotes.map((note) => <li key={note}>{note}</li>)}
                </ul>
              ) : (
                <p className="panel-meta ok">字段覆盖率与引用的提取版本一致，无版本漂移。</p>
              )}
              <table className="data-table sub-table">
                <thead>
                  <tr><th>来源 URL</th><th>抓取结果</th><th>HTTP 状态码</th><th>失败原因码</th></tr>
                </thead>
                <tbody>
                  {snapshotUrls.map((u) => (
                    <tr key={String(u.URL)}>
                      <td>{asText(u.URL)}</td>
                      <td>{asText(u.抓取结果)}</td>
                      <td>{asText(u.HTTP状态码)}</td>
                      <td>{asText(u.失败原因码)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {conflicts.length > 0 && (
            <>
              <h4>版本冲突处置（历史确认结论优先，两组版本依据并存）</h4>
              <ul className="drift-list warn">
                {conflicts.map((c, idx) => (
                  <li key={idx}>
                    历史作业版本 {JSON.stringify(c['历史作业版本'])} → 新一轮 v{asText(c['新一轮作业版本'])}：{asText(c['处置'])}
                  </li>
                ))}
              </ul>
            </>
          )}

          <h4>回写字段提取侧流水</h4>
          <table className="data-table sub-table">
            <thead>
              <tr><th>快照版本</th><th>阶段</th><th>质量结论</th><th>回写时间</th></tr>
            </thead>
            <tbody>
              {writebackLedger.map((item, idx) => (
                <tr key={idx}>
                  <td>v{asText(item['快照版本'])}</td>
                  <td>{asText(item['阶段'])}</td>
                  <td>{asText(item['质量结论'])}</td>
                  <td>{asText(item['回写时间'])}</td>
                </tr>
              ))}
              {writebackLedger.length === 0 && (
                <tr><td className="empty-state" colSpan={4}>暂无回写流水</td></tr>
              )}
            </tbody>
          </table>
        </section>
      )}

      <section className="panel">
        <h3>作业质量清单数据集（快照即依据）</h3>
        <table className="data-table sub-table">
          <thead>
            <tr>
              <th>报告编号</th><th>作业编号</th><th>作业版本</th><th>提取器/版本</th>
              <th>总/成功/失败</th><th>字段覆盖率</th><th>质量结论</th><th>确认状态</th>
            </tr>
          </thead>
          <tbody>
            {checklist.map((item) => (
              <tr key={String(item.id)}>
                <td>{asText(item['报告编号'])}</td>
                <td>{asText(item['作业编号'])}</td>
                <td>v{asText(item['作业版本'])}</td>
                <td>{asText(item['提取器编号'])} v{asText(item['提取器版本'])}</td>
                <td>{asText(item['总URL数'])}/{asText(item['成功URL数'])}/{asText(item['失败URL数'])}</td>
                <td>{asRate(item['字段覆盖率'] as number)}</td>
                <td>{asText(item['质量结论'])}</td>
                <td>{asText(item['确认状态'])}{item['冻结'] ? '（冻结）' : ''}</td>
              </tr>
            ))}
            {checklist.length === 0 && (
              <tr><td className="empty-state" colSpan={8}>暂无清单数据，生成报告后自动产出</td></tr>
            )}
          </tbody>
        </table>
      </section>

      <footer className="page-foot">
        <span>共 {total} 条采集质量记录</span>
        {infoMessage && <span className="info-text">{infoMessage}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
