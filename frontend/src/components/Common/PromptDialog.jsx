/**
 * PromptDialog — Electron-safe replacement for window.prompt.
 *
 * Electron's BrowserWindow disables window.prompt() by default; it returns
 * null immediately without showing anything. This component is a real React
 * modal that resolves a Promise — same call-site shape as window.prompt:
 *
 *     const v = await promptUser({ title: 'Rename', defaultValue: 'foo' })
 *     // v = string | null  (null when user cancelled)
 *
 * Provider is mounted once at the app root. The `promptUser()` helper looks
 * up the active resolver registered by the provider and shows the dialog.
 * No context boilerplate at the call site.
 */
import { useEffect, useRef, useState } from 'react'

let _resolverRef = { fn: null }

/** Top-level helper. Resolves to string|number|null. */
export function promptUser({ title = 'Input', message = '', defaultValue = '',
                              placeholder = '', type = 'text',
                              min, max, step,
                              okLabel = 'OK', cancelLabel = 'Cancel' } = {}) {
  return new Promise((resolve) => {
    if (!_resolverRef.fn) { resolve(window.prompt(title, String(defaultValue))); return }
    _resolverRef.fn({ title, message, defaultValue, placeholder, type, min, max, step, okLabel, cancelLabel, resolve })
  })
}

/** Convenience: numeric prompt. Coerces to Number, returns null on cancel/NaN. */
export async function promptNumber(opts) {
  const v = await promptUser({ type: 'number', ...opts })
  if (v == null || v === '') return null
  const n = Number(v)
  return Number.isFinite(n) ? n : null
}


/** Mount once near the root. Renders the modal when prompt is active. */
export default function PromptDialogProvider() {
  const [state, setState] = useState(null)         // { title, message, defaultValue, ..., resolve } | null
  const inputRef = useRef(null)

  useEffect(() => {
    _resolverRef.fn = (req) => setState({ ...req, value: String(req.defaultValue ?? '') })
    return () => { _resolverRef.fn = null }
  }, [])

  useEffect(() => {
    if (state && inputRef.current) {
      inputRef.current.focus()
      inputRef.current.select?.()
    }
  }, [state])

  if (!state) return null

  const close = (value) => {
    const r = state.resolve
    setState(null)
    r?.(value)
  }
  const onSubmit = (e) => { e?.preventDefault?.(); close(state.value) }
  const onCancel = () => close(null)
  const onKey = (e) => { if (e.key === 'Escape') onCancel() }

  return (
    <div onKeyDown={onKey} style={{
      position: 'fixed', inset: 0, zIndex: 100000,
      background: 'rgba(0,0,0,0.55)', display: 'flex',
      alignItems: 'center', justifyContent: 'center',
    }}>
      <form onSubmit={onSubmit} style={{
        background: '#161b22', border: '1px solid #30363d', borderRadius: 10,
        boxShadow: '0 10px 30px rgba(0,0,0,0.8)',
        padding: '14px 16px', minWidth: 320, maxWidth: 480, color: '#e6edf3',
      }}>
        <div style={{ fontWeight: 700, fontSize: 13, marginBottom: 6 }}>{state.title}</div>
        {state.message && (
          <div style={{ fontSize: 11, color: '#8b949e', marginBottom: 8 }}>{state.message}</div>
        )}
        <input
          ref={inputRef}
          type={state.type}
          value={state.value}
          placeholder={state.placeholder}
          min={state.min} max={state.max} step={state.step}
          onChange={(e) => setState((s) => ({ ...s, value: e.target.value }))}
          style={{
            width: '100%', padding: '6px 8px', fontSize: 13,
            background: '#0d1117', border: '1px solid #30363d',
            color: '#e6edf3', borderRadius: 6, outline: 'none',
          }}
        />
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 12 }}>
          <button type="button" onClick={onCancel} style={btnStyle('#21262d', '#c9d1d9', '#30363d')}>
            {state.cancelLabel}
          </button>
          <button type="submit" style={btnStyle('#1f6feb', '#fff', '#1f6feb')}>
            {state.okLabel}
          </button>
        </div>
      </form>
    </div>
  )
}

const btnStyle = (bg, fg, border) => ({
  padding: '5px 12px', fontSize: 12, fontWeight: 600,
  background: bg, color: fg, border: `1px solid ${border}`,
  borderRadius: 6, cursor: 'pointer',
})
