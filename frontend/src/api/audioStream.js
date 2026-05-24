// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

/**
 * SdrAudioPlayer — plays the backend's in-process demodulated audio.
 *
 * Opens `WS /api/v1/sdr/devices/{id}/audio/stream`, reads the JSON `audio_format`
 * header, then schedules the streamed mono int16 PCM frames through the Web Audio
 * API (the AudioContext resamples the stream rate to the device output rate). A
 * small lead buffer absorbs network jitter; on underrun the playhead re-bases.
 */
import { wsUrl } from './host'

export class SdrAudioPlayer {
  constructor() {
    this.ws = null
    this.ctx = null
    this.gain = null
    this.rate = 16000
    this.playTime = 0
    this.onState = null      // (state) => void  — {status, ...}
    this.onText = null       // (msg) => void    — decoded text lines (digital data modes)
  }

  start(deviceId, { frequency_hz, mode = 'nfm', bandwidth_hz = null } = {}) {
    this.stop()
    // Build the query, then let wsUrl() resolve the (possibly remote) backend host
    // and append the auth token — so Listen works against an appliance the same as
    // against localhost.
    let path = `/api/v1/sdr/devices/${encodeURIComponent(deviceId)}/audio/stream`
      + `?frequency_hz=${encodeURIComponent(Math.round(frequency_hz))}&mode=${encodeURIComponent(mode)}`
    if (bandwidth_hz) path += `&bandwidth_hz=${encodeURIComponent(Math.round(bandwidth_hz))}`
    const url = wsUrl(path)

    let ws
    try { ws = new WebSocket(url) } catch (e) { this.onState?.({ status: 'error', detail: String(e?.message || e) }); return }
    ws.binaryType = 'arraybuffer'
    this.ws = ws
    this.onState?.({ status: 'connecting' })
    ws.onmessage = (ev) => {
      if (typeof ev.data === 'string') {
        let m = null; try { m = JSON.parse(ev.data) } catch { return }
        if (m.type === 'audio_format') { this._open(m.sample_rate || 16000); this.onState?.({ status: 'playing', ...m }) }
        else if (m.type === 'decode') { this.onText?.(m) }
        else if (m.type === 'error') { this.onState?.({ status: 'error', detail: m.detail }); this.stop() }
        // m.type === 'ping' ignored
        return
      }
      this._enqueue(ev.data)
    }
    ws.onerror = () => this.onState?.({ status: 'error', detail: 'audio socket error' })
    ws.onclose = (ev) => this.onState?.({ status: 'stopped', code: ev?.code })
  }

  _open(rate) {
    this.rate = rate
    const AC = window.AudioContext || window.webkitAudioContext
    if (!AC) { this.onState?.({ status: 'error', detail: 'no Web Audio support' }); return }
    this.ctx = new AC()
    this.gain = this.ctx.createGain(); this.gain.gain.value = 1.0
    this.gain.connect(this.ctx.destination)
    this.playTime = this.ctx.currentTime + 0.2     // lead buffer
    if (this.ctx.state === 'suspended') this.ctx.resume().catch(() => {})
  }

  _enqueue(arrbuf) {
    if (!this.ctx) return
    const i16 = new Int16Array(arrbuf)
    if (!i16.length) return
    const f32 = new Float32Array(i16.length)
    for (let i = 0; i < i16.length; i++) f32[i] = i16[i] / 32768
    const buf = this.ctx.createBuffer(1, f32.length, this.rate)
    buf.copyToChannel(f32, 0)
    const src = this.ctx.createBufferSource()
    src.buffer = buf; src.connect(this.gain)
    const now = this.ctx.currentTime
    if (this.playTime < now) this.playTime = now + 0.05   // re-base after an underrun
    src.start(this.playTime)
    this.playTime += buf.duration
  }

  setVolume(v) { if (this.gain) this.gain.gain.value = Math.max(0, Math.min(2, Number(v) || 0)) }

  stop() {
    try { this.ws && this.ws.close() } catch { /* noop */ }
    this.ws = null
    try { this.ctx && this.ctx.close() } catch { /* noop */ }
    this.ctx = null; this.playTime = 0
  }
}
