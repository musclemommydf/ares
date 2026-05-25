// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

//! ares_native — optional Rust acceleration (Track D, D4).
//!
//! The seam, not a rewrite. `app.core.native` imports this if the wheel is built
//! and the Python callers fall back to their pure-Python paths otherwise, so Ares
//! always runs. Ported here are the hot loops that are (a) pure scalar Python —
//! NOT numpy-backed — and (b) on a per-pixel / per-path critical path:
//!
//!   - terrain diffraction (diffraction.py): all five knife-edge models, called
//!     per-pixel from the coverage/raster path. Zero numpy in the original.
//!   - ITM horizon analysis (_hzns): the one sequential, non-vectorisable loop in
//!     the Longley-Rice profile path (_zlsq1/_dlthx are already numpy → left in
//!     Python).
//!
//! Numeric parity with the Python originals is asserted by test_native_parity.

use pyo3::prelude::*;

const C: f64 = 3e8;

// ── proof-of-wiring kernels (kept for the native shim / benchmarks) ──────────
#[pyfunction]
fn version() -> &'static str {
    env!("CARGO_PKG_VERSION")
}

#[pyfunction]
fn sum_squares(xs: Vec<f64>) -> f64 {
    xs.iter().map(|x| x * x).sum()
}

// ── diffraction helpers (mirror diffraction.py exactly) ──────────────────────
fn fresnel_v(h: f64, d1: f64, d2: f64, wl: f64) -> f64 {
    if d1 <= 0.0 || d2 <= 0.0 || wl <= 0.0 {
        return 0.0;
    }
    h * (2.0 * (d1 + d2) / (wl * d1 * d2)).sqrt()
}

fn knife_edge_loss_db(v: f64) -> f64 {
    if v < -0.7 {
        return 0.0;
    }
    let inner = ((v - 0.1).powi(2) + 1.0).sqrt() + v - 0.1;
    if inner <= 0.0 {
        return 0.0;
    }
    (6.9 + 20.0 * inner.log10()).max(0.0)
}

fn los_height_at(d: f64, d_total: f64, h_tx: f64, h_rx: f64) -> f64 {
    if d_total <= 0.0 {
        return h_tx;
    }
    h_tx + (h_rx - h_tx) * d / d_total
}

/// (clearance, distance-from-start) for each interior point.
fn clearances(elev: &[f64], dist: &[f64], tx_h: f64, rx_h: f64) -> Vec<(f64, f64)> {
    let n = elev.len();
    if n < 3 {
        return Vec::new();
    }
    let h_tx = elev[0] + tx_h;
    let h_rx = elev[n - 1] + rx_h;
    let d_total = dist[n - 1] - dist[0];
    let mut out = Vec::with_capacity(n - 2);
    for i in 1..n - 1 {
        let d = dist[i] - dist[0];
        let los = los_height_at(d, d_total, h_tx, h_rx);
        out.push((elev[i] - los, dist[i] - dist[0]));
    }
    out
}

fn single_knife_edge(elev: &[f64], dist: &[f64], tx_h: f64, rx_h: f64, freq: f64) -> f64 {
    let n = elev.len();
    if n < 3 || freq <= 0.0 {
        return 0.0;
    }
    let wl = C / freq;
    let d_total = dist[n - 1] - dist[0];
    let cl = clearances(elev, dist, tx_h, rx_h);
    if cl.is_empty() {
        return 0.0;
    }
    // worst (highest) obstacle — matches Python max(..., key=clearance)
    let mut best = cl[0];
    for &c in &cl[1..] {
        if c.0 > best.0 {
            best = c;
        }
    }
    if best.0 <= 0.0 {
        return 0.0;
    }
    let (d1, d2) = (best.1, d_total - best.1);
    if d1 <= 0.0 || d2 <= 0.0 {
        return 0.0;
    }
    knife_edge_loss_db(fresnel_v(best.0, d1, d2, wl)).max(0.0)
}

fn epstein_peterson(elev: &[f64], dist: &[f64], tx_h: f64, rx_h: f64, freq: f64) -> f64 {
    let n = elev.len();
    if n < 3 || freq <= 0.0 {
        return 0.0;
    }
    let wl = C / freq;
    let d_total = dist[n - 1] - dist[0];
    let mut total = 0.0;
    for (h, d) in clearances(elev, dist, tx_h, rx_h) {
        if h <= 0.0 {
            continue;
        }
        let (d1, d2) = (d, d_total - d);
        if d1 <= 0.0 || d2 <= 0.0 {
            continue;
        }
        total += knife_edge_loss_db(fresnel_v(h, d1, d2, wl)).max(0.0);
    }
    total
}

