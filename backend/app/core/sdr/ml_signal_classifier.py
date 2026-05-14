"""
sdr/ml_signal_classifier.py — reference ML signal classifier for the UAS feed /
modulation identifier.

The rule-based classifier in ``video_exploit.classify_modulation`` /
``uas_video.classify_band`` stays the default and the interpretable fallback. This
module is the *concrete* implementation of the optional ML stage that
``uas_video.set_ml_classifier`` plugs in:

  * ``feature_vector(iq, fs)`` — a fixed-length numeric feature vector (occupied
    bandwidth, spectral flatness, envelope coefficient-of-variation, the OFDM
    cyclic-prefix autocorrelation peak + best FFT length + guard fraction, the 4th-
    order cumulant ratio |C40/C42|, a symbol-rate estimate, a roll-off estimate, the
    FM line-rate ratio, and a few spectral-correlation-function summary stats) — pure
    NumPy, no extra dependencies. Use it directly with a scikit-learn / XGBoost
    feature classifier, or as auxiliary inputs to a CNN.
  * ``load_model(path)`` — loads an ONNX model (``onnxruntime``) or a TorchScript /
    state-dict model (``torch``), whichever is installed.
  * ``Classifier`` — wraps a loaded model and exposes ``classify(iq, fs, band=None)
    -> {feed_type, confidence, probs, model}``, the contract ``set_ml_classifier``
    expects.
  * ``register(model_path, classes=None)`` — load + wrap + ``uas_video.set_ml_classifier(...)``.

How to train one (your model, not shipped — it must be trained on signals it'll see):
  1. Data: the **DroneRF** dataset (DJI / WiFi / Bluetooth IQ at 2.4 GHz), the
     **DeepSig RadioML** sets (modulation classes), and/or your own captures labelled
     with the rule-based classifier as weak labels (then hand-correct a subset).
  2. Model: a small 1-D CNN on the (normalised) IQ, or a 2-D CNN on the log-spectrogram
     or the spectral-correlation function, or — simplest — a RandomForest / gradient-
     boosted trees on ``feature_vector(iq, fs)``. Output one of ``DEFAULT_CLASSES``
     (Ares feed-type ids) plus an "unknown" class; train with class weights (the
     proprietary OFDM family is the hard part — OcuSync vs. Lightbridge vs. HDZero vs.
     Walksnail vs. a generic COFDM modem).
  3. Export: ``torch.onnx.export(model, dummy_iq, "ares_sig.onnx", ...)`` (or save a
     ``.pt`` TorchScript). Output should be class logits/probabilities over
     ``DEFAULT_CLASSES`` (override the order via ``register(path, classes=[...])``).
  4. Use: ``from app.core.sdr import ml_signal_classifier as ml; ml.register("ares_sig.onnx")``
     at startup (e.g. from ``main.py``'s lifespan) — the verdict is then ensembled with
     the rule-based one (boosts on agreement, adds an alternative on disagreement).
"""
from __future__ import annotations

import math
from typing import Optional

import numpy as np

from . import uas_video

DEFAULT_CLASSES = [
    "fm_analog_video_ntsc", "fm_analog_video_pal", "fm_analog_video_secam", "vsb_analog_video",
    "dvbt", "dvbt2", "dvbs", "dvbs2", "isdbt_1seg", "cofdm_mpegts", "qam_mpegts",
    "dji_ocusync", "dji_lightbridge", "hdzero", "walksnail", "cdl_becdl",
    "remote_id", "dji_droneid", "unknown_digital", "unknown_analog",
]

FEATURE_NAMES = [
    "occupied_bw_hz", "flatness", "envelope_cv",
    "ofdm_cp_corr", "ofdm_fft_len", "ofdm_guard_fraction", "ofdm_useful_symbol_us",
    "cumulant_ratio_c40_c42", "symbol_rate_hz", "rolloff",
    "fm_video_line_ratio", "scf_peak", "scf_peak_alpha_norm", "scf_kurtosis",
]


