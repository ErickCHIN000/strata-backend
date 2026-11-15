import {
  ROUTES_AREA, SIDEBAR_NAV_AREA, STATUSBAR_AREAS, host, haptic, useQuery, queryClient, useValue,
  ConfirmDialog, Select, SelectTrigger, SelectValue, SelectContent, SelectItem,
} from '@hermes/plugin-sdk'
import { useEffect, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const title = 'Strata Backend'
const path = '/strata-backend'
const hour = 60 * 60 * 1000
const pluginUpdateInterval = 2 * 60 * 1000
const buttonClass = 'rounded-md border border-(--ui-stroke-secondary) px-3 py-1.5 text-sm hover:text-(--ui-accent) disabled:opacity-50'
const inputClass = 'w-full rounded-md border border-(--ui-stroke-secondary) bg-transparent px-2 py-1.5 text-sm'
const sectionClass = 'flex flex-col gap-3 border-t border-(--ui-stroke-secondary) pt-4'
const preferredRepo = 'SC117/Qwen3.8-Flash-Next-GSQ-RCO-abliterated-GGUF'
const emptySelection = ':empty:'

function ThemedSelect({ label, value, options, onChange, disabled, placeholder }) {
  const selected = value === '' && options.some(([id]) => id === '') ? emptySelection : value ?? ''
  return jsxs('div', {
    children: [
      jsx('div', { children: label }),
      jsxs(Select, {
        value: selected, disabled, onValueChange: id => onChange(id === emptySelection ? '' : id),
        children: [
          jsx(SelectTrigger, {
            'aria-label': label, className: 'w-full min-w-0',
            children: jsx(SelectValue, { className: 'truncate', placeholder }),
          }),
          jsx(SelectContent, {
            className: 'max-w-[min(85vw,48rem)]',
            children: options.map(([id, title]) => jsx(SelectItem, {
              value: id || emptySelection, className: 'whitespace-normal break-words', children: title,
            }, id || emptySelection)),
          }),
        ],
      }),
    ],
  })
}

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
                onClick: () => { setRepo(preferredRepo); setFilename('IQ3_S'); clearLookup() },
                children: 'Use SC117 Flash-Next IQ3_S',
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
      catalog?.groups.length > 0 && jsx(ThemedSelect, {
        label: 'Files to download', value: selected, onChange: setSelected, placeholder: 'Choose a file or shard group',
        options: catalog.groups.map(row => [row.id, [row.label, downloadSize(row.bytes), row.cached ? 'Cached' : null].filter(Boolean).join(' · ')]),
      }),
      group && jsxs('div', {
        className: 'flex flex-wrap items-center gap-2',
        children: [
          jsx(DownloadModelButton, {
            selection, title: group.label, downloaded, pending: busy, onDownload,
            label: group.cached ? 'Use cached files' : 'Download',
          }),
          jsx('span', { children: `${group.files.length} ${group.files.length === 1 ? 'file' : 'files'}${group.bytes == null ? '' : ` · ${downloadSize(group.bytes)}`}` }),
          jsx('span', { className: 'text-(--ui-text-secondary)', children: 'Raw GGUF · Strata compatibility unverified' }),
        ],
      }),
    ],
  })
}

