import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/contentextract'
const columns = [
  '提取器编号',
  '内容类型',
  '正文选择器',
  '关联作业编号',
  '提取器版本',
  '字段覆盖率',
  '最新质量结论',
  '最近质量回写',
]

type Row = Record<string, string | number | null | undefined | object>

function asText(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—'
  if (typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

function asRate(value: unknown): string {
  if (typeof value !== 'number' || Number.isNaN(value)) return '—'
  return `${(value * 100).toFixed(1)}%`
}

export default function ContentextractView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [infoMessage, setInfoMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [coverageTarget, setCoverageTarget] = useState<Row | null>(null)
  const [coverageValue, setCoverageValue] = useState('')

  const reload = useCallback(async () => {
    setErrorMessage('')
    const params = new URLSearchParams()
    if (filters['提取器编号']) params.set('keyword', filters['提取器编号'])
    if (filters['关联作业编号']) params.set('job_no', filters['关联作业编号'])
    try {
      const response = await request(`${ENDPOINT}?${params.toString()}`)
      if (!response.ok) throw new Error('提取器列表读取失败')
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '内容提取列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  // 指标由服务端数据计算，与采集质量页面切换后不会出现字段覆盖率错位。
  const stats = useMemo(() => {
    const coverages = rows
      .map((row) => row['字段覆盖率'])
      .filter((value): value is number => typeof value === 'number')
    const avg = coverages.length ? coverages.reduce((s, v) => s + v, 0) / coverages.length : 0
    return [
      { label: '提取器总数', value: String(rows.length) },
      { label: '已发布提取器', value: String(rows.filter((r) => r.status === '已发布').length) },
      { label: '评审中提取器', value: String(rows.filter((r) => r.status === '提取器评审中').length) },
      { label: '平均字段覆盖率', value: `${(avg * 100).toFixed(1)}%` },
    ]
  }, [rows])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setInfoMessage('')
    try {
      const response = await request(`${ENDPOINT}/${String(row.id)}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: { action } }),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) throw new Error(payload.message ?? '内容提取动作未生效')
      setInfoMessage(payload.message)
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '内容提取操作失败')
    }
  }

  async function submitCoverage(event: React.FormEvent) {
    event.preventDefault()
    if (!coverageTarget) return
    setErrorMessage('')
    setInfoMessage('')
    const numeric = Number(coverageValue)
    if (Number.isNaN(numeric) || numeric < 0 || numeric > 1) {
      setErrorMessage('字段覆盖率必须是 0~1 之间的数值（例如 0.85）')
      return
    }
    try {
      const response = await request(`${ENDPOINT}/${String(coverageTarget.id)}/coverage`, {
        method: 'POST',
        body: JSON.stringify({
          values: { 字段覆盖率: numeric, request_id: `fe-cov-${Date.now()}` },
        }),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) throw new Error(payload.message ?? '覆盖率写入失败')
      setInfoMessage(payload.message)
      setCoverageTarget(null)
      setCoverageValue('')
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '覆盖率写入失败')
    }
  }

  const actions = ['提交提取器评审', '发布提取器', '废弃提取器']

  return (
    <section className="page" data-module="contentextract">
      <header className="page-head">
        <div>
          <h2>内容提取管理</h2>
          <p className="page-desc">
            管理结构化提取器与其版本化字段覆盖率；质量结论与作业质量清单确认后会回写到本清单。
          </p>
        </div>
        <div className="page-actions">
          <a className="btn" href={`${ENDPOINT}/export`} target="_blank" rel="noreferrer">
            导出内容提取清单
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
        {['提取器编号', '关联作业编号'].map((field) => (
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
          {rows.map((row) => {
            const writeback = row['最近质量回写'] as Record<string, unknown> | null | undefined
            const ledger = Array.isArray(row['质量回写流水']) ? (row['质量回写流水'] as Row[]) : []
            return (
              <tr key={String(row.id)}>
                <td>{asText(row['提取器编号'])}</td>
                <td>{asText(row['内容类型'])}</td>
                <td>{asText(row['正文选择器'])}</td>
                <td>{asText(row['关联作业编号'])}</td>
                <td>v{asText(row['提取器版本'])}</td>
                <td>{asRate(row['字段覆盖率'] as number)}</td>
                <td><span className={`badge badge-${String(row['最新质量结论'])}`}>{asText(row['最新质量结论'])}</span></td>
                <td>
                  {writeback
                    ? `${asText(writeback['报告编号'])} v${asText(writeback['快照版本'])} · ${asText(writeback['阶段'])}`
                    : '—'}
                  {ledger.length > 0 && <span className="ledger-count">（流水 {ledger.length} 条）</span>}
                </td>
                <td><span className={`status-tag status-${String(row.status)}`}>{asText(row.status)}</span></td>
                <td className="row-actions">
                  {actions.map((action) => (
                    <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                      {action}
                    </button>
                  ))}
                  <button className="link" type="button" onClick={() => { setCoverageTarget(row); setCoverageValue('') }}>
                    写入覆盖率
                  </button>
                </td>
              </tr>
            )
          })}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 2}>暂无内容提取数据</td>
            </tr>
          )}
        </tbody>
      </table>

      {coverageTarget && (
        <form className="panel" onSubmit={submitCoverage}>
          <div className="panel-head">
            <h3>写入字段覆盖率：{asText(coverageTarget['提取器编号'])} v{asText(coverageTarget['提取器版本'])}</h3>
            <button className="btn ghost" type="button" onClick={() => setCoverageTarget(null)}>取消</button>
          </div>
          <p className="panel-meta">
            若该提取版本已被已确认/已归档质量快照冻结，写入会被拒绝——旧指标不能写进已确认版本，需升版提取器。
          </p>
          <label className="filter-item">
            <span>字段覆盖率（0~1）</span>
            <input
              autoFocus
              value={coverageValue}
              placeholder="例如 0.85"
              onChange={(event) => setCoverageValue(event.target.value)}
            />
          </label>
          <button className="btn primary" type="submit">提交覆盖率</button>
        </form>
      )}

      <footer className="page-foot">
        <span>共 {total} 条内容提取记录</span>
        {infoMessage && <span className="info-text">{infoMessage}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
