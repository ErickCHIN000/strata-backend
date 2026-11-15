import { ROUTES_AREA, SIDEBAR_NAV_AREA, STATUSBAR_AREAS, host, haptic, useQuery, queryClient, useValue } from '@hermes/plugin-sdk'
import { useEffect, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const title = 'Strata Backend'
const path = '/strata-backend'
const hour = 60 * 60 * 1000
const buttonClass = 'rounded-md border border-(--ui-stroke-secondary) px-3 py-1.5 text-sm hover:text-(--ui-accent) disabled:opacity-50'
const inputClass = 'w-full rounded-md border border-(--ui-stroke-secondary) bg-transparent px-2 py-1.5 text-sm'
const sectionClass = 'flex flex-col gap-3 border-t border-(--ui-stroke-secondary) pt-4'
const preferredRepo = 'DavidAU/Qwen3.8-Flash-Next-Abliterated-Uncensored-Turbo-Brilliance-50x-Reasoning-Instruct-GSQ-RCO-GGUF'

function catalogId(family, model) {
  return `catalog:${family}:${model}`
}

function downloadSize(bytes) {
  return bytes == null ? null : `${(bytes / 1e9).toFixed(1)} GB`
}

function DownloadModelButton({ selection, title, downloaded, pending, onDownload, label = 'Download' }) {
  return jsx('button', {
    type: 'button', className: buttonClass, disabled: pending || downloaded,
    'aria-label': downloaded ? `${title} downloaded` : `${label} ${title}`,
    onClick: () => onDownload(selection), children: downloaded ? 'Downloaded' : label,
  })
}

function ModelSuggestions({ catalog, downloads, busy, onDownload }) {
  const { recommendation, families } = catalog.data ?? {}
  if (catalog.isPending) return jsx('p', { children: 'Reading Strata’s model catalog…' })
  if (catalog.isError || catalog.data?.error) {
    return jsx('p', { role: 'alert', children: catalog.data?.error ?? 'Could not load model suggestions.' })
  }

  const context = recommendation?.context ? `${Math.round(recommendation.context / 1024)}K context` : null
  const download = recommendation?.download_gb == null ? null : `${recommendation.download_gb} GB download`
  return jsxs('section', {
    className: sectionClass,
    children: [
      jsx('h2', { className: 'font-medium', children: 'Suggested models' }),
      jsxs('div', {
        className: 'flex flex-col gap-1 rounded-md border border-(--ui-stroke-secondary) p-3',
        children: [
          recommendation?.title && jsx('strong', { children: recommendation.title }),
          (context || download) && jsx('p', { children: [context, download].filter(Boolean).join(' · ') }),
          jsx('p', { className: 'text-(--ui-text-secondary)', children: recommendation?.why }),
          ...(recommendation?.notes ?? []).map(note => jsx('p', { children: note }, note)),
          recommendation?.model && jsx('div', {
            children: jsx(DownloadModelButton, {
              selection: { source: 'catalog', family: recommendation.family, model: recommendation.model },
              title: recommendation.title, pending: busy, onDownload,
              downloaded: downloads.has(catalogId(recommendation.family, recommendation.model)),
            }),
          }),
        ],
      }),
      families?.length > 0 && jsxs('details', {
        children: [
          jsx('summary', { className: 'cursor-pointer', children: 'Browse Strata catalog' }),
          ...families.map(family => jsxs('div', {
            className: 'mt-3',
            children: [
              jsx('h3', { className: 'font-medium', children: family.title }),
              family.unavailable_reason && jsx('p', { children: family.unavailable_reason }),
              jsx('ul', {
                className: 'divide-y divide-(--ui-stroke-secondary)',
                children: family.sizes.map(size => jsxs('li', {
                  className: 'flex flex-wrap justify-between gap-x-4 py-1.5',
                  children: [
                    jsx('span', { children: `${size.model}${size.installed ? ' · Installed' : ''}${size.experimental ? ' · Experimental' : ''}` }),
                    jsx('span', {
                      className: 'text-(--ui-text-secondary)',
                      children: [
                        size.on_this_pc,
                        size.fit_gb == null ? null : `${size.fit_gb} GB memory`,
                        size.download_gb == null ? null : `${size.download_gb} GB download`,
                      ].filter(Boolean).join(' · '),
                    }),
                    jsx(DownloadModelButton, {
                      selection: { source: 'catalog', family: family.family, model: size.model },
                      title: `${family.title} ${size.model}`, pending: busy, onDownload,
                      downloaded: downloads.has(catalogId(family.family, size.model)),
                    }),
                  ],
                }, size.model)),
              }),
            ],
          }, family.family)),
        ],
      }),
    ],
  })
}

function HuggingFaceModels({ ctx, downloads, busy, onDownload }) {
  const [repo, setRepo] = useState('')
  const [filename, setFilename] = useState('')
  const [catalog, setCatalog] = useState(null)
  const [selected, setSelected] = useState('')
  const [reading, setReading] = useState(false)
  const [error, setError] = useState('')
  const lookupLock = useRef(false)
  const group = catalog?.groups?.find(row => row.id === selected)

  function clearLookup() {
    setCatalog(null)
    setSelected('')
    setError('')
  }

  async function findFiles(event) {
    event.preventDefault()
    if (!repo.trim() || lookupLock.current) return
    lookupLock.current = true
    setReading(true)
    clearLookup()
    try {
      const result = await ctx.rest('/runtime/hf-files', {
        method: 'POST', body: { repo: repo.trim(), filename: filename.trim() }, timeoutMs: 155_000,
      })
      if (result.error) throw new Error(result.error)
      setCatalog(result)
      setSelected(result.selected ?? (result.groups.length === 1 && !filename.trim() ? result.groups[0].id : ''))
      if (filename.trim() && !result.selected) setError('Choose the exact GGUF file or shard group from the results.')
    } catch (failure) {
      setError(failure?.message ?? 'Could not read the Hugging Face repository.')
    } finally {
      lookupLock.current = false
      setReading(false)
    }
  }

  const selection = group && { source: 'hf', repo: catalog.repo, revision: catalog.revision, group: group.id }
  const downloaded = selection && downloads.has(`hf:${selection.repo}@${selection.revision}:${selection.group}`)
  return jsxs('section', {
    className: sectionClass,
    children: [
      jsx('h2', { className: 'font-medium', children: 'Hugging Face models' }),
      jsxs('form', {
        className: 'flex flex-col gap-2', onSubmit: findFiles,
        children: [
          jsxs('label', {
            children: ['Repository', jsx('input', {
              className: inputClass, placeholder: 'owner/model-GGUF', value: repo, required: true, disabled: reading,
              onChange: event => { setRepo(event.target.value); clearLookup() },
            })],
          }),
          jsxs('label', {
            children: ['GGUF filename or quantization', jsx('input', {
              className: inputClass, placeholder: 'Qwen3.8-27B-UD-Q4_K_M.gguf', value: filename, disabled: reading,
              onChange: event => { setFilename(event.target.value); clearLookup() },
            })],
          }),
          jsxs('div', {
            className: 'flex flex-wrap gap-2',
            children: [
              jsx('button', {
                type: 'submit', className: buttonClass, disabled: reading || busy || !repo.trim(),
                children: reading ? 'Checking Hugging Face…' : 'Find GGUF files',
              }),
              jsx('button', {
                type: 'button', className: buttonClass, disabled: reading,
                onClick: () => { setRepo(preferredRepo); setFilename(''); clearLookup() },
                children: 'Use DavidAU Flash-Next repo',
              }),
            ],
          }),
        ],
      }),
      error && jsx('p', { role: 'alert', children: error }),
      catalog && jsx('p', {
        className: 'text-(--ui-text-secondary)',
        children: catalog.authenticated ? 'Existing Hugging Face login detected.' : catalog.existing_cli
          ? 'Hugging Face CLI detected. Using anonymous access.' : 'Using anonymous Hugging Face access.',
      }),
      catalog?.groups.length === 0 && jsx('p', { children: 'This repository has no complete GGUF files or shard sets.' }),
      catalog?.groups.length > 0 && jsxs('label', {
        children: ['Files to download', jsxs('select', {
          className: inputClass, value: selected, onChange: event => setSelected(event.target.value),
          children: [jsx('option', { value: '', children: 'Choose a file or shard group' }),
            ...catalog.groups.map(row => jsx('option', {
              value: row.id, children: [row.label, downloadSize(row.bytes), row.cached ? 'Cached' : null].filter(Boolean).join(' · '),
            }, row.id))],
        })],
      }),
      group && jsxs('div', {
        className: 'flex flex-wrap items-center gap-2',
        children: [
          jsx(DownloadModelButton, {
            selection, title: group.label, downloaded, pending: busy, onDownload,
            label: group.cached ? 'Use cached files' : 'Download',
          }),
          jsx('span', { children: `${group.files.length} ${group.files.length === 1 ? 'file' : 'files'}${group.bytes == null ? '' : ` · ${downloadSize(group.bytes)}`}` }),
          jsx('span', { className: 'text-(--ui-text-secondary)', children: 'Strata compatibility unverified' }),
        ],
      }),
    ],
  })
}

const controls = [
  { flag: '--max-context', label: 'Context tokens', min: 1, step: 1 },
  { flag: '--memory-limit-mib', label: 'Serving RAM budget (MiB)', min: 0, step: 1 },
  { flag: '--resident-budget-gib', label: 'Resident expert RAM (GiB)', min: 0.001, step: 'any' },
  { flag: '--vram-reserve-mib', label: 'VRAM to leave free (MiB)', min: 0, step: 1 },
]

const advancedFields = {
  engine_args: 'Engine arguments (JSON array)', launch_args: 'Launch arguments (JSON array)',
  setup_args: 'Setup arguments (JSON array)', server_args: 'Server arguments (JSON array)',
  env: 'Environment (JSON object)', config: 'Strata run config overrides (JSON object)',
}

function profileDraft(profile = {}) {
  return {
    id: profile.id, name: profile.name ?? '', model_id: profile.model_id ?? '', controls: profile.controls ?? {},
    ...Object.fromEntries(Object.keys(advancedFields).map(name => [
      name, JSON.stringify(profile[name] ?? (name.endsWith('_args') ? [] : {}), null, 2),
    ])),
  }
}

function OptionReference({ ctx, queryScope, revision, enabled }) {
  const [opened, setOpened] = useState(false)
  const [section, setSection] = useState('Engine')
  const [filter, setFilter] = useState('')
  const reference = useQuery({
    queryKey: [...queryScope, 'strata-model-options', revision],
    queryFn: () => ctx.rest('/runtime/model-options', { timeoutMs: 55_000 }),
    enabled: enabled && opened, staleTime: hour, retry: false,
  })
  const sections = reference.data?.sections ?? {}
  const selection = sections[section] ? section : Object.keys(sections)[0]
  const doc = sections[selection] ?? ''
  const visible = filter.trim() ? doc.split('\n').filter(line => line.toLowerCase().includes(filter.toLowerCase().trim())).join('\n') : doc
  return jsxs('details', {
    onToggle: event => setOpened(event.currentTarget.open),
    children: [
      jsx('summary', { className: 'cursor-pointer', children: 'Strata option reference' }),
      reference.isFetching && jsx('p', { children: 'Reading options from this build…' }),
      (reference.isError || reference.data?.error) && jsx('p', { role: 'alert', children: reference.data?.error ?? 'Could not read the Strata option reference.' }),
      selection && jsxs('div', {
        className: 'mt-2 flex flex-col gap-2',
        children: [
          jsxs('select', {
            'aria-label': 'Option category', className: inputClass, value: selection, onChange: event => setSection(event.target.value),
            children: Object.keys(sections).map(name => jsx('option', { value: name, children: name }, name)),
          }),
          jsx('input', {
            'aria-label': 'Filter options', className: inputClass, value: filter, placeholder: 'Filter options',
            onChange: event => setFilter(event.target.value),
          }),
          jsx('pre', { className: 'max-h-64 overflow-auto whitespace-pre-wrap text-xs', children: visible || 'No matching options.' }),
        ],
      }),
    ],
  })
}

function ModelProfiles({ ctx, queryScope, library, catalog, revision, onSave }) {
  const [draft, setDraft] = useState(() => profileDraft())
  const [saving, setSaving] = useState(false)
  const [feedback, setFeedback] = useState(null)
  const profiles = library?.profiles ?? []
  const entries = new Map((library?.models ?? []).map(model => [model.id, model.title]))
  for (const family of catalog?.families ?? []) {
    for (const size of family.sizes) entries.set(catalogId(family.family, size.model), `${family.title} ${size.model}`)
  }
  if (draft.model_id && !entries.has(draft.model_id)) entries.set(draft.model_id, 'Previously selected model')

  function edit(name, value) {
    setDraft(previous => ({ ...previous, [name]: value }))
    setFeedback(null)
  }

  async function save(event) {
    event.preventDefault()
    if (saving) return
    setSaving(true)
    setFeedback(null)
    try {
      const advanced = Object.fromEntries(Object.keys(advancedFields).map(name => [name, JSON.parse(draft[name])]))
      const result = await ctx.rest('/runtime/save-profile', {
        method: 'POST', body: { ...draft, model_id: draft.model_id || null, ...advanced },
      })
      if (!result.ok) throw new Error(result.error || 'Could not save the profile.')
      setDraft(profileDraft(result.profile))
      setFeedback({ message: 'Profile saved.' })
      await onSave()
    } catch (failure) {
      setFeedback({ error: true, message: failure instanceof SyntaxError ? 'Fix the JSON in the advanced settings.' : failure?.message })
    } finally {
      setSaving(false)
    }
  }

  return jsxs('section', {
    className: sectionClass,
    children: [
      jsx('h2', { className: 'font-medium', children: 'Model profiles' }),
      jsxs('form', {
        className: 'flex flex-col gap-3', onSubmit: save,
        children: [
          jsxs('label', {
            children: ['Saved profiles', jsxs('select', {
              className: inputClass, value: draft.id ?? '', disabled: saving,
              onChange: event => { setDraft(profileDraft(profiles.find(profile => profile.id === event.target.value))); setFeedback(null) },
              children: [jsx('option', { value: '', children: 'New profile' }),
                ...profiles.map(profile => jsx('option', { value: profile.id, children: profile.name }, profile.id))],
            })],
          }),
          jsxs('label', {
            children: ['Name', jsx('input', {
              className: inputClass, value: draft.name, required: true, maxLength: 120, disabled: saving,
              onChange: event => edit('name', event.target.value),
            })],
          }),
          jsxs('label', {
            children: ['Model', jsxs('select', {
              className: inputClass, value: draft.model_id, disabled: saving, onChange: event => edit('model_id', event.target.value),
              children: [jsx('option', { value: '', children: 'Any model' }),
                ...Array.from(entries, ([id, title]) => jsx('option', { value: id, children: title }, id))],
            })],
          }),
          jsx('div', {
            className: 'grid gap-2 sm:grid-cols-2',
            children: controls.map(control => jsxs('label', {
              children: [control.label, jsx('input', {
                className: inputClass, type: 'number', min: control.min, step: control.step, placeholder: 'Strata default',
                value: draft.controls[control.flag] ?? '', disabled: saving,
                onChange: event => edit('controls', { ...draft.controls, [control.flag]: event.target.value }),
              })],
            }, control.flag)),
          }),
          jsxs('details', {
            children: [
              jsx('summary', { className: 'cursor-pointer', children: 'Advanced profile settings' }),
              jsx('p', { className: 'my-2 text-(--ui-text-secondary)', children: 'Engine arguments are appended after the resource controls. Each argument has its own array entry.' }),
              ...Object.entries(advancedFields).map(([name, title]) => jsxs('label', {
                className: 'mb-2 block',
                children: [title, jsx('textarea', {
                  className: `${inputClass} font-mono`, rows: 3, value: draft[name], disabled: saving,
                  onChange: event => edit(name, event.target.value),
                })],
              }, name)),
            ],
          }),
          jsx(OptionReference, { ctx, queryScope, revision, enabled: Boolean(revision) }),
          jsx('div', { children: jsx('button', { type: 'submit', className: buttonClass, disabled: saving, children: saving ? 'Saving…' : 'Save profile' }) }),
          feedback && jsx('p', { role: feedback.error ? 'alert' : 'status', children: feedback.message }),
        ],
      }),
    ],
  })
}

function StrataPage({ ctx }) {
  const connection = useValue(host.state.connectionId)
  const profile = useValue(host.state.profile)
  const connected = useValue(host.state.gateway) === 'open'
  const queryScope = [ctx.source, connection, profile]
  const statusKey = [ctx.source, 'strata-runtime-status', connection, profile]
  const status = useQuery({
    queryKey: statusKey,
    queryFn: () => ctx.rest('/runtime/status'),
    enabled: connected,
    refetchInterval: query => query.state.data?.operation ? 2000 : false,
    retry: false,
  })
  const data = status.data
  const latest = useQuery({
    queryKey: [ctx.source, 'strata-latest', connection, profile, data?.revision],
    queryFn: () => ctx.rest('/runtime/latest'),
    enabled: connected && Boolean(data) && !data.operation,
    staleTime: hour,
    refetchInterval: hour,
    retry: false,
  })
  const models = useQuery({
    queryKey: [ctx.source, 'strata-models', connection, profile, data?.revision],
    queryFn: () => ctx.rest('/runtime/models', { timeoutMs: 50_000 }),
    enabled: connected && Boolean(data?.ready) && !data?.operation,
    staleTime: hour,
    retry: false,
  })
  const libraryKey = [...queryScope, 'strata-library']
  const library = useQuery({
    queryKey: libraryKey,
    queryFn: () => ctx.rest('/runtime/library'),
    enabled: connected && Boolean(data), retry: false,
  })
  const lastOperation = useRef(null)
  useEffect(() => {
    if (lastOperation.current && !data?.operation && connected) {
      void queryClient.invalidateQueries({ queryKey: [ctx.source, connection, profile, 'strata-library'], exact: true })
    }
    lastOperation.current = data?.operation
  }, [data?.operation, connected, ctx.source, connection, profile])
  const requestLock = useRef(false)
  const [requesting, setRequesting] = useState(false)
  const [confirmWipe, setConfirmWipe] = useState(false)
  const [restarting, setRestarting] = useState(false)
  const pending = requesting || Boolean(data?.operation)
  const needsRestart = status.isError && /\b404\b/.test(String(status.error))

  async function restartGateway() {
    setRestarting(true)
    try {
      if (await host.restartGateway()) await status.refetch()
    } catch (error) {
      host.notifyError(error, 'Could not restart the Hermes gateway')
    } finally {
      setRestarting(false)
    }
  }

  async function run(operation, body) {
    if (requestLock.current || data?.operation) return
    requestLock.current = true
    setRequesting(true)
    setConfirmWipe(false)
    try {
      haptic('tap')
      const result = await ctx.rest(`/runtime/${operation}`, body ? { method: 'POST', body } : { method: 'POST' })
      if (!result.ok) throw new Error(result.error || 'The operation failed.')
      queryClient.setQueryData(statusKey, previous => ({ ...previous, operation }))
      await queryClient.invalidateQueries({ queryKey: statusKey, exact: true })
      await queryClient.invalidateQueries({ queryKey: libraryKey, exact: true })
    } catch (error) {
      host.notifyError(error, 'Could not start Strata operation')
    } finally {
      requestLock.current = false
      setRequesting(false)
    }
  }

  const operationLabels = {
    setup: 'Downloading and setting up Strata…',
    update: 'Updating and building Strata…',
    wipe: 'Removing managed Strata files…',
    download: 'Downloading model files…',
  }
  const busyLabel = operationLabels[data?.operation]
  const updateAvailable = Boolean(latest.data?.update_available)
  const primaryAction = updateAvailable ? 'update' : 'setup'
  const primaryLabel = updateAvailable ? 'Update Strata' : data?.revision ? 'Set up Strata' : 'Download and set up'
  const stateLabel = data?.ready ? 'Engine ready' : data?.revision ? 'Setup incomplete' : 'Not downloaded'
  const latestRevision = latest.data?.latest
  const modelDownloads = library.data?.models ?? []
  const downloads = new Set(modelDownloads.map(model => model.id))
  const scopeKey = JSON.stringify([connection, profile])
  const modelButtons = { downloads, busy: pending || !connected, onDownload: body => void run('download', body) }

  return jsxs('section', {
    className: 'mx-auto flex w-full max-w-3xl flex-col gap-4 p-6 text-sm',
    children: [
      jsx('h1', { className: 'text-lg font-medium', children: title }),
      jsx('p', {
        className: 'text-(--ui-text-secondary)',
        'aria-live': 'polite',
        children: busyLabel ?? (connected ? data ? stateLabel : status.isError ? 'Backend unavailable' : 'Checking Strata…' : 'Waiting for the Hermes backend…'),
      }),
      data && !status.isError && jsxs('dl', {
        className: 'grid grid-cols-2 gap-2',
        children: [
          jsx('dt', { children: 'Installed commit' }),
          jsx('dd', { children: data.revision?.slice(0, 7) ?? '—' }),
          jsx('dt', { children: 'GitHub main' }),
          jsx('dd', { children: latestRevision?.slice(0, 7) ?? (latest.isFetching ? 'Checking…' : 'Unavailable') }),
        ],
      }),
      data && !data.ready && !pending && jsx('p', {
        className: 'text-(--ui-text-secondary)',
        children: 'Setup builds the Strata engine and installs its Python dependencies in a private virtual environment. Model selection is separate.',
      }),
      jsxs('div', {
        className: 'flex flex-wrap items-center gap-2',
        children: [
          (!data?.ready || updateAvailable) && jsx('button', {
            type: 'button', className: `${buttonClass} text-(--ui-accent)`,
            disabled: !connected || !data || pending || confirmWipe || status.isError,
            onClick: () => void run(primaryAction), children: primaryLabel,
          }),
          jsx('button', {
            type: 'button', className: buttonClass, disabled: !connected || !data || pending || latest.isFetching,
            onClick: () => void latest.refetch(), children: 'Check for updates',
          }),
          data?.has_data && !confirmWipe && jsx('button', {
            type: 'button', className: buttonClass, disabled: !connected || pending,
            onClick: () => setConfirmWipe(true), children: 'Fresh wipe',
          }),
        ],
      }),
      confirmWipe && jsxs('div', {
        className: 'flex flex-col items-start gap-2 rounded-md border border-(--ui-stroke-secondary) p-3',
        children: [
          jsx('p', { children: 'Delete the Strata checkout, venv, downloaded models, and setup files managed by this plugin?' }),
          jsxs('div', { className: 'flex gap-2', children: [
            jsx('button', {
              type: 'button', className: buttonClass, disabled: !connected || pending,
              onClick: () => void run('wipe'), children: 'Delete managed files',
            }),
            jsx('button', { type: 'button', className: buttonClass, onClick: () => setConfirmWipe(false), children: 'Cancel' }),
          ] }),
        ],
      }),
      (data?.error || status.isError) && jsxs('div', {
        className: 'flex flex-col items-start gap-2',
        children: [
          jsx('p', {
            role: 'alert',
            children: data?.error ?? (needsRestart ? 'Restart the gateway once to activate the new Strata backend.' : 'The Strata backend is unavailable.'),
          }),
          status.isError && jsx('button', {
            type: 'button', className: buttonClass, disabled: !connected || restarting || status.isFetching,
            onClick: needsRestart ? () => void restartGateway() : () => void status.refetch(),
            children: restarting ? 'Restarting gateway…' : needsRestart ? 'Restart gateway' : 'Retry',
          }),
        ],
      }),
      data?.log && jsxs('details', {
        children: [
          jsx('summary', { className: 'cursor-pointer', children: 'Activity log' }),
          jsx('pre', { className: 'mt-2 max-h-64 overflow-auto whitespace-pre-wrap text-xs', children: data.log }),
        ],
      }),
      data?.ready && jsx(ModelSuggestions, { catalog: models, ...modelButtons }),
      data?.ready && jsx(HuggingFaceModels, { ctx, ...modelButtons }, scopeKey),
      modelDownloads.length > 0 && jsxs('section', {
        className: sectionClass,
        children: [
          jsx('h2', { className: 'font-medium', children: 'Downloaded models' }),
          jsx('ul', {
            className: 'divide-y divide-(--ui-stroke-secondary)',
            children: modelDownloads.map(model => jsxs('li', {
              className: 'break-words py-2',
              children: [jsx('div', { children: model.title }), jsx('div', {
                className: 'text-(--ui-text-secondary)',
                children: [model.repo, downloadSize(model.bytes), model.source === 'hf' ? 'Strata compatibility unverified' : null].filter(Boolean).join(' · '),
              })],
            }, model.id)),
          }),
        ],
      }),
      (library.isError || library.data?.error) && jsx('p', { role: 'alert', children: library.data?.error ?? 'Could not load model downloads and profiles.' }),
      library.data?.profiles && jsx(ModelProfiles, {
        ctx, queryScope, library: library.data, catalog: models.data, revision: data?.ready ? data.revision : null,
        onSave: () => queryClient.invalidateQueries({ queryKey: libraryKey, exact: true }),
      }, scopeKey),
    ],
  })
}

function VersionPill({ ctx }) {
  const connection = useValue(host.state.connectionId)
  const profile = useValue(host.state.profile)
  const gateway = useValue(host.state.gateway)
  const queryKey = [ctx.source, 'version-status', connection, profile]
  const { data, isError } = useQuery({
    queryKey,
    queryFn: () => ctx.rest('/version-status'),
    enabled: gateway === 'open',
    staleTime: hour,
    refetchInterval: hour,
    retry: false,
  })
  const updateLock = useRef(false)
  const [isUpdating, setIsUpdating] = useState(false)

  async function applyUpdate() {
    if (updateLock.current) return

    updateLock.current = true
    setIsUpdating(true)
    try {
      haptic('tap')
      host.notify({ kind: 'info', message: 'Updating Strata Backend…' })
      const result = await ctx.rest('/apply-update', { method: 'POST', timeoutMs: 180_000 })
      if (!result.ok) throw new Error(result.error || 'The update failed.')

      queryClient.setQueryData(queryKey, { current: result.version, update_available: false })
      await queryClient.invalidateQueries({ queryKey, exact: true })
    } catch (error) {
      host.notifyError(error, 'Could not update Strata Backend')
    } finally {
      updateLock.current = false
      setIsUpdating(false)
    }
  }

  const label = data?.current ? `Strata ${data.current}` : title
  const baseClass = 'inline-flex items-center gap-1.5 whitespace-nowrap rounded-full px-1.5 text-[0.6875rem] text-(--ui-text-tertiary)'
  if (!data?.update_available && !isUpdating) {
    return jsx('span', {
      className: baseClass,
      title: data?.error ?? (isError ? 'Strata Backend version check unavailable' : title),
      children: label,
    })
  }

  return jsxs('button', {
    type: 'button',
    className: `${baseClass} hover:text-(--ui-text-secondary) disabled:cursor-wait`,
    disabled: isUpdating,
    'aria-label': isUpdating ? 'Updating Strata Backend' : `Update Strata Backend to ${data.latest}`,
    title: isUpdating ? 'Updating Strata Backend…' : `Update Strata Backend to ${data.latest}`,
    onClick: applyUpdate,
    children: [
      label,
      jsx('span', {
        className: 'rounded-full border border-current px-1 text-(--ui-accent)',
        children: isUpdating ? 'Updating…' : `↑ ${data.latest}`,
      }),
    ],
  })
}

export default {
  id: 'strata-backend',
  name: title,
  register(ctx) {
    ctx.register({
      id: 'page',
      area: ROUTES_AREA,
      title,
      data: { path },
      render: () => jsx(StrataPage, { ctx }),
    })
    ctx.register({
      id: 'nav',
      area: SIDEBAR_NAV_AREA,
      data: { path, label: title, codicon: 'server' },
    })
    ctx.register({
      id: 'version',
      area: STATUSBAR_AREAS.right,
      order: 130,
      render: () => jsx(VersionPill, { ctx }),
    })
  },
}
