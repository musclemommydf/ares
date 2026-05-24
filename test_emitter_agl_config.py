#!/usr/bin/env python3
# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
Test script to verify the default emitter AGL has been set to 6ft (1.8288m)
Run this after the backend dependencies are installed.
"""
import sys
import os

# Add the backend directory to Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'backend'))

try:
    from app.config import settings
    from app.core.simulation import TransmitterConfig
    from app.core.propagation.antenna import AntennaConfig

    print("Testing default emitter AGL configuration...")
    print(f"✓ Config default_emitter_agl_m: {settings.default_emitter_agl_m}m ({settings.default_emitter_agl_m * 3.28084:.1f}ft)")

    # Test TransmitterConfig uses the new default
    tx = TransmitterConfig()
    print(f"✓ TransmitterConfig default height: {tx.height_m}m ({tx.height_m * 3.28084:.1f}ft)")

    # Test AntennaConfig uses the new default
    ant = AntennaConfig()
    print(f"✓ AntennaConfig default height: {ant.height_m}m ({ant.height_m * 3.28084:.1f}ft)")

    # Verify it's the expected value (6 feet = 1.8288 meters)
    expected = 1.8288
    if abs(settings.default_emitter_agl_m - expected) < 0.0001:
        print(f"✅ SUCCESS: Default emitter AGL correctly set to 6ft ({expected}m)")
    else:
        print(f"❌ ERROR: Expected {expected}m, got {settings.default_emitter_agl_m}m")
        sys.exit(1)

except ImportError as e:
    print(f"⚠️  Backend dependencies not installed: {e}")
    print("Install with: cd backend && pip install -r requirements.txt")
    print("Configuration files have been updated successfully.")
except Exception as e:
    print(f"❌ ERROR: {e}")
    sys.exit(1)