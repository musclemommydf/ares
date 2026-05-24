// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

import { Component } from 'react'
import { AlertTriangle } from 'lucide-react'

/**
 * Catches render/runtime errors in a subtree so one misbehaving panel (e.g. a
 * Plotly 3-D chart that fails to get a WebGL context) shows a recoverable
 * message instead of blanking the whole app. `resetKey` — when it changes the
 * boundary clears its error and re-renders the children (so switching tabs after
 * a crash works without a page reload).
 */
export default class ErrorBoundary extends Component {
  constructor(props) {
    super(props)
    this.state = { error: null }
  }

  static getDerivedStateFromError(error) {
    return { error }
  }

  componentDidCatch(error, info) {
    // eslint-disable-next-line no-console
    console.error('[ErrorBoundary]', this.props.label || '', error, info?.componentStack)
  }

  componentDidUpdate(prevProps) {
    if (this.state.error && prevProps.resetKey !== this.props.resetKey) {
      this.setState({ error: null })
    }
  }

  render() {
    if (this.state.error) {
      if (this.props.fallback) return this.props.fallback(this.state.error, () => this.setState({ error: null }))
      return (
        <div style={{
          height: '100%', minHeight: 120, display: 'flex', flexDirection: 'column',
          alignItems: 'center', justifyContent: 'center', gap: 8, padding: 20,
          color: '#f0883e', textAlign: 'center', fontSize: 12,
        }}>
          <AlertTriangle size={20} />
          <div style={{ color: '#e6edf3', fontWeight: 600 }}>
            {this.props.label ? `${this.props.label} hit an error` : 'This panel hit an error'}
          </div>
          <div style={{ color: '#8b949e', maxWidth: 460, wordBreak: 'break-word' }}>
            {String(this.state.error?.message || this.state.error)}
          </div>
          <button className="btn btn-ghost" style={{ fontSize: 11, padding: '3px 10px', marginTop: 4 }}
            onClick={() => this.setState({ error: null })}>
            Retry
          </button>
        </div>
      )
    }
    return this.props.children
  }
}