fn bullington(elev: &[f64], dist: &[f64], tx_h: f64, rx_h: f64, freq: f64) -> f64 {
    let n = elev.len();
    if n < 3 || freq <= 0.0 {
        return 0.0;
    }
    let wl = C / freq;
    let d_total = dist[n - 1] - dist[0];
    let h_tx = elev[0] + tx_h;
    let h_rx = elev[n - 1] + rx_h;

    let mut max_slope_tx = f64::NEG_INFINITY;
    for i in 1..n - 1 {
        let d = dist[i] - dist[0];
        if d <= 0.0 {
            continue;
        }
        let slope = (elev[i] - h_tx) / d;
        if slope > max_slope_tx {
            max_slope_tx = slope;
        }
    }
    let mut max_slope_rx = f64::NEG_INFINITY;
    for i in 1..n - 1 {
        let d = d_total - (dist[i] - dist[0]);
        if d <= 0.0 {
            continue;
        }
        let slope = (elev[i] - h_rx) / d;
        if slope > max_slope_rx {
            max_slope_rx = slope;
        }
    }

    let denom = max_slope_tx + max_slope_rx;
    let d_edge = if denom.abs() < 1e-9 {
        d_total / 2.0
    } else {
        let e = (h_rx - h_tx + max_slope_rx * d_total) / denom;
        e.max(1.0).min(d_total - 1.0)
    };
    let h_edge = h_tx + max_slope_tx * d_edge;
    let clearance = h_edge - los_height_at(d_edge, d_total, h_tx, h_rx);
    if clearance <= 0.0 {
        return 0.0;
    }
    let (d1, d2) = (d_edge, d_total - d_edge);
    if d1 <= 0.0 || d2 <= 0.0 {
        return 0.0;
    }
    knife_edge_loss_db(fresnel_v(clearance, d1, d2, wl)).max(0.0)
}

fn deygout_recurse(elev: &[f64], dist: &[f64], tx_h: f64, rx_h: f64, freq: f64, depth: u32) -> f64 {
    let n = elev.len();
    if n < 3 || depth > 4 {
        return 0.0;
    }
    let wl = C / freq;
    let d_start = dist[0];
    let d_total = dist[n - 1] - d_start;
    let mut best_v = f64::NEG_INFINITY;
    let mut best_idx: isize = -1;
    for i in 1..n - 1 {
        let d = dist[i] - d_start;
        let h_clear = elev[i] - los_height_at(d, d_total, tx_h, rx_h);
        let (d1, d2) = (d, d_total - d);
        if d1 <= 0.0 || d2 <= 0.0 {
            continue;
        }
        let v = fresnel_v(h_clear, d1, d2, wl);
        if v > best_v {
            best_v = v;
            best_idx = i as isize;
        }
    }
    if best_idx < 0 || best_v <= 0.0 {
        return 0.0;
    }
    let bi = best_idx as usize;
    let mut loss = knife_edge_loss_db(best_v).max(0.0);
    loss += deygout_recurse(&elev[..=bi], &dist[..=bi], tx_h, elev[bi], freq, depth + 1);
    loss += deygout_recurse(&elev[bi..], &dist[bi..], elev[bi], rx_h, freq, depth + 1);
    loss
}

fn deygout(elev: &[f64], dist: &[f64], tx_h: f64, rx_h: f64, freq: f64) -> f64 {
    let n = elev.len();
    if n < 3 || freq <= 0.0 {
        return 0.0;
    }
    let h_tx = elev[0] + tx_h;
    let h_rx = elev[n - 1] + rx_h;
    deygout_recurse(elev, dist, h_tx, h_rx, freq, 0)
}

fn giovanelli(elev: &[f64], dist: &[f64], tx_h: f64, rx_h: f64, freq: f64) -> f64 {
    let n = elev.len();
    if n < 3 || freq <= 0.0 {
        return 0.0;
    }
    let ep = epstein_peterson(elev, dist, tx_h, rx_h, freq);
    if ep <= 0.0 {
        return 0.0;
    }
    let bull = bullington(elev, dist, tx_h, rx_h, freq);
    let j = 0.2;
    let combined = bull + j * (ep - bull);
    bull.max(combined)
}