# ── feature extraction (pure NumPy) ─────────────────────────────────────────
def feature_dict(iq: np.ndarray, fs: float) -> dict:
    """Named features (a superset of the rule-based classifier's outputs + a small
    spectral-correlation-function summary). Missing values are 0.0."""
    x = np.asarray(iq, dtype=np.complex64)
    if x.size < 4096:
        return {k: 0.0 for k in FEATURE_NAMES}
    from . import video_exploit  # local import — avoids any import-order surprises
    f = video_exploit.classify_modulation(x, fs)
    out = {
        "occupied_bw_hz": float(f.get("occupied_bw_hz_est", 0.0)),
        "flatness": 0.0,
        "envelope_cv": float(f.get("envelope_cv", 0.0)),
        "ofdm_cp_corr": float(f.get("ofdm_cp_corr", 0.0)),
        "ofdm_fft_len": float(f.get("ofdm_fft_len", 0.0)),
        "ofdm_guard_fraction": float(f.get("ofdm_guard_fraction", 0.0)),
        "ofdm_useful_symbol_us": float(f.get("ofdm_useful_symbol_us", 0.0)),
        "cumulant_ratio_c40_c42": float(f.get("cumulant_ratio_c40_c42", 0.0)),
        "symbol_rate_hz": float(f.get("symbol_rate_hz_est", 0.0)),
        "rolloff": float(f.get("rolloff_est", 0.0)),
        "fm_video_line_ratio": float(f.get("fm_video_line_ratio", f.get("fm_line_ratio", 0.0))),
    }
    # spectral flatness (Wiener entropy of the PSD) — 0..1, ~1 = noise-/OFDM-like
    n = min(x.size, 1 << 16)
    seg = x[:n] - np.mean(x[:n])
    psd = np.abs(np.fft.fft(seg * np.hanning(n))) ** 2 + 1e-20
    out["flatness"] = round(float(np.exp(np.mean(np.log(psd))) / np.mean(psd)), 4)
    # crude cyclic spectral-correlation: |x|^2 demod -> its FFT -> peak away from DC
    sq = np.abs(seg) ** 2
    sq = sq - sq.mean()
    S = np.abs(np.fft.rfft(sq))
    fa = np.fft.rfftfreq(sq.size, d=1.0 / fs)
    band = fa > fs * 0.005
    if band.any():
        i_pk = int(np.argmax(S[band]))
        peak = float(S[band][i_pk])
        med = float(np.median(S[band]) + 1e-12)
        out["scf_peak"] = round(peak / med, 3)
        out["scf_peak_alpha_norm"] = round(float(fa[band][i_pk]) / fs, 5)
        out["scf_kurtosis"] = round(float(((S[band] - S[band].mean()) ** 4).mean() / (S[band].var() ** 2 + 1e-20)), 3)
    else:
        out["scf_peak"] = out["scf_peak_alpha_norm"] = out["scf_kurtosis"] = 0.0
    return {k: out.get(k, 0.0) for k in FEATURE_NAMES}


def feature_vector(iq: np.ndarray, fs: float) -> np.ndarray:
    d = feature_dict(iq, fs)
    return np.asarray([d[k] for k in FEATURE_NAMES], dtype=np.float32)


# ── model loading + the Classifier wrapper ──────────────────────────────────
def load_model(path: str):
    """Load an ONNX (.onnx) or Torch (.pt/.pth/.ckpt) model — whichever runtime is installed.
    Returns an opaque object the Classifier wraps. Raises if neither runtime is available."""
    p = str(path)
    if p.endswith(".onnx"):
        import onnxruntime as ort  # type: ignore
        return ("onnx", ort.InferenceSession(p, providers=ort.get_available_providers()))
    if p.endswith((".pt", ".pth", ".ckpt")):
        import torch  # type: ignore
        m = torch.jit.load(p) if p.endswith(".pt") else torch.load(p, map_location="cpu")
        if hasattr(m, "eval"):
            m.eval()
        return ("torch", m)
    if p.endswith(".tflite"):
        # Lazy import — TFLite is optional; ARM-friendly path for embedded Ares nodes.
        try:
            import tflite_runtime.interpreter as tflite  # type: ignore
            interp = tflite.Interpreter(model_path=p)
        except Exception:
            import tensorflow as tf  # type: ignore   # full TF as a fallback
            interp = tf.lite.Interpreter(model_path=p)
        interp.allocate_tensors()
        return ("tflite", interp)
    raise ValueError(f"unrecognised model file (want .onnx / .pt / .pth / .ckpt / .tflite): {p}")


