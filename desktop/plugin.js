import { ROUTES_AREA, SIDEBAR_NAV_AREA, STATUSBAR_AREAS, host, haptic, useQuery, queryClient, useValue } from '@hermes/plugin-sdk'
import { useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const title = 'Strata Backend'
const path = '/strata-backend'
const hour = 60 * 60 * 1000

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
      render: () => jsx('h1', { className: 'p-3 text-sm font-medium', children: title }),
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