const controls = [
  { flag: '--max-context', label: 'Context tokens', min: 1, step: 1 },
  { flag: '--memory-limit-mib', label: 'Serving RAM budget (MiB)', min: 0, step: 1 },
  { flag: '--resident-budget-gib', label: 'Resident expert RAM (GiB)', min: 0.001, step: 'any' },
  { flag: '--vram-reserve-mib', label: 'VRAM reserve target (MiB)', min: 0, step: 1 },
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
          jsx(ThemedSelect, {
            label: 'Option category', value: selection, onChange: setSection,
            options: Object.keys(sections).map(name => [name, name]),
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

function LibraryDeleteDialog({ ctx, target, onClose, onDeleted }) {
  return jsx(ConfirmDialog, {
    open: true,
    destructive: true,
    title: target.kind === 'model' ? 'Delete downloaded model?' : 'Delete saved profile?',
    description: target.kind === 'model'
      ? `Are you sure you want to delete the downloaded files for “${target.name}”? Its saved profiles will remain.`
      : `Are you sure you want to delete “${target.name}”? Its downloaded model will remain.`,
    confirmLabel: 'Delete',
    busyLabel: 'Deleting…',
    doneLabel: 'Deleted',
    onClose,
    onConfirm: async () => {
      const result = await ctx.rest(`/runtime/delete-${target.kind}`, {
        method: 'POST', body: { id: target.id }, timeoutMs: 60_000,
      })
      if (!result.ok) throw new Error(result.error || 'Could not delete the selected item.')
      if (result.warning) host.notify({ kind: 'info', message: result.warning })
      await onDeleted()
    },
  })
}

function ModelProfiles({ ctx, queryScope, library, revision, busy, onSave }) {
  const [draft, setDraft] = useState(() => profileDraft())
  const [saving, setSaving] = useState(false)
  const [feedback, setFeedback] = useState(null)
  const [deleteProfile, setDeleteProfile] = useState(null)
  const profiles = library?.profiles ?? []
  const savedProfile = profiles.find(profile => profile.id === draft.id)
  const entries = new Map((library?.models ?? []).map(model => [model.id, model.title]))
  const missingModel = draft.model_id && !entries.has(draft.model_id)

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
      setFeedback({ message: result.warning ?? 'Profile saved.' })
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
          jsx(ThemedSelect, {
            label: 'Saved profiles', value: draft.id ?? '', disabled: saving,
            onChange: id => { setDraft(profileDraft(profiles.find(profile => profile.id === id))); setFeedback(null) },
            options: [['', 'New profile'], ...profiles.map(profile => [profile.id, profile.name])],
          }),
          jsxs('label', {
            children: ['Name', jsx('input', {
              className: inputClass, value: draft.name, required: true, maxLength: 120, disabled: saving,
              onChange: event => edit('name', event.target.value),
            })],
          }),
          jsx(ThemedSelect, {
            label: 'Model', value: missingModel ? null : draft.model_id, disabled: saving,
            placeholder: 'Saved model not downloaded', onChange: id => edit('model_id', id),
            options: [['', 'Any model'], ...entries],
          }),
          missingModel && jsx('p', { className: 'text-(--ui-text-secondary)', children: 'This profile keeps its saved model until you choose another option.' }),
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
          jsxs('div', { className: 'flex flex-wrap gap-2', children: [
            jsx('button', { type: 'submit', className: buttonClass, disabled: saving, children: saving ? 'Saving…' : 'Save profile' }),
            savedProfile && jsx('button', {
              type: 'button', className: buttonClass, disabled: saving || busy,
              onClick: () => setDeleteProfile({ kind: 'profile', id: savedProfile.id, name: savedProfile.name }),
              children: 'Delete profile',
            }),
          ] }),
          feedback && jsx('p', { role: feedback.error ? 'alert' : 'status', children: feedback.message }),
        ],
      }),
      deleteProfile && jsx(LibraryDeleteDialog, {
        ctx, target: deleteProfile, onClose: () => setDeleteProfile(null),
        onDeleted: async () => {
          setDraft(profileDraft())
          setFeedback(null)
          await onSave()
        },
      }),
    ],
  })
}

function DownloadProgress({ transfer, active, busy, pausing, onPause, onResume }) {
  if (!transfer) {
    return active && jsx('p', { children: 'This download started with an older plugin version. Progress and pause are available on new transfers.' })
  }
  const percent = transfer.total_bytes ? Math.min(100, Math.round((transfer.bytes ?? 0) / transfer.total_bytes * 100)) : null
  const phaseLabels = {
    preparing: 'Preparing download', downloading: 'Downloading', verifying: 'Verifying file',
    paused: 'Paused', interrupted: 'Interrupted', failed: 'Download failed', complete: 'Download complete',
  }
  const resume = !active && ['paused', 'failed', 'interrupted'].includes(transfer.phase)
  const phase = transfer.pause_requested || pausing ? 'Pausing…' : phaseLabels[transfer.phase]
  const amount = transfer.bytes == null ? null : `${downloadSize(transfer.bytes)}${transfer.total_bytes ? ` / ${downloadSize(transfer.total_bytes)}` : ''}`
  return jsxs('section', {
    className: 'flex flex-col gap-2 rounded-md border border-(--ui-stroke-secondary) p-3',
    children: [
      jsx('h2', { className: 'font-medium', children: transfer.title ?? 'Model download' }),
      jsx('p', { children: [phase, amount, percent == null ? null : `${percent}%`].filter(Boolean).join(' · ') }),
      percent != null && jsx('div', {
        role: 'progressbar', 'aria-label': 'Model download progress', 'aria-valuemin': 0, 'aria-valuemax': 100, 'aria-valuenow': percent,
        className: 'h-2 overflow-hidden rounded-full bg-(--ui-stroke-secondary)',
        children: jsx('div', { className: 'h-full bg-(--ui-accent)', style: { width: `${percent}%` } }),
      }),
      active && transfer.file && jsx('p', {
        className: 'break-words text-(--ui-text-secondary)',
        children: `File ${Math.min(transfer.completed_files + 1, transfer.files)} of ${transfer.files} · ${transfer.file}`,
      }),
      active && transfer.phase !== 'complete' && jsx('div', {
        children: jsx('button', {
          type: 'button', className: buttonClass, onClick: onPause,
          disabled: pausing || transfer.pause_requested || transfer.phase === 'paused', children: 'Pause download',
        }),
      }),
      resume && jsx('div', {
        children: jsx('button', { type: 'button', className: buttonClass, disabled: busy, onClick: onResume, children: 'Resume download' }),
      }),
    ],
  })
}

