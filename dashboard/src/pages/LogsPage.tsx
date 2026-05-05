import { useEffect, useState } from 'react'
import { fetchAPI } from '../api/client'

export default function LogsPage() {
  const [health, setHealth] = useState<unknown>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false

    fetchAPI<unknown>('/health')
      .then((data) => {
        if (cancelled) return
        setHealth(data)
      })
      .catch((e) => {
        if (cancelled) return
        setError(e instanceof Error ? e.message : String(e))
      })

    return () => {
      cancelled = true
    }
  }, [])

  return (
    <div className="p-6">
      <h1 className="text-xl font-semibold">Logs</h1>
      <p className="mt-2 text-sm opacity-70">
        Backend health (proxy via Vite): <span className="font-mono">GET /health</span>
      </p>

      {error ? (
        <pre className="mt-4 rounded-lg bg-red-50 p-4 text-sm text-red-700">{error}</pre>
      ) : (
        <pre className="mt-4 rounded-lg bg-neutral-100 p-4 text-sm">
          {health ? JSON.stringify(health, null, 2) : 'Loading...'}
        </pre>
      )}
    </div>
  )
}
