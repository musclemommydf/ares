// SPDX-License-Identifier: MIT OR Apache-2.0
// Copyright (c) 2026 Ares

//! ares_native — optional Rust acceleration (Track D, D4).
//!
//! This is the *seam*, not a rewrite. `app.core.native` imports this module if the
//! wheel is built and falls back to pure Python otherwise, so Ares always runs.
//! Hot-path ports land here only when their D4 profiling trigger fires (see the
//! D4 table in ROADMAP.md):
//!   - real-time multi-channel IQ pipeline  (live_df / manager)
//!   - multi-VFO channelizer / squelch
//!   - DBPSK NIC modem
//!   - ITM inner loop
//!
//! For now it exposes a wiring proof so the build + fallback path are testable.

use pyo3::prelude::*;

/// Version of the native module (proves the wheel loaded).
#[pyfunction]
fn version() -> &'static str {
    env!("CARGO_PKG_VERSION")
}

/// Tiny reference kernel — sum of squares. Stands in for a real inner loop until
/// a candidate is promoted here; lets `app.core.native` verify dispatch + that
/// results match the pure-Python path.
#[pyfunction]
fn sum_squares(xs: Vec<f64>) -> f64 {
    xs.iter().map(|x| x * x).sum()
}

#[pymodule]
fn ares_native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(version, m)?)?;
    m.add_function(wrap_pyfunction!(sum_squares, m)?)?;
    Ok(())
}
