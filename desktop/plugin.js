import { ROUTES_AREA, SIDEBAR_NAV_AREA, STATUSBAR_AREAS, host, haptic, useQuery, queryClient, useValue } from '@hermes/plugin-sdk'
import { useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const title = 'Strata Backend'
const path = '/strata-backend'
const hour = 60 * 60 * 1000

function ModelSuggestions({ catalog }) {
  const { recommendation, families } = catalog.data ?? {}
  if (catalog.isPending) return jsx('p', { children: 'Reading Strata’s model catalog…' })
  if (catalog.isError || catalog.data?.error) {
    return jsx('p', { role: 'alert', children: catalog.data?.error ?? 'Could not load model suggestions.' })
  }

  const context = recommendation?.context ? `${Math.round(recommendation.context / 1024)}K context` : null
  const download = recommendation?.download_gb == null ? null : `${recommendation.download_gb} GB download`
  return jsxs('section', {
    className: 'flex flex-col gap-3 border-t border-(--ui-stroke-secondary) pt-4',
    children: [
      jsx('h2', { className: 'font-medium', children: 'Suggested models' }),
      jsxs('div', {
        className: 'flex flex-col gap-1 rounded-md border border-(--ui-stroke-secondary) p-3',
        children: [
          recommendation?.title && jsx('strong', { children: recommendation.title }),
          (context || download) && jsx('p', { children: [context, download].filter(Boolean).join(' · ') }),
          jsx('p', { className: 'text-(--ui-text-secondary)', children: recommendation?.why }),
          ...(recommendation?.notes ?? []).map(note => jsx('p', { children: note }, note)),
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

function StrataPage({ ctx }) {
  const connection = useValue(host.state.connectionId)
  const profile = useValue(host.state.profile)
  const connected = useValue(host.state.gateway) === 'open'
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

  async function run(operation) {
    if (requestLock.current || data?.operation) return
    requestLock.current = true
    setRequesting(true)
    setConfirmWipe(false)
    try {
      haptic('tap')
      const result = await ctx.rest(`/runtime/${operation}`, { method: 'POST' })
      if (!result.ok) throw new Error(result.error || 'The operation failed.')
      queryClient.setQueryData(statusKey, previous => ({ ...previous, operation }))
      await queryClient.invalidateQueries({ queryKey: statusKey, exact: true })
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
  }
  const busyLabel = operationLabels[data?.operation]
  const updateAvailable = Boolean(latest.data?.update_available)
  const primaryAction = updateAvailable ? 'update' : 'setup'
  const primaryLabel = updateAvailable ? 'Update Strata' : data?.revision ? 'Set up Strata' : 'Download and set up'
  const stateLabel = data?.ready ? 'Engine ready' : data?.revision ? 'Setup incomplete' : 'Not downloaded'
  const latestRevision = latest.data?.latest
  const buttonClass = 'rounded-md border border-(--ui-stroke-secondary) px-3 py-1.5 text-sm hover:text-(--ui-accent) disabled:opacity-50'

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
      data?.ready && !pending && jsx(ModelSuggestions, { catalog: models }),
      data?.log && jsxs('details', {
        children: [
          jsx('summary', { className: 'cursor-pointer', children: 'Setup log' }),
          jsx('pre', { className: 'mt-2 max-h-64 overflow-auto whitespace-pre-wrap text-xs', children: data.log }),
        ],
      }),
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