function startupLabel(row) {
  if (row?.stage === 'downloading-mtp') return ['Downloading MTP', row.detail].filter(Boolean).join(' · ')
  if (row?.stage === 'packing-mtp') return 'Packing MTP'
  if (row?.stage === 'packing-model') return 'Packing model'
  if (row?.stage === 'loading-model' || row?.state === 'starting') return 'Loading model'
  if (row?.state === 'preparing') return 'Preparing model files'
  return row?.state
}

function ServingStats({ stats }) {
  const rate = value => Number.isFinite(value) ? `${value.toLocaleString()} tok/s` : '—'
  const memory = (used, total) => Number.isFinite(used) && Number.isFinite(total)
    ? `${(used / 2 ** 30).toFixed(1)} / ${(total / 2 ** 30).toFixed(1)} GiB` : '—'
  const live = Number.isFinite(stats.generation_tps) || Number.isFinite(stats.prompt_tps)
  return jsxs('div', {
    className: 'mt-2 grid gap-2',
    children: [
      jsx('dl', {
        className: 'grid grid-cols-2 gap-x-4 gap-y-1',
        children: [
          ['System RAM', memory(stats.ram_used, stats.ram_total)],
          ['GPU VRAM', memory(stats.vram_used, stats.vram_total)],
          Number.isFinite(stats.expert_cache_mib) && ['GPU expert cache', `${(stats.expert_cache_mib / 1024).toFixed(1)} GiB`],
          live && ['Live generation', rate(stats.generation_tps)],
          live && ['Live prompt processing', rate(stats.prompt_tps)],
        ].filter(Boolean).map(([label, value]) => jsxs('div', {
          children: [
            jsx('dt', { className: 'text-(--ui-text-secondary)', children: label }),
            jsx('dd', { className: 'tabular-nums', children: value }),
          ],
        }, label)),
      }),
      stats.recent.length > 0 && jsxs('table', {
        className: 'w-full text-left text-xs tabular-nums',
        children: [
          jsx('caption', { className: 'text-left text-(--ui-text-secondary)', children: 'Recent Strata requests, newest first' }),
          jsx('thead', { children: jsxs('tr', { children: [
            jsx('th', { scope: 'col', className: 'pr-3 font-medium', children: 'Generation' }),
            jsx('th', { scope: 'col', className: 'font-medium', children: 'Prompt processing' }),
          ] }) }),
          jsx('tbody', { children: stats.recent.map((sample, index) => jsxs('tr', { children: [
            jsx('td', { className: 'pr-3', children: Number.isFinite(sample.generated)
              ? `${rate(sample.generation_tps)} · ${sample.generated.toLocaleString()} tokens` : rate(sample.generation_tps) }),
            jsx('td', { children: Number.isFinite(sample.prompt_read) && Number.isFinite(sample.prompt_total)
              ? `${rate(sample.prompt_tps)} · ${sample.prompt_read.toLocaleString()} of ${sample.prompt_total.toLocaleString()} read` : rate(sample.prompt_tps) }),
          ] }, index)) }),
        ],
      }),
    ],
  })
}