/// Dispatch by model name; unknown ⇒ deygout (matches diffraction.py default).
#[pyfunction]
fn diffraction_db(
    model: &str,
    elevations: Vec<f64>,
    distances: Vec<f64>,
    tx_height_m: f64,
    rx_height_m: f64,
    freq_hz: f64,
) -> f64 {
    let (e, d) = (&elevations[..], &distances[..]);
    match model {
        "single_knife_edge" => single_knife_edge(e, d, tx_height_m, rx_height_m, freq_hz),
        "epstein_peterson" => epstein_peterson(e, d, tx_height_m, rx_height_m, freq_hz),
        "bullington" => bullington(e, d, tx_height_m, rx_height_m, freq_hz),
        "giovanelli" => giovanelli(e, d, tx_height_m, rx_height_m, freq_hz),
        _ => deygout(e, d, tx_height_m, rx_height_m, freq_hz),
    }
}

// ── ITM horizon analysis (mirror itm_its._hzns exactly) ──────────────────────
/// Returns (the0, the1, dl0, dl1) — the two horizon elevation angles + distances.
#[pyfunction]
fn itm_hzns(pfl: Vec<f64>, hg0: f64, hg1: f64, gme: f64, dist: f64) -> (f64, f64, f64, f64) {
    let np_ = pfl[0] as usize;
    let xi = pfl[1];
    let za = pfl[2] + hg0;
    let zb = pfl[np_ + 2] + hg1;
    let qc = 0.5 * gme;
    let mut q = qc * dist;
    let mut the1 = (zb - za) / dist;
    let mut the0 = the1 - q;
    the1 = -the1 - q;
    let mut dl0 = dist;
    let mut dl1 = dist;
    if np_ >= 2 {
        let mut sa = 0.0;
        let mut sb = dist;
        let mut wq = true;
        for i in 1..np_ {
            sa += xi;
            sb -= xi;
            q = pfl[i + 2] - (qc * sa + the0) * sa - za;
            if q > 0.0 {
                the0 += q / sa;
                dl0 = sa;
                wq = false;
            }
            if !wq {
                q = pfl[i + 2] - (qc * sb + the1) * sb - zb;
                if q > 0.0 {
                    the1 += q / sb;
                    dl1 = sb;
                }
            }
        }
    }
    (the0, the1, dl0, dl1)
}

// ── DVB FEC chain (mirror dvb_fec.py / dvb_inner_fec.py exactly) ──────────────
use std::sync::OnceLock;

const PRIM: u16 = 0x11D;
const NROOTS: usize = 16;

struct Gf {
    exp: [u8; 512],
    log: [u8; 256],
}

fn gf() -> &'static Gf {
    static G: OnceLock<Gf> = OnceLock::new();
    G.get_or_init(|| {
        let mut exp = [0u8; 512];
        let mut log = [0u8; 256];
        let mut x: u16 = 1;
        for i in 0..255 {
            exp[i] = x as u8;
            log[x as usize] = i as u8;
            x <<= 1;
            if x & 0x100 != 0 {
                x ^= PRIM;
            }
        }
        for i in 255..512 {
            exp[i] = exp[i - 255];
        }
        Gf { exp, log }
    })
}

#[inline]
fn gmul(g: &Gf, a: u8, b: u8) -> u8 {
    if a == 0 || b == 0 {
        0
    } else {
        g.exp[g.log[a as usize] as usize + g.log[b as usize] as usize]
    }
}
#[inline]
fn gdiv(g: &Gf, a: u8, b: u8) -> u8 {
    if a == 0 || b == 0 {
        0
    } else {
        g.exp[(g.log[a as usize] as i32 - g.log[b as usize] as i32).rem_euclid(255) as usize]
    }
}
#[inline]
fn gpow(g: &Gf, p: i32) -> u8 {
    g.exp[p.rem_euclid(255) as usize]
}

/// Horner eval, high-order coefficient first.
fn poly_eval(g: &Gf, poly: &[u8], x: u8) -> u8 {
    let mut y = poly[0];
    for &c in &poly[1..] {
        y = gmul(g, y, x) ^ c;
    }
    y
}
/// Eval a low-order-first polynomial at x.
fn eval_lo(g: &Gf, poly_lo: &[u8], x: u8) -> u8 {
    let mut y = 0u8;
    let mut xp = 1u8;
    for &c in poly_lo {
        y ^= gmul(g, c, xp);
        xp = gmul(g, xp, x);
    }
    y
}
fn poly_scale(g: &Gf, p: &[u8], s: u8) -> Vec<u8> {
    p.iter().map(|&c| gmul(g, c, s)).collect()
}
fn poly_mul(g: &Gf, p: &[u8], q: &[u8]) -> Vec<u8> {
    let mut r = vec![0u8; p.len() + q.len() - 1];
    for (i, &a) in p.iter().enumerate() {
        if a != 0 {
            for (j, &b) in q.iter().enumerate() {
                r[i + j] ^= gmul(g, a, b);
            }
        }
    }
    r
}
/// Right-aligned XOR add (matches dvb_fec._poly_add).
fn poly_add(p: &[u8], q: &[u8]) -> Vec<u8> {
    let n = p.len().max(q.len());
    let mut r = vec![0u8; n];
    for i in 0..p.len() {
        r[i + n - p.len()] = p[i];
    }
    for i in 0..q.len() {
        r[i + n - q.len()] ^= q[i];
    }
    r
}