def _softmax(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.float64).ravel()
    if v.size == 0:
        return v
    if np.all((v >= 0) & (v <= 1)) and abs(v.sum() - 1.0) < 1e-3:
        return v  # already probabilities
    e = np.exp(v - v.max())
    return e / (e.sum() + 1e-12)


class Classifier:
    """Wraps a loaded ONNX / Torch model (or any ``fn(feature_vector) -> logits`` callable),
    exposing the ``classify`` method ``uas_video.set_ml_classifier`` expects."""

    def __init__(self, model, classes: Optional[list] = None):
        self.model = model
        self.classes = list(classes) if classes else list(DEFAULT_CLASSES)

    def _infer(self, feat: np.ndarray) -> np.ndarray:
        m = self.model
        if isinstance(m, tuple) and m[0] == "onnx":
            sess = m[1]
            inp = sess.get_inputs()[0].name
            out = sess.run(None, {inp: feat[None, :].astype(np.float32)})
            return np.asarray(out[0]).ravel()
        if isinstance(m, tuple) and m[0] == "torch":
            import torch  # type: ignore
            with torch.no_grad():
                y = m[1](torch.from_numpy(feat[None, :]).float())
            return y.detach().cpu().numpy().ravel()
        if isinstance(m, tuple) and m[0] == "tflite":
            interp = m[1]
            in_det = interp.get_input_details()[0]
            out_det = interp.get_output_details()[0]
            # Tflite is picky about dtype + shape — coerce.
            x = feat[None, :].astype(in_det.get("dtype", np.float32))
            # Some models quantise the input; rescale if so.
            scale, zp = in_det.get("quantization", (0.0, 0))
            if scale and scale > 0:
                x = (x / scale + zp).astype(in_det["dtype"])
            interp.set_tensor(in_det["index"], x.reshape(in_det["shape"]))
            interp.invoke()
            out = interp.get_tensor(out_det["index"])
            return np.asarray(out, dtype=np.float32).ravel()
        if callable(m):
            return np.asarray(m(feat)).ravel()
        raise TypeError("unsupported model object")

    def classify(self, iq: np.ndarray, fs: float, band: Optional[dict] = None) -> dict:
        feat = feature_vector(iq, fs)
        logits = self._infer(feat)
        probs = _softmax(logits)
        if probs.size == 0:
            return {"feed_type": "unknown_digital", "confidence": 0.0, "model": "ml-empty"}
        k = int(np.argmax(probs))
        labels = self.classes if len(self.classes) == probs.size else [f"class_{i}" for i in range(probs.size)]
        top = sorted(range(probs.size), key=lambda i: -probs[i])[:3]
        return {
            "feed_type": labels[k] if labels[k] in DEFAULT_CLASSES or labels[k].startswith("class_") else "unknown_digital",
            "confidence": round(float(probs[k]), 3),
            "probs": {labels[i]: round(float(probs[i]), 4) for i in top},
            "model": "ml",
        }


# ── registration into the uas_video hook ────────────────────────────────────
def register(model_path: str, *, classes: Optional[list] = None) -> dict:
    """Load ``model_path`` and register it as ``uas_video.ML_CLASSIFIER`` (so its verdict
    is ensembled with the rule-based classifier). Returns a status dict; never raises."""
    try:
        model = load_model(model_path)
    except ImportError:
        return {"registered": False, "reason": "neither onnxruntime nor torch is installed — `pip install onnxruntime` (or torch)"}
    except Exception as e:
        return {"registered": False, "reason": str(e)}
    clf = Classifier(model, classes)
    uas_video.set_ml_classifier(lambda iq, fs, band=None: clf.classify(iq, fs, band))
    return {"registered": True, "model_path": str(model_path), "classes": len(clf.classes)}


def unregister() -> None:
    uas_video.set_ml_classifier(None)


def status() -> dict:
    def _have(mod: str) -> bool:
        try:
            __import__(mod)
            return True
        except Exception:
            return False
    return {
        "feature_extractor": f"available (numpy) — {len(FEATURE_NAMES)} features",
        "feature_names": list(FEATURE_NAMES),
        "default_classes": list(DEFAULT_CLASSES),
        "onnxruntime": _have("onnxruntime"),
        "torch": _have("torch"),
        "tflite_runtime": _have("tflite_runtime"),
        "tensorflow": _have("tensorflow"),
        "registered": uas_video.ML_CLASSIFIER is not None,
    }