function ServingControls({ ctx, serving }) {
  const [requesting, setRequesting] = useState(false)
  const [confirmStop, setConfirmStop] = useState(null)
  const state = serving.data
  const rows = (state?.instances ?? []).filter(row => !row.provider)

  async function request(operation, body) {
    if (requesting) return
    setRequesting(true)
    setConfirmStop(null)
    try {
      const result = await ctx.rest(`/runtime/${operation}`, { method: 'POST', body, timeoutMs: 18_000 })
      if (!result.ok) throw new Error(result.error || 'The operation failed.')
      await serving.refetch()
    } catch (error) {
      host.notifyError(error, 'Could not manage the Strata provider')
    } finally {
      setRequesting(false)
    }
  }

  return jsxs('section', {
    className: sectionClass,
    children: [
      jsx('h2', { className: 'font-medium', children: 'Hermes provider' }),
      jsx('p', { className: 'text-(--ui-text-secondary)', children: state?.provider_ready
        ? 'Select a saved Strata profile in the Hermes model picker to load it. Selecting another provider unloads Strata.'
        : state?.models?.length ? 'Registering the Strata provider with Hermes…' : 'Save a profile linked to a downloaded model to make it available in the picker.' }),
      jsx('h3', { className: 'font-medium', children: 'Strata processes' }),
      !rows.length && jsx('p', { className: 'text-(--ui-text-secondary)', children: 'No Strata processes detected.' }),
      rows.length > 0 && jsx('ul', {
        className: 'divide-y divide-(--ui-stroke-secondary)',
        children: rows.map(row => jsxs('li', {
          className: 'flex flex-wrap items-center justify-between gap-2 py-2',
          children: [
            jsxs('div', { className: 'min-w-0 break-words', children: [
              jsx('div', { children: `${row.name} · ${startupLabel(row)}` }),
              jsx('div', { className: 'text-(--ui-text-secondary)', children: [row.managed ? 'Managed' : 'External', row.pid && `PID ${row.pid}`, row.path].filter(Boolean).join(' · ') }),
              row.stats && jsx(ServingStats, { stats: row.stats }),
              row.log && jsxs('details', { children: [jsx('summary', { children: 'Server log' }),
                jsx('pre', { className: 'max-h-48 overflow-auto whitespace-pre-wrap text-xs', children: row.log })] }),
            ] }),
            row.state !== 'exited' && jsx('button', {
              type: 'button', className: buttonClass, disabled: requesting,
              onClick: () => row.managed ? void request('stop-instance', row) : setConfirmStop(row), children: 'Stop process',
            }),
          ],
        }, `${row.pid}:${row.started}`)),
      }),
      confirmStop && jsxs('div', {
        className: 'flex flex-wrap items-center gap-2',
        children: [jsx('span', { children: `Stop external Strata process ${confirmStop.pid}?` }),
          jsx('button', { type: 'button', className: buttonClass, onClick: () => void request('stop-instance', confirmStop), children: 'Confirm stop' }),
          jsx('button', { type: 'button', className: buttonClass, onClick: () => setConfirmStop(null), children: 'Cancel' })],
      }),
      (serving.isError || state?.error) && jsx('p', { role: 'alert', children: state?.error ?? 'Could not read Strata process status.' }),
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
  const serving = useQuery({
    queryKey: [...queryScope, 'strata-serving'],
    queryFn: () => ctx.rest('/runtime/serving'),
    enabled: connected && Boolean(data),
    refetchInterval: 3000, retry: false,
  })
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
  const [deleteModel, setDeleteModel] = useState(null)
  const [restarting, setRestarting] = useState(false)
  const [pausing, setPausing] = useState(false)
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
      queryClient.setQueryData(statusKey, previous => ({ ...previous, operation: result.operation ?? operation }))
      await queryClient.invalidateQueries({ queryKey: statusKey, exact: true })
      await queryClient.invalidateQueries({ queryKey: libraryKey, exact: true })
    } catch (error) {
      host.notifyError(error, 'Could not start Strata operation')
    } finally {
      requestLock.current = false
      setRequesting(false)
    }
  }

  async function pauseDownload() {
    if (pausing) return
    setPausing(true)
    try {
      const result = await ctx.rest('/runtime/pause-download', { method: 'POST' })
      if (!result.ok) throw new Error(result.error || 'Could not pause the download.')
      await status.refetch()
    } catch (error) {
      host.notifyError(error, 'Could not pause the model download')
    } finally {
      setPausing(false)
    }
  }

  const operationLabels = {
    setup: 'Downloading and setting up Strata…',
    update: 'Updating and building Strata…',
    wipe: 'Removing managed Strata files…',
    download: 'Downloading model files…',
    serve: startupLabel(serving.data?.instances?.find(row => !row.provider && row.state === 'preparing'))
      ?? 'Preparing model files',
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
  const modelsFolder = library.data?.models_folder

  async function openModelsFolder() {
    if (!modelsFolder) return
    try {
      const routes = await host.profileRoutes()
      if (connection !== host.state.connectionId.get() || profile !== host.state.profile.get()) return
      const route = routes.find(candidate => candidate.connectionId === (connection ?? 'local') && candidate.profile === profile)
      if (route?.mode !== 'local') throw new Error('Opening the models folder requires a local Hermes backend.')
      const result = await window.hermesDesktop?.openDir?.(modelsFolder)
      if (!result?.ok) throw new Error(result?.error || 'Opening folders requires Hermes Desktop.')
    } catch (error) {
      host.notifyError(error, 'Could not open the Strata models folder')
    }
  }

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
          jsx('button', {
            type: 'button', className: buttonClass, disabled: !connected || !modelsFolder || data?.operation === 'wipe',
            onClick: () => void openModelsFolder(), children: 'Open models folder',
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
      jsx(DownloadProgress, {
        transfer: data?.download, active: data?.operation === 'download', busy: pending || !connected, pausing,
        onPause: () => void pauseDownload(), onResume: () => void run('resume-download'),
      }),
      data?.log && jsxs('details', {
        children: [
          jsx('summary', { className: 'cursor-pointer', children: 'Activity log' }),
          jsx('pre', { className: 'mt-2 max-h-64 overflow-auto whitespace-pre-wrap text-xs', children: data.log }),
        ],
      }),
      data?.ready && jsx(ModelSuggestions, { catalog: models, ...modelButtons }),
      data?.ready && jsx(HuggingFaceModels, { ctx, ...modelButtons }, `hf:${scopeKey}`),
      modelDownloads.length > 0 && jsxs('section', {
        className: sectionClass,
        children: [
          jsx('h2', { className: 'font-medium', children: 'Downloaded models' }),
          jsx('ul', {
            className: 'divide-y divide-(--ui-stroke-secondary)',
            children: modelDownloads.map(model => jsxs('li', {
              className: 'flex flex-wrap items-center justify-between gap-2 py-2',
              children: [
                jsxs('div', { className: 'min-w-0 break-words', children: [
                  jsx('div', { children: model.title }),
                  jsx('div', {
                    className: 'text-(--ui-text-secondary)',
                    children: [model.repo, downloadSize(model.bytes)].filter(Boolean).join(' · '),
                  }),
                ] }),
                jsx('button', {
                  type: 'button', className: buttonClass, disabled: !connected || pending,
                  'aria-label': `Delete ${model.title}`,
                  onClick: () => setDeleteModel({ kind: 'model', id: model.id, name: model.title, scope: scopeKey }),
                  children: 'Delete model',
                }),
              ],
            }, model.id)),
          }),
        ],
      }),
      deleteModel?.scope === scopeKey && jsx(LibraryDeleteDialog, {
        ctx, target: deleteModel, onClose: () => setDeleteModel(null),
        onDeleted: async () => {
          await queryClient.invalidateQueries({ queryKey: libraryKey, exact: true })
          await status.refetch()
        },
      }),
      (library.isError || library.data?.error) && jsx('p', { role: 'alert', children: library.data?.error ?? 'Could not load model downloads and profiles.' }),
      library.data?.profiles && jsx(ModelProfiles, {
        ctx, queryScope, library: library.data, revision: data?.ready ? data.revision : null, busy: pending || !connected,
        onSave: () => queryClient.invalidateQueries({ queryKey: libraryKey, exact: true }),
      }, `profiles:${scopeKey}`),
      connected && data && jsx(ServingControls, { ctx, serving }, `serving:${scopeKey}`),
    ],
  })
}

function VersionPill({ ctx }) {
  const connection = useValue(host.state.connectionId)
  const profile = useValue(host.state.profile)
  const gateway = useValue(host.state.gateway)
  const queryKey = [ctx.source, 'version-status', connection, profile]
  const { data, isError, isFetching, refetch } = useQuery({
    queryKey,
    queryFn: () => ctx.rest('/version-status'),
    enabled: gateway === 'open',
    staleTime: pluginUpdateInterval,
    refetchInterval: pluginUpdateInterval,
    refetchIntervalInBackground: true,
    refetchOnReconnect: 'always',
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
    return jsx('button', {
      type: 'button', className: `${baseClass} hover:text-(--ui-text-secondary)`,
      disabled: gateway !== 'open' || isFetching,
      'aria-label': 'Check for Strata Backend updates',
      title: data?.error ?? (isError ? 'Retry Strata Backend update check' : 'Check for Strata Backend updates'),
      onClick: () => void refetch(), children: isFetching ? `${label} · Checking…` : label,
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

function followDesktopModel(ctx) {
  if (!window.hermesDesktop) return
  const windowKey = Symbol.for('strata-backend:desktop-window')
  const client = window[windowKey] ?? (window[windowKey] = crypto.randomUUID())
  let sequence = Date.now() * 1000
  let sending = false
  let queued = false
  let claimFocus = document.hasFocus()
  let closed = false
  let lastModel = ''
  let reportedError = ''
  let observedScope = ''
  let observedModel = null
  let load = false
  const sessionModels = new Map()
  const sessionProbes = new Set()

  function send(model, focus, exiting = false) {
    sequence = Math.max(sequence + 1, Date.now() * 1000)
    return ctx.rest('/runtime/desktop-selection', {
      method: 'POST', body: { client, sequence, model, focus, load, closed: exiting }, timeoutMs: 16_000,
    })
  }

  async function refresh() {
    if (closed || host.state.gateway.get() !== 'open') return
    if (sending) { queued = true; return }
    sending = true
    try {
      const active = host.state.activeSessionId.get()
      const sid = host.state.focusedSessionId.get() ?? active
      const connection = host.state.connectionId.get()
      const profile = host.state.profile.get()
      const scope = JSON.stringify([connection, profile, sid])
      if (scope !== observedScope) {
        observedScope = scope
        observedModel = null
        load = false
      }
      let model = host.state.model.get()
      if (sid) {
        const owner = host.state.focusedSessionOwner.get()
        if (sid !== active && owner && (owner.connectionId !== (connection ?? 'local') || owner.profile !== profile)) return
        // The primary composer paints model picks optimistically. A live chat's
        // session.info is authoritative, including switches that are deferred or rolled back.
        const observed = sessionModels.get(scope)
        if ((!observed || (sid === active && observed.model !== model)) && !sessionProbes.has(scope)) {
          // Confirm the session independently so a slow RPC cannot stop the desktop lease.
          sessionProbes.add(scope)
          void host.request('session.info', { session_id: sid }).then(info => {
            const focused = host.state.focusedSessionId.get() ?? host.state.activeSessionId.get()
            if (closed || sid !== focused || connection !== host.state.connectionId.get() || profile !== host.state.profile.get()) return
            if (sessionModels.get(scope) === observed && typeof info?.model === 'string' && info.model !== observed?.model) {
              sessionModels.set(scope, { model: info.model })
              void refresh()
            }
          }).catch(() => undefined).finally(() => sessionProbes.delete(scope))
        }
        model = sessionModels.get(scope)?.model
      }
      if (model) {
        if (observedModel !== null && model !== observedModel) load = true
        observedModel = model
        lastModel = model
      }
      const focus = claimFocus
      claimFocus = false
      const result = await send(model || null, focus)
      if (result?.error && result.error !== reportedError && !result.error.includes('already running')) {
        reportedError = result.error
        host.notifyError(new Error(result.error), 'Could not update the Strata model selection')
      } else if (result?.ok) {
        reportedError = ''
      }
    } catch {
      // Gateway reconnection and the periodic heartbeat retry this selection.
    } finally {
      sending = false
      if (queued && !closed) {
        queued = false
        void refresh()
      }
    }
  }

  const refreshSoon = () => void refresh()
  for (const state of [host.state.model, host.state.gateway, host.state.profile, host.state.connectionId,
    host.state.activeSessionId, host.state.focusedSessionId]) {
    ctx.onDispose(state.listen(refreshSoon))
  }
  ctx.onEvent('session.info', event => {
    if (event.session_id && typeof event.payload?.model === 'string') {
      const connection = host.state.connectionId.get()
      const profile = host.state.profile.get()
      if (event.profile && event.profile !== profile) return
      sessionModels.set(JSON.stringify([connection, profile, event.session_id]), { model: event.payload.model })
      if (event.session_id === (host.state.focusedSessionId.get() ?? host.state.activeSessionId.get())) refreshSoon()
    }
  })
  ctx.addEventListener(window, 'focus', () => { claimFocus = true; refreshSoon() })
  ctx.addEventListener(window, 'pagehide', () => {
    closed = true
    if (host.state.gateway.get() === 'open') void send(lastModel, false, true).catch(() => undefined)
  })
  ctx.onDispose(() => { closed = true })
  ctx.setInterval(refreshSoon, 4000)
  refreshSoon()
}

export default {
  id: 'strata-backend',
  name: title,
  register(ctx) {
    followDesktopModel(ctx)
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
