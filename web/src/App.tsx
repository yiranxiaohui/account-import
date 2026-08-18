import { useEffect, useMemo, useState } from 'react'
import type { FormEvent } from 'react'
import {
  createImportJob,
  createRecover401Job,
  getHealth,
  getJob,
  getLocalAccountRecords,
  getSub2APIConfig,
  getSub2APIOptions,
  saveSub2APIConfig,
  scanSub2API401Accounts,
} from './api'
import type {
  ImportJobRequest,
  JobSnapshot,
  LocalAccountList,
  Sub2API401Scan,
  Sub2APIConfig,
  Sub2APIGroupOption,
  Sub2APIProxyOption,
} from './types'
import './App.css'

const TERMINAL_STATUSES = new Set(['succeeded', 'partial', 'failed'])
const STORAGE_KEYS = {
  redeemUrl: 'account-import:redeem-url',
  sub2apiUrl: 'account-import:sub2api-url',
} as const
const LEGACY_STORAGE_KEYS = {
  redeemUrl: 'team-import:redeem-url',
  sub2apiUrl: 'team-import:sub2api-url',
} as const

function readStoredValue(key: string, legacyKey: string): string | null {
  const current = localStorage.getItem(key)
  if (current !== null) return current
  const legacy = localStorage.getItem(legacyKey)
  if (legacy !== null) {
    localStorage.setItem(key, legacy)
    localStorage.removeItem(legacyKey)
  }
  return legacy
}

const IMPORT_STAGES = [
  { key: 'checking', label: '校验额度', caption: '确认兑换码状态' },
  { key: 'downloading', label: '下载凭据', caption: '找回并合并账号' },
  { key: 'importing', label: '导入 Sub2API', caption: '创建可用账号' },
]

const RECOVERY_STAGES = [
  { key: 'checking', label: '扫描 401', caption: '读取 Sub2API 失效状态' },
  { key: 'downloading', label: '找回凭据', caption: '按兑换码下载新额度' },
  { key: 'importing', label: '更新账号', caption: '原地替换并清除错误' },
]

type ViewKey = 'workspace' | 'records' | 'settings'
type TaskMode = 'import' | 'recovery'

const NAV_ITEMS: Array<{ key: ViewKey; label: string; caption: string }> = [
  { key: 'workspace', label: '任务工作台', caption: '导入与 401 找回' },
  { key: 'records', label: '本地记录', caption: 'SQLite 账号账本' },
  { key: 'settings', label: '系统配置', caption: 'Sub2API 连接配置' },
]

const VIEW_META: Record<ViewKey, { eyebrow: string; title: string; accent: string; description: string }> = {
  workspace: {
    eyebrow: 'REDEEM · RECOVER · DELIVER',
    title: '任务工作台',
    accent: '凭据交付',
    description: '在额度导入与 401 自动找回之间切换，任务参数互不干扰。',
  },
  records: {
    eyebrow: 'LOCAL · SQLITE · AUDIT',
    title: '账号记录',
    accent: '本地留痕',
    description: '查看已导入和已恢复账号，不保存任何 Token 或下载凭据。',
  },
  settings: {
    eyebrow: 'TARGET · SECURITY · PERSISTENCE',
    title: '连接配置',
    accent: '安全持久化',
    description: '管理 Sub2API 地址、管理员凭据、默认分组和 TLS。',
  },
}

function NavigationIcon({ view }: { view: ViewKey }) {
  if (view === 'workspace') {
    return <svg viewBox="0 0 24 24"><path d="M5 7h10M12 4l3 3-3 3M19 17H9M12 14l-3 3 3 3" /></svg>
  }
  if (view === 'records') {
    return <svg viewBox="0 0 24 24"><ellipse cx="12" cy="6" rx="7" ry="3" /><path d="M5 6v6c0 1.7 3.1 3 7 3s7-1.3 7-3V6M5 12v6c0 1.7 3.1 3 7 3s7-1.3 7-3v-6" /></svg>
  }
  return <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3" /><path d="M19 13.5v-3l-2-.7-.7-1.7.9-1.9-2.1-2.1-1.9.9-1.7-.7L10.5 2h-3l-.7 2-1.7.7-1.9-.9-2.1 2.1.9 1.9-.7 1.7L0 10.5v3l2 .7.7 1.7-.9 1.9 2.1 2.1 1.9-.9 1.7.7.7 2.3h3l.7-2 1.7-.7 1.9.9 2.1-2.1-.9-1.9.7-1.7z" transform="translate(1.5 0) scale(.87)" /></svg>
}