/// RS(204,188) errors-only decode → (Some(data188), n_errors) or (None, -1).
#[pyfunction]
fn rs_decode_204(code: Vec<u8>) -> (Option<Vec<u8>>, i32) {
    if code.len() != 204 {
        return (None, -1);
    }
    let g = gf();
    const PAD: usize = 255 - 204;
    let mut msg = vec![0u8; PAD];
    msg.extend_from_slice(&code);
    let n = msg.len(); // 255

    let mut synd = vec![0u8; NROOTS + 1];
    for i in 0..NROOTS {
        synd[i + 1] = poly_eval(g, &msg, gpow(g, i as i32));
    }
    if *synd.iter().max().unwrap() == 0 {
        return (Some(msg[PAD..PAD + 188].to_vec()), 0);
    }

    // Berlekamp-Massey → error locator Λ (high-order first)
    let mut err_loc: Vec<u8> = vec![1];
    let mut old_loc: Vec<u8> = vec![1];
    let synd_shift = synd.len() - NROOTS; // = 1
    for i in 0..NROOTS {
        let kk = i + synd_shift;
        let mut delta = synd[kk];
        for j in 1..err_loc.len() {
            delta ^= gmul(g, err_loc[err_loc.len() - (j + 1)], synd[kk - j]);
        }
        old_loc.push(0);
        if delta != 0 {
            if old_loc.len() > err_loc.len() {
                let new_loc = poly_scale(g, &old_loc, delta);
                old_loc = poly_scale(g, &err_loc, gdiv(g, 1, delta));
                err_loc = new_loc;
            }
            err_loc = poly_add(&err_loc, &poly_scale(g, &old_loc, delta));
        }
    }
    while !err_loc.is_empty() && err_loc[0] == 0 {
        err_loc.remove(0);
    }
    let errs = err_loc.len() as i32 - 1;
    if errs * 2 > NROOTS as i32 || errs == 0 {
        return (None, -1);
    }

    // Chien: Λ low-order first; error at index k ⇒ Λ(α^-(n-1-k)) = 0
    let lam_lo: Vec<u8> = err_loc.iter().rev().copied().collect();
    let mut err_pos: Vec<usize> = Vec::new();
    for k in 0..n {
        let xk_inv = gpow(g, -((n - 1 - k) as i32));
        if eval_lo(g, &lam_lo, xk_inv) == 0 {
            err_pos.push(k);
        }
    }
    if err_pos.len() as i32 != errs {
        return (None, -1);
    }

    // Forney
    let s_lo: Vec<u8> = synd[1..].to_vec();
    let omega_full = poly_mul(g, &lam_lo, &s_lo);
    let omega_lo = &omega_full[..NROOTS.min(omega_full.len())];
    let mut e = vec![0u8; n];
    for &k in &err_pos {
        let xk = gpow(g, (n - 1 - k) as i32);
        let xk_inv = gdiv(g, 1, xk);
        let mut denom = 1u8;
        for &k2 in &err_pos {
            if k2 != k {
                denom = gmul(g, denom, 1 ^ gmul(g, xk_inv, gpow(g, (n - 1 - k2) as i32)));
            }
        }
        if denom == 0 {
            return (None, -1);
        }
        e[k] = gdiv(g, eval_lo(g, omega_lo, xk_inv), denom);
    }
    let msg = poly_add(&msg, &e);
    for i in 0..NROOTS {
        if poly_eval(g, &msg, gpow(g, i as i32)) != 0 {
            return (None, -1);
        }
    }
    (Some(msg[PAD..PAD + 188].to_vec()), err_pos.len() as i32)
}

