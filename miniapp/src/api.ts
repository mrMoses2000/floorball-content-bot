import type { Bootstrap } from './types'
import { demo } from './demo'

const initData = () => window.Telegram?.WebApp.initData ?? ''

export class ApiError extends Error {
  constructor(message: string, readonly code: string) { super(message) }
}

async function request(path: string, options: RequestInit = {}): Promise<Bootstrap> {
  const response = await fetch(path, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      Authorization: `tma ${initData()}`,
      ...options.headers,
    },
  })
  const body = await response.json()
  if (!response.ok) throw new ApiError(body?.error?.message || 'Не удалось загрузить данные.', body?.error?.code || 'unknown')
  return body
}

export const api = {
  bootstrap: () => (import.meta.env.DEV || import.meta.env.VITE_DEMO_UI === 'true') && !initData()
    ? Promise.resolve(structuredClone(demo))
    : request('./api/miniapp/v1/bootstrap'),
  start: (mode: string) => request(`./api/miniapp/v1/sessions/${mode}`, { method: 'POST' }),
  language: (language: 'ru' | 'kz') =>
    request('./api/miniapp/v1/preferences/language', {
      method: 'PATCH', body: JSON.stringify({ language }),
    }),
  field: (sessionId: string, fieldPath: string, revision: number, value: unknown, clear = false) =>
    request(`./api/miniapp/v1/sessions/${sessionId}/fields/${fieldPath}`, {
      method: 'PATCH',
      body: JSON.stringify({ request_id: crypto.randomUUID(), revision, value, clear }),
    }),
}