function parseCardCodes(source: string): string[] {
  return [...new Set(source.split(/[\s,，;；]+/).map((item) => item.trim()).filter(Boolean))]
}

function compactUrl(value: string): string {
  return value.trim().replace(/\/+$/, '')
}

function stagePosition(stage: string): number {
  if (stage === 'queued') return -1
  if (stage === 'checking') return 0
  if (stage === 'redeeming' || stage === 'downloading') return 1
  if (stage === 'importing') return 2
  if (stage === 'completed') return 3
  return -1
}

function StatCard({ label, value, accent }: { label: string; value: number | string; accent?: string }) {
  return (
    <div className="stat-card">
      <span>{label}</span>
      <strong style={{ color: accent }}>{value}</strong>
    </div>
  )
}

function EyeIcon({ open }: { open: boolean }) {
  return open ? (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M3 3l18 18M10.6 10.7a2 2 0 002.7 2.7M9.9 4.3A10.8 10.8 0 0112 4c5.5 0 9 5 9 5a16 16 0 01-2.1 2.6M6.6 6.6C4.4 8 3 10 3 10s3.5 5 9 5c1 0 2-.2 2.8-.4" />
    </svg>
  ) : (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M3 12s3.5-5 9-5 9 5 9 5-3.5 5-9 5-9-5-9-5z" />
      <circle cx="12" cy="12" r="2.4" />
    </svg>
  )
}