/// DVB energy-dispersal de-randomiser over whole 188-byte TS packets.
#[pyfunction]
fn dvb_derandomise(packets: Vec<u8>) -> Vec<u8> {
    let n_pkt = packets.len() / 188;
    // PRBS x^15+x^14+1, seed 0b100101010000000, over 8×187 payload bytes.
    let mut prbs = vec![0u8; 187 * 8];
    let mut reg: u16 = 0b100101010000000;
    for slot in prbs.iter_mut() {
        let mut b = 0u8;
        for _ in 0..8 {
            let bit = (((reg >> 14) ^ (reg >> 13)) & 1) as u8;
            b = (b << 1) | bit;
            reg = ((reg << 1) | bit as u16) & 0x7FFF;
        }
        *slot = b;
    }
    let mut out = packets;
    let mut grp = 0usize;
    while grp < n_pkt {
        let mut k = 0usize;
        for p in grp..(grp + 8).min(n_pkt) {
            let base = p * 188;
            out[base] = 0x47;
            for j in 1..188 {
                out[base + j] ^= prbs[k];
                k += 1;
            }
        }
        grp += 8;
    }
    out
}

/// Soft-decision Viterbi over the DVB-T K=7 trellis (G1=171₈, G2=133₈).
#[pyfunction]
fn viterbi_decode(soft_pairs: Vec<f64>, terminated: bool) -> Vec<u8> {
    const NSTATES: usize = 64;
    let t_len = soft_pairs.len() / 2;
    if t_len == 0 {
        return Vec::new();
    }
    // trellis: predecessors of each next-state (2 each)
    let g1 = 0o171u32;
    let g2 = 0o133u32;
    let mut pred = [[0usize; 2]; NSTATES];
    let mut pred_in = [[0u8; 2]; NSTATES];
    let mut pred_outidx = [[0usize; 2]; NSTATES];
    let mut filled = [0usize; NSTATES];
    for s in 0..NSTATES {
        for u in 0u8..2 {
            let reg = ((u as u32) << 6) | s as u32;
            let o0 = ((reg & g1).count_ones() & 1) as usize;
            let o1 = ((reg & g2).count_ones() & 1) as usize;
            let ns = (reg >> 1) as usize;
            let slot = filled[ns];
            filled[ns] += 1;
            pred[ns][slot] = s;
            pred_in[ns][slot] = u;
            pred_outidx[ns][slot] = (o0 << 1) | o1;
        }
    }
    // symbol per 2-bit output index, 0→+1 1→−1
    let sym0: [f64; 4] = [1.0, 1.0, -1.0, -1.0]; // 1 - 2*((i>>1)&1)
    let sym1: [f64; 4] = [1.0, -1.0, 1.0, -1.0]; // 1 - 2*(i&1)

    const NEG: f64 = -1e18;
    let mut pm = [NEG; NSTATES];
    pm[0] = 0.0;
    let mut tb = vec![[0u8; NSTATES]; t_len];
    let mut prev = vec![[0u8; NSTATES]; t_len];

    for t in 0..t_len {
        let r0 = soft_pairs[2 * t];
        let r1 = soft_pairs[2 * t + 1];
        let bm = [
            r0 * sym0[0] + r1 * sym1[0],
            r0 * sym0[1] + r1 * sym1[1],
            r0 * sym0[2] + r1 * sym1[2],
            r0 * sym0[3] + r1 * sym1[3],
        ];
        let mut newpm = [0f64; NSTATES];
        for ns in 0..NSTATES {
            let c0 = pm[pred[ns][0]] + bm[pred_outidx[ns][0]];
            let c1 = pm[pred[ns][1]] + bm[pred_outidx[ns][1]];
            let choose = if c1 > c0 { 1 } else { 0 }; // numpy argmax → first max
            newpm[ns] = if choose == 1 { c1 } else { c0 };
            tb[t][ns] = pred_in[ns][choose];
            prev[t][ns] = pred[ns][choose] as u8;
        }
        pm = newpm;
    }

    let mut end = 0usize;
    if !terminated {
        let mut best = pm[0];
        for s in 1..NSTATES {
            if pm[s] > best {
                best = pm[s];
                end = s;
            }
        }
    }
    let mut bits = vec![0u8; t_len];
    let mut s = end;
    for t in (0..t_len).rev() {
        bits[t] = tb[t][s];
        s = prev[t][s] as usize;
    }
    if terminated {
        bits.truncate(t_len - 6); // K-1 = 6 flush bits
    }
    bits
}

#[pymodule]
fn ares_native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(version, m)?)?;
    m.add_function(wrap_pyfunction!(sum_squares, m)?)?;
    m.add_function(wrap_pyfunction!(diffraction_db, m)?)?;
    m.add_function(wrap_pyfunction!(itm_hzns, m)?)?;
    m.add_function(wrap_pyfunction!(rs_decode_204, m)?)?;
    m.add_function(wrap_pyfunction!(dvb_derandomise, m)?)?;
    m.add_function(wrap_pyfunction!(viterbi_decode, m)?)?;
    Ok(())
}
