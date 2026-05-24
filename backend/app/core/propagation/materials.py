# SPDX-License-Identifier: MIT OR Apache-2.0
# Copyright (c) 2026 Ares

"""
RF Material Properties for Ray Tracing
Permittivity and conductivity values from ITU-R P.2040-2 and literature.
"""
import cmath
import math
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class Material(str, Enum):
    CONCRETE = "concrete"
    BRICK = "brick"
    GLASS = "glass"
    WOOD = "wood"
    VEGETATION = "vegetation"
    DRY_EARTH = "dry_earth"
    WET_EARTH = "wet_earth"
    SEA_WATER = "sea_water"
    ASPHALT = "asphalt"
    METAL = "metal"
    AVERAGE_GROUND = "average_ground"


@dataclass
class MaterialProps:
    name: str
    epsilon_r: float       # Relative permittivity (real part)
    sigma: float           # Conductivity (S/m)
    thickness_m: float     # Default thickness for penetration (m)
    roughness_m: float     # Surface roughness for scattering (m)
    color: str             # UI color for visualization


MATERIALS: dict[Material, MaterialProps] = {
    Material.CONCRETE:       MaterialProps("Concrete",       5.31,  0.326,    0.2,    0.005,  "#94a3b8"),
    Material.BRICK:          MaterialProps("Brick",          3.75,  0.038,    0.1,    0.01,   "#b45309"),
    Material.GLASS:          MaterialProps("Glass",          6.27,  0.0,      0.003,  0.0001, "#7dd3fc"),
    Material.WOOD:           MaterialProps("Wood",           1.99,  0.0012,   0.02,   0.002,  "#92400e"),
    Material.VEGETATION:     MaterialProps("Vegetation",    12.0,   0.5,      1.0,    0.1,    "#16a34a"),
    Material.DRY_EARTH:      MaterialProps("Dry Earth",      3.0,   0.001,    float('inf'), 0.01, "#d97706"),
    Material.WET_EARTH:      MaterialProps("Wet Earth",     25.0,   0.02,     float('inf'), 0.01, "#065f46"),
    Material.SEA_WATER:      MaterialProps("Sea Water",     80.0,   4.0,      float('inf'), 0.1,  "#1d4ed8"),
    Material.ASPHALT:        MaterialProps("Asphalt",        3.1,   0.0001,   float('inf'), 0.003, "#374151"),
    Material.METAL:          MaterialProps("Metal",          1.0,   1e6,      0.001,  0.0,    "#6b7280"),
    Material.AVERAGE_GROUND: MaterialProps("Average Ground",15.0,   0.005,    float('inf'), 0.05, "#78716c"),
}


def reflection_coefficient_db(
    material: Material,
    freq_hz: float,
    incidence_angle_deg: float,
    polarization: str = "vertical",
) -> float:
    """
    Compute reflection coefficient (dB) using Fresnel equations.

    incidence_angle_deg: angle from surface normal (0=normal incidence, 90=grazing)
    polarization: 'vertical' (TM) or 'horizontal' (TE)

    Returns reflection loss in dB (0 = perfect mirror, large negative = absorbed).
    Returned value is <= 0 dB.
    """
    props = MATERIALS.get(material, MATERIALS[Material.AVERAGE_GROUND])
    omega = 2.0 * math.pi * freq_hz
    eps0 = 8.854187817e-12

    # Complex permittivity: eps_c = eps_r - j*sigma/(omega*eps0)
    eps_c: complex = props.epsilon_r - 1j * props.sigma / (omega * eps0)

    theta_i = math.radians(max(0.0, min(89.9, incidence_angle_deg)))
    cos_i = math.cos(theta_i)
    sin_i = math.sin(theta_i)

    # Snell's law in complex form: sin(theta_t)^2 = sin(theta_i)^2 / eps_c
    sin_t_sq: complex = (sin_i ** 2) / eps_c
    cos_t: complex = cmath.sqrt(1.0 - sin_t_sq)

    sqrt_eps = cmath.sqrt(eps_c)

    if polarization == "horizontal":
        # TE polarization: r = (cos_i - sqrt(eps_c)*cos_t) / (cos_i + sqrt(eps_c)*cos_t)
        numerator   = cos_i - sqrt_eps * cos_t
        denominator = cos_i + sqrt_eps * cos_t
    else:
        # TM polarization: r = (sqrt(eps_c)*cos_i - cos_t) / (sqrt(eps_c)*cos_i + cos_t)
        numerator   = sqrt_eps * cos_i - cos_t
        denominator = sqrt_eps * cos_i + cos_t

    if abs(denominator) < 1e-20:
        return 0.0

    r: complex = numerator / denominator
    R = abs(r) ** 2

    # Ament roughness reduction factor
    k = 2.0 * math.pi * freq_hz / 3e8
    roughness_factor = math.exp(-2.0 * (k * props.roughness_m * cos_i) ** 2)
    R *= roughness_factor
    R = max(0.0, min(1.0, R))

    if R < 1e-10:
        return -100.0
    return min(0.0, 10.0 * math.log10(R))


def penetration_loss_db(
    material: Material,
    freq_hz: float,
    thickness_m: Optional[float] = None,
) -> float:
    """
    Compute transmission loss through a material layer (dB).
    Uses ITU-R P.2040 plane-wave attenuation.

    Returns loss in dB (positive = signal reduction).
    """
    props = MATERIALS.get(material, MATERIALS[Material.AVERAGE_GROUND])
    t = thickness_m if thickness_m is not None else props.thickness_m

    if t == float('inf') or t > 100.0:
        return 100.0   # effectively opaque

    omega = 2.0 * math.pi * freq_hz
    eps0  = 8.854187817e-12
    mu0   = 4.0 * math.pi * 1e-7

    eps_r  = props.epsilon_r
    sigma  = props.sigma

    # Attenuation constant alpha from plane-wave in lossy medium
    # gamma = j*omega*sqrt(mu0*(eps_r*eps0 - j*sigma/omega))
    # alpha = Re(gamma)
    x = sigma / (omega * eps0 * eps_r) if eps_r > 0 else 0.0

    alpha = (omega * math.sqrt(mu0 * eps0 * eps_r / 2.0)
             * math.sqrt(math.sqrt(1.0 + x * x) - 1.0))

    loss_db = 20.0 * math.log10(math.e) * 2.0 * alpha * t
    return max(0.0, loss_db)


def material_info() -> list[dict]:
    """Return material properties as list of dicts for API responses."""
    result = []
    for mat, props in MATERIALS.items():
        result.append({
            "id":          mat.value,
            "name":        props.name,
            "epsilon_r":   props.epsilon_r,
            "sigma_s_m":   props.sigma,
            "thickness_m": props.thickness_m if props.thickness_m != float('inf') else None,
            "roughness_m": props.roughness_m,
            "color":       props.color,
        })
    return result