function App() {
  const [activeView, setActiveView] = useState<ViewKey>('workspace')
  const [taskMode, setTaskMode] = useState<TaskMode>('import')
  const [redeemBaseUrl, setRedeemBaseUrl] = useState(
    () => {
      const savedUrl = readStoredValue(STORAGE_KEYS.redeemUrl, LEGACY_STORAGE_KEYS.redeemUrl)
      return !savedUrl || savedUrl === 'https://xx1xx.team'
        ? 'https://30d.team/'
        : savedUrl
    },
  )
  const [sub2apiBaseUrl, setSub2apiBaseUrl] = useState(
    () => readStoredValue(STORAGE_KEYS.sub2apiUrl, LEGACY_STORAGE_KEYS.sub2apiUrl) || 'http://localhost:8080',
  )
  const [token, setToken] = useState('')
  const [showToken, setShowToken] = useState(false)
  const [cardInput, setCardInput] = useState('')
  const [mode, setMode] = useState<'all' | '401'>('all')
  const [verifyTls, setVerifyTls] = useState(true)
  const [groupId, setGroupId] = useState<number | null>(null)
  const [proxyId, setProxyId] = useState<number | null>(null)
  const [groupOptions, setGroupOptions] = useState<Sub2APIGroupOption[]>([])
  const [proxyOptions, setProxyOptions] = useState<Sub2APIProxyOption[]>([])
  const [loadingOptions, setLoadingOptions] = useState(false)
  const [recoveryScan, setRecoveryScan] = useState<Sub2API401Scan | null>(null)
  const [scanning401, setScanning401] = useState(false)
  const [localAccounts, setLocalAccounts] = useState<LocalAccountList>({ total: 0, items: [] })
  const [loadingLocalAccounts, setLoadingLocalAccounts] = useState(true)
  const [configLoaded, setConfigLoaded] = useState(false)
  const [configDirty, setConfigDirty] = useState(false)
  const [hasSavedToken, setHasSavedToken] = useState(false)
  const [configUpdatedAt, setConfigUpdatedAt] = useState<string | null>(null)
  const [savingConfig, setSavingConfig] = useState(false)
  const [apiOnline, setApiOnline] = useState<boolean | null>(null)
  const [sdkVersion, setSdkVersion] = useState('')
  const [job, setJob] = useState<JobSnapshot | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [formError, setFormError] = useState('')

  const codes = useMemo(() => parseCardCodes(cardInput), [cardInput])
  const running = submitting || savingConfig || scanning401 || (!!job && !TERMINAL_STATUSES.has(job.status))
  const currentStage = job ? stagePosition(job.stage) : -1
  const isRecoveryJob = job?.summary.operation === 'recover_401'
  const stages = isRecoveryJob ? RECOVERY_STAGES : IMPORT_STAGES
  const activeJobId = job?.id
  const activeJobStatus = job?.status

  useEffect(() => {
    getHealth()
      .then((result) => {
        setApiOnline(true)
        setSdkVersion(result.sdk_version)
      })
      .catch(() => setApiOnline(false))

    getLocalAccountRecords()
      .then(setLocalAccounts)
      .catch((error) => {
        setFormError(error instanceof Error ? error.message : '读取本地账号记录失败')
      })
      .finally(() => setLoadingLocalAccounts(false))

    getSub2APIConfig()
      .then(async (config) => {
        if (config.configured && config.base_url) {
          setSub2apiBaseUrl(config.base_url)
          localStorage.removeItem(STORAGE_KEYS.sub2apiUrl)
          localStorage.removeItem(LEGACY_STORAGE_KEYS.sub2apiUrl)
        }
        setHasSavedToken(config.has_token)
        setVerifyTls(config.verify_tls)
        setGroupId(config.group_id ?? null)
        setConfigUpdatedAt(config.updated_at || null)
        setConfigDirty(!config.configured)
        if (config.has_token) {
          setLoadingOptions(true)
          try {
            const options = await getSub2APIOptions()
            setGroupOptions(options.groups)
            setProxyOptions(options.proxies)
          } catch (error) {
            setFormError(error instanceof Error ? error.message : '读取分组和代理失败')
          } finally {
            setLoadingOptions(false)
          }
        }
      })
      .catch((error) => {
        setFormError(error instanceof Error ? error.message : '读取 Sub2API 配置失败')
      })
      .finally(() => setConfigLoaded(true))
  }, [])

  useEffect(() => {
    if (!activeJobId || (activeJobStatus && TERMINAL_STATUSES.has(activeJobStatus))) return
    let cancelled = false
    const poll = async () => {
      try {
        const next = await getJob(activeJobId)
        if (!cancelled) setJob(next)
      } catch (error) {
        if (!cancelled) setFormError(error instanceof Error ? error.message : '读取任务进度失败')
      }
    }
    const timer = window.setInterval(poll, 1800)
    void poll()
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [activeJobId, activeJobStatus])

  useEffect(() => {
    if (!activeJobStatus || !TERMINAL_STATUSES.has(activeJobStatus)) return
    getLocalAccountRecords()
      .then(setLocalAccounts)
      .catch(() => undefined)
  }, [activeJobStatus])

  const applySavedConfig = (config: Sub2APIConfig) => {
    setHasSavedToken(config.has_token)
    setGroupId(config.group_id ?? null)
    setConfigUpdatedAt(config.updated_at || null)
    setConfigDirty(false)
    setToken('')
  }

  const persistSub2APIConfig = async (): Promise<Sub2APIConfig> => {
    if (!compactUrl(sub2apiBaseUrl)) {
      throw new Error('请输入 Sub2API 地址')
    }
    if (!hasSavedToken && !token.trim()) {
      throw new Error('首次保存时请输入 Sub2API 管理员 API Key 或 Access Token')
    }
    const config = await saveSub2APIConfig({
      base_url: compactUrl(sub2apiBaseUrl),
      access_token: token.trim() || undefined,
      verify_tls: verifyTls,
      group_id: groupId,
    })
    applySavedConfig(config)
    return config
  }

  const handleSaveConfig = async () => {
    setActiveView('settings')
    setFormError('')
    setSavingConfig(true)
    try {
      if (configDirty || token.trim() || !hasSavedToken) {
        await persistSub2APIConfig()
      }
      setLoadingOptions(true)
      try {
        const options = await getSub2APIOptions()
        setGroupOptions(options.groups)
        setProxyOptions(options.proxies)
      } catch (error) {
        throw new Error(`配置已保存，但读取分组和代理失败：${error instanceof Error ? error.message : '未知错误'}`)
      } finally {
        setLoadingOptions(false)
      }
    } catch (error) {
      setFormError(error instanceof Error ? error.message : '保存 Sub2API 配置失败')
    } finally {
      setSavingConfig(false)
    }
  }

  const handleSubmit = async (event: FormEvent) => {
    event.preventDefault()
    setActiveView('workspace')
    setTaskMode('import')
    setFormError('')
    if (!codes.length) {
      setFormError('请先粘贴至少一个兑换码')
      return
    }
    if (codes.length > 100) {
      setFormError('单次最多处理 100 个兑换码')
      return
    }
    localStorage.setItem(STORAGE_KEYS.redeemUrl, compactUrl(redeemBaseUrl))
    const payload: ImportJobRequest = {
      redeem_base_url: compactUrl(redeemBaseUrl),
      card_codes: codes,
      mode,
      proxy_id: proxyId,
    }

    setSubmitting(true)
    setJob(null)
    try {
      if (configDirty || token.trim() || !hasSavedToken) {
        await persistSub2APIConfig()
      }
      const result = await createImportJob(payload)
      setJob(result.job)
    } catch (error) {
      setFormError(error instanceof Error ? error.message : '创建任务失败')
    } finally {
      setSubmitting(false)
    }
  }

  const handleScan401 = async () => {
    setActiveView('workspace')
    setTaskMode('recovery')
    setFormError('')
    setScanning401(true)
    try {
      if (configDirty || token.trim() || !hasSavedToken) {
        await persistSub2APIConfig()
      }
      const result = await scanSub2API401Accounts()
      setRecoveryScan(result)
    } catch (error) {
      setFormError(error instanceof Error ? error.message : '扫描 Sub2API 401 账号失败')
    } finally {
      setScanning401(false)
    }
  }

  const handleRecover401 = async () => {
    setActiveView('workspace')
    setTaskMode('recovery')
    const accountIds = recoveryScan?.accounts
      .filter((account) => Boolean(account.card_code))
      .map((account) => account.id) ?? []
    if (!accountIds.length) {
      setFormError('当前扫描结果中没有带兑换码的 401 账号')
      return
    }

    setFormError('')
    setSubmitting(true)
    setJob(null)
    try {
      if (configDirty || token.trim() || !hasSavedToken) {
        await persistSub2APIConfig()
      }
      const result = await createRecover401Job({
        redeem_base_url: compactUrl(redeemBaseUrl),
        account_ids: accountIds,
      })
      setJob(result.job)
    } catch (error) {
      setFormError(error instanceof Error ? error.message : '创建 401 找回任务失败')
    } finally {
      setSubmitting(false)
    }
  }

  const handleRefreshLocalAccounts = async () => {
    setActiveView('records')
    setFormError('')
    setLoadingLocalAccounts(true)
    try {
      setLocalAccounts(await getLocalAccountRecords())
    } catch (error) {
      setFormError(error instanceof Error ? error.message : '读取本地账号记录失败')
    } finally {
      setLoadingLocalAccounts(false)
    }
  }

  const resetTask = () => {
    setJob(null)
    setFormError('')
  }

  const health = job?.summary.health
  const download = job?.summary.download
  const imported = job?.summary.import
  const recovery = job?.summary.recovery
  const jobScan = job?.summary.scan
  const importedCount = Number(imported?.account_created || 0)
  const failedCount = Number(imported?.account_failed || 0) + Number(download?.failed || 0)
  const recoveredCount = Number(recovery?.updated || 0)
  const recoveryFailedCount = Number(recovery?.failed || 0)

  return (
    <div className="app-shell">
      <div className="ambient ambient-one" />
      <div className="ambient ambient-two" />

      <aside className="app-sidebar">
        <div className="brand sidebar-brand">
          <div className="brand-mark" aria-hidden="true">
            <svg viewBox="0 0 32 32">
              <path d="M7 9.5h11.5a5.5 5.5 0 010 11H14" />
              <path d="M11 6l-4 3.5 4 3.5M21 19l4 3.5-4 3.5" />
            </svg>
          </div>
          <div>
            <strong>Account Import</strong>
            <span>额度交付工作台</span>
          </div>
        </div>

        <nav className="sidebar-nav" aria-label="主导航">
          <span className="sidebar-label">工作台</span>
          {NAV_ITEMS.map((item) => (
            <button
              type="button"
              className={activeView === item.key ? 'active' : ''}
              key={item.key}
              onClick={() => {
                setActiveView(item.key)
                setFormError('')
              }}
            >
              <span className="nav-icon"><NavigationIcon view={item.key} /></span>
              <span className="nav-copy"><strong>{item.label}</strong><small>{item.caption}</small></span>
              {item.key === 'workspace' && Boolean(recoveryScan?.recoverable) && <span className="nav-badge">{recoveryScan?.recoverable}</span>}
              {item.key === 'records' && localAccounts.total > 0 && (
                <span className="nav-badge">{localAccounts.total}</span>
              )}
              {item.key === 'settings' && configDirty && <span className="nav-dot" />}
            </button>
          ))}
        </nav>

        <div className="sidebar-footer">
          <div className={`sidebar-health ${apiOnline === false ? 'is-offline' : ''}`}><i /><span>{apiOnline === false ? '后端离线' : '服务在线'}</span></div>
          <small>SQLite 本地持久化</small>
        </div>
      </aside>

      <div className="workspace-shell">
        <header className="topbar">
          <div className="page-context">
            <span>ACCOUNT IMPORT /</span>
            <strong>{VIEW_META[activeView].title}</strong>
          </div>
          <div className={`service-status ${apiOnline === false ? 'is-offline' : ''}`}>
            <i />
            {apiOnline === null ? '正在连接' : apiOnline ? `服务正常${sdkVersion ? ` · SDK ${sdkVersion}` : ''}` : '后端离线'}
          </div>
        </header>

      <main className="workspace-main">
        <section className="hero-copy">
          <div className="eyebrow"><span /> {VIEW_META[activeView].eyebrow}</div>
          <h1>{VIEW_META[activeView].title}，<br /><em>{VIEW_META[activeView].accent}</em></h1>
          <p>{VIEW_META[activeView].description}</p>
        </section>

        <div className={`workflow-grid view-${activeView}`}>
          <form
            className={`panel form-panel page-panel task-${taskMode}`}
            onSubmit={(event) => {
              if (activeView === 'workspace' && taskMode === 'import') handleSubmit(event)
              else event.preventDefault()
            }}
          >
            <div className="panel-heading">
              <div>
                <span className="panel-index">{activeView === 'workspace' ? '01' : activeView === 'records' ? '02' : '03'}</span>
                <div>
                  <h2>{activeView === 'workspace' ? '凭据任务' : activeView === 'records' ? '本地账号账本' : 'Sub2API 配置'}</h2>
                  <p>{activeView === 'workspace' ? '选择额度导入或 401 自动找回' : activeView === 'records' ? '查看已导入和已恢复的账号' : '连接实例并设置默认分组'}</p>
                </div>
              </div>
              {activeView === 'settings' && (
                <span className={`secure-chip ${configDirty ? 'is-dirty' : 'is-saved'}`}>
                  <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 10V8a5 5 0 0110 0v2M6 10h12v10H6z" /></svg>
                  {configLoaded ? (configDirty ? '配置待保存' : '配置已保存') : '正在读取配置'}
                </span>
              )}
            </div>

            <div className="task-mode-switch workspace-only">
              <button
                type="button"
                className={taskMode === 'import' ? 'active' : ''}
                onClick={() => {
                  setTaskMode('import')
                  setFormError('')
                  if (job && TERMINAL_STATUSES.has(job.status)) setJob(null)
                }}
                disabled={running}
              >
                <strong>额度导入</strong>
                <span>粘贴兑换码，下载并创建账号</span>
              </button>
              <button
                type="button"
                className={taskMode === 'recovery' ? 'active' : ''}
                onClick={() => {
                  setTaskMode('recovery')
                  setFormError('')
                  if (job && TERMINAL_STATUSES.has(job.status)) setJob(null)
                }}
                disabled={running}
              >
                <strong>401 找回</strong>
                <span>扫描失效账号并原地更新凭据</span>
              </button>
            </div>

            <div className="field-grid address-grid">
              <label className="field workspace-only">
                <span>兑换服务地址</span>
                <div className="input-wrap">
                  <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8" /><path d="M4 12h16M12 4a13 13 0 010 16M12 4a13 13 0 000 16" /></svg>
                  <input type="url" value={redeemBaseUrl} onChange={(e) => setRedeemBaseUrl(e.target.value)} placeholder="https://redeem.example.com" disabled={running} required={activeView === 'workspace'} />
                </div>
              </label>
              <label className="field settings-only">
                <span>Sub2API 地址</span>
                <div className="input-wrap">
                  <svg viewBox="0 0 24 24" aria-hidden="true"><rect x="4" y="5" width="16" height="14" rx="3" /><path d="M8 9h8M8 13h5" /></svg>
                  <input
                    type="url"
                    value={sub2apiBaseUrl}
                    onChange={(e) => {
                      setSub2apiBaseUrl(e.target.value)
                      setConfigDirty(true)
                      setRecoveryScan(null)
                    }}
                    placeholder="http://localhost:8080"
                    disabled={running || !configLoaded}
                    required={activeView === 'settings'}
                  />
                </div>
              </label>
            </div>

            <label className="field settings-only">
              <span>管理员 API Key / Access Token</span>
              <div className="input-wrap token-input">
                <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="8" cy="12" r="4" /><path d="M12 12h8M17 12v3M20 12v2" /></svg>
                <input
                  type={showToken ? 'text' : 'password'}
                  value={token}
                  onChange={(e) => {
                    setToken(e.target.value)
                    setConfigDirty(true)
                    setRecoveryScan(null)
                  }}
                  placeholder={hasSavedToken ? '已保存；留空表示继续使用原凭据' : '粘贴管理员 API Key（推荐）或 Access Token'}
                  autoComplete="off"
                  disabled={running || !configLoaded}
                  required={activeView === 'settings' && !hasSavedToken}
                />
                <button type="button" className="icon-button" onClick={() => setShowToken((value) => !value)} aria-label={showToken ? '隐藏令牌' : '显示令牌'}>
                  <EyeIcon open={showToken} />
                </button>
              </div>
              <small>{hasSavedToken ? '服务端已保存凭据；输入新凭据并保存即可更新。' : '推荐使用 admin- 开头的管理员 API Key；凭据将写入权限为 0600 的服务端配置文件，不会回传到浏览器。'}</small>
            </label>

            <div className="field import-only">
              <div className="field-label-row">
                <span>兑换范围</span>
                <span className="recommended">推荐下载全部</span>
              </div>
              <div className="mode-selector">
                <label className={mode === 'all' ? 'selected' : ''}>
                  <input type="radio" name="mode" value="all" checked={mode === 'all'} onChange={() => setMode('all')} disabled={running} />
                  <i><span /></i>
                  <span><strong>下载全部额度</strong><small>刷新并下载兑换码下全部账号</small></span>
                </label>
                <label className={mode === '401' ? 'selected' : ''}>
                  <input type="radio" name="mode" value="401" checked={mode === '401'} onChange={() => setMode('401')} disabled={running} />
                  <i><span /></i>
                  <span><strong>只找回 401</strong><small>仅处理凭据已失效的账号</small></span>
                </label>
              </div>
            </div>

            <label className="field import-only">
              <div className="field-label-row">
                <span>本次导入代理</span>
                <span className="recommended">仅用于当前任务</span>
              </div>
              <div className="select-wrap">
                <select
                  value={proxyId ?? ''}
                  onChange={(event) => setProxyId(event.target.value ? Number(event.target.value) : null)}
                  disabled={running || !configLoaded || loadingOptions}
                >
                  <option value="">不使用代理（直连）</option>
                  {proxyOptions.map((proxy) => (
                    <option value={proxy.id} key={proxy.id}>
                      {proxy.name} · {proxy.protocol}://{proxy.host}:{proxy.port}
                    </option>
                  ))}
                </select>
              </div>
              <small>{loadingOptions ? '正在读取 Sub2API 代理…' : `已读取 ${proxyOptions.length} 个可用代理；选择不会保存为全局配置`}</small>
            </label>

            <label className="field code-field import-only">
              <div className="field-label-row">
                <span>兑换码</span>
                <span className={`code-count ${codes.length > 100 ? 'over-limit' : ''}`}>{codes.length} / 100</span>
              </div>
              <textarea value={cardInput} onChange={(e) => setCardInput(e.target.value)} placeholder={'每行一个兑换码，也支持空格或逗号分隔\n\nRCL-XXXX-XXXX\nRCL-YYYY-YYYY'} spellCheck={false} disabled={running} />
              <div className="textarea-foot">
                <span>自动去重 · 每 20 个分为一批</span>
                {cardInput && <button type="button" onClick={() => setCardInput('')} disabled={running}>清空</button>}
              </div>
            </label>

            <div className="destination-grid single-destination settings-only">
              <label className="field">
                <span>目标分组</span>
                <div className="select-wrap">
                  <select
                    value={groupId ?? ''}
                    onChange={(event) => {
                      setGroupId(event.target.value ? Number(event.target.value) : null)
                      setConfigDirty(true)
                    }}
                    disabled={running || !configLoaded || loadingOptions}
                  >
                    <option value="">不绑定分组</option>
                    {groupId !== null && !groupOptions.some((group) => group.id === groupId) && (
                      <option value={groupId}>已保存分组 #{groupId}（当前列表中不可用）</option>
                    )}
                    {groupOptions.map((group) => (
                      <option value={group.id} key={group.id}>
                        {group.name}{group.platform ? ` · ${group.platform}` : ''}
                      </option>
                    ))}
                  </select>
                </div>
                <small>{loadingOptions ? '正在读取 Sub2API 分组…' : `已读取 ${groupOptions.length} 个可用分组`}</small>
              </label>
            </div>

            <div className="options-row single-option settings-only">
              <label className="toggle-row">
                <input
                  type="checkbox"
                  checked={verifyTls}
                  onChange={(e) => {
                    setVerifyTls(e.target.checked)
                    setConfigDirty(true)
                  }}
                  disabled={running || !configLoaded}
                />
                <i><span /></i>
                <span><strong>校验 HTTPS 证书</strong><small>自签名内网实例可关闭</small></span>
              </label>
              <div className="naming-rule">
                <strong>账号写入规则</strong>
                <span>名称 = 邮箱 · 备注 = 兑换码</span>
              </div>
            </div>

            <div className="config-save-row settings-only">
              <span className={configDirty ? 'dirty' : 'saved'}>
                <i />
                {configDirty
                  ? 'Sub2API 配置有未保存的更改'
                  : configUpdatedAt
                    ? `已持久化 · ${new Date(configUpdatedAt).toLocaleString('zh-CN', { hour12: false })}`
                    : 'Sub2API 配置已持久化'}
              </span>
              <button type="button" onClick={handleSaveConfig} disabled={running || loadingOptions || !configLoaded}>
                {savingConfig ? '保存中…' : loadingOptions ? '加载中…' : configDirty || token.trim() ? '保存并刷新' : '刷新选项'}
              </button>
            </div>

            <section className="recovery-tool recovery-only">
              <div className="recovery-tool-heading">
                <div>
                  <span>401 自动找回</span>
                  <small>读取 Sub2API 已记录的 401，按备注中的兑换码原地更新凭据</small>
                </div>
                <button
                  type="button"
                  className="scan-button"
                  onClick={handleScan401}
                  disabled={running || !configLoaded || apiOnline === false}
                >
                  {scanning401 ? '扫描中…' : recoveryScan ? '重新扫描' : '扫描 401'}
                </button>
              </div>
              {recoveryScan && (
                <div className="recovery-scan-result">
                  <div className="recovery-metrics">
                    <span><strong>{recoveryScan.scanned}</strong>账号总数</span>
                    <span><strong>{recoveryScan.detected_401}</strong>检测到 401</span>
                    <span className="recoverable"><strong>{recoveryScan.recoverable}</strong>可自动找回</span>
                    <span className={recoveryScan.missing_card_code ? 'warning' : ''}>
                      <strong>{recoveryScan.missing_card_code}</strong>缺少兑换码
                    </span>
                  </div>
                  <div className="recovery-action-row">
                    <span>涉及 {recoveryScan.unique_codes} 个兑换码；更新时保留原分组、代理和账号 ID。</span>
                    <button
                      type="button"
                      onClick={handleRecover401}
                      disabled={running || recoveryScan.recoverable === 0}
                    >
                      找回并更新 {recoveryScan.recoverable} 个账号
                    </button>
                  </div>
                </div>
              )}
            </section>

            <section className="local-ledger records-only">
              <div className="local-ledger-heading">
                <div>
                  <span>本地账号记录</span>
                  <small>SQLite 已记录 {localAccounts.total} 个账号，不保存 Token 或凭据</small>
                </div>
                <button
                  type="button"
                  onClick={handleRefreshLocalAccounts}
                  disabled={loadingLocalAccounts}
                >
                  {loadingLocalAccounts ? '读取中…' : '刷新'}
                </button>
              </div>
              {localAccounts.items.length ? (
                <div className="local-ledger-list">
                  {localAccounts.items.map((account) => (
                    <div className="local-ledger-item" key={`${account.sub2api_base_url}-${account.sub2api_account_id}`}>
                      <div>
                        <strong>{account.email}</strong>
                        <span>{account.card_code}</span>
                      </div>
                      <div>
                        <span>{account.platform} · #{account.sub2api_account_id}</span>
                        <small>
                          {account.last_operation === 'recover_401' ? '401 找回' : '首次导入'} · {' '}
                          {new Date(account.updated_at).toLocaleString('zh-CN', { hour12: false })}
                        </small>
                      </div>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="local-ledger-empty">
                  {loadingLocalAccounts ? '正在读取本地数据库…' : '完成首次导入后，账号记录会显示在这里。'}
                </div>
              )}
            </section>

            {formError && <div className="form-error"><span>!</span>{formError}</div>}

            <button className="submit-button import-only" type="submit" disabled={running || !configLoaded || apiOnline === false}>
              {running ? <><span className="spinner" />任务执行中</> : <><span>开始兑换并导入</span><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h14M14 7l5 5-5 5" /></svg></>}
            </button>
          </form>

          <section className="panel progress-panel workspace-only">
            <div className="panel-heading compact">
              <div>
                <span className="panel-index">02</span>
                <div><h2>任务进度</h2><p>每个阶段都清晰可见</p></div>
              </div>
              {job && <span className={`job-state state-${job.status}`}>{job.status === 'succeeded' ? '已完成' : job.status === 'partial' ? '部分完成' : job.status === 'failed' ? '执行失败' : '运行中'}</span>}
            </div>

            {!job ? (
              <div className="empty-state">
                <div className="orbit-graphic" aria-hidden="true">
                  <div className="orbit orbit-a"><i /></div>
                  <div className="orbit orbit-b"><i /></div>
                  <div className="orbit-core"><svg viewBox="0 0 32 32"><path d="M8 10h11a5 5 0 010 10h-6M12 7l-4 3 4 3M20 19l4 3-4 3" /></svg></div>
                </div>
                <h3>{taskMode === 'recovery' ? '等待新的 401 找回任务' : '等待新的额度导入任务'}</h3>
                <p>
                  {taskMode === 'recovery'
                    ? '扫描 Sub2API 中的失效账号后，找回和更新进度会显示在这里。'
                    : '粘贴兑换码并开始任务后，兑换、下载和导入进度会显示在这里。'}
                </p>
                {taskMode === 'recovery' ? (
                  <div className="mini-flow"><span>扫描 401</span><i>→</i><span>找回凭据</span><i>→</i><span>原地更新</span></div>
                ) : (
                  <div className="mini-flow"><span>兑换码</span><i>→</i><span>额度文件</span><i>→</i><span>Sub2API</span></div>
                )}
              </div>
            ) : (
              <div className="job-content">
                <div className="progress-summary">
                  <div>
                    <span>{job.message}</span>
                    <strong>{job.progress}<small>%</small></strong>
                  </div>
                  <div className="progress-track"><i style={{ width: `${job.progress}%` }} /></div>
                </div>

                <div className="steps">
                  {stages.map((item, index) => {
                    const done = currentStage > index
                    const active = currentStage === index
                    const failed = job.status === 'failed' && active
                    return (
                      <div className={`step ${done ? 'done' : ''} ${active ? 'active' : ''} ${failed ? 'failed' : ''}`} key={item.key}>
                        <div className="step-marker">{done ? <svg viewBox="0 0 24 24"><path d="M5 12l4 4L19 7" /></svg> : index + 1}</div>
                        <div><strong>{item.label}</strong><span>{item.caption}</span></div>
                        {active && !failed && <span className="step-pulse" />}
                      </div>
                    )
                  })}
                </div>

                <div className="stats-grid">
                  {isRecoveryJob ? (
                    <>
                      <StatCard label="检测到 401" value={Number(jobScan?.detected_401 || 0)} />
                      <StatCard label="选择找回" value={Number(jobScan?.selected || 0)} accent="#70cfff" />
                      <StatCard label="成功更新" value={recoveredCount} accent="#5de3bb" />
                      <StatCard label="需检查" value={recoveryFailedCount} accent={recoveryFailedCount ? '#ffb86b' : undefined} />
                    </>
                  ) : (
                    <>
                      <StatCard label="检测账号" value={Number(health?.total || 0)} />
                      <StatCard label="下载账号" value={Number(download?.accounts || 0)} accent="#70cfff" />
                      <StatCard label="成功导入" value={importedCount} accent="#5de3bb" />
                      <StatCard label="需检查" value={failedCount} accent={failedCount ? '#ffb86b' : undefined} />
                    </>
                  )}
                </div>

                <div className="event-log">
                  <div className="event-title"><span>执行记录</span><small>{job.events.length} 条</small></div>
                  <div className="event-list">
                    {job.events.slice().reverse().map((event, index) => (
                      <div className={`event event-${event.level}`} key={`${event.time}-${index}`}>
                        <i />
                        <div><span>{event.message}</span><time>{new Date(event.time).toLocaleTimeString('zh-CN', { hour12: false })}</time></div>
                      </div>
                    ))}
                  </div>
                </div>

                {job.error && <div className="job-error"><strong>失败原因</strong><span>{job.error}</span></div>}
                {TERMINAL_STATUSES.has(job.status) && <button className="reset-button" type="button" onClick={resetTask}>创建下一个任务</button>}
              </div>
            )}
          </section>
        </div>
      </main>

      <footer>
        <span><i />Sub2API 配置以 0600 权限持久化；下载凭据不落盘</span>
        <span>FastAPI <b>×</b> React <b>×</b> Sub2API</span>
      </footer>
      </div>
    </div>
  )
}

export default App
