"""The headphones this tool knows, and what differs between them (PROTOCOL.md §11).

Headphones are recognised by their Bluetooth name. The handshake has the last
word on the protocol: if it disagrees with the table, the generic settings for
the protocol it reports are used instead.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from .protocol import (
    BATTERY_KINDS,
    BATTERY_SINGLE,
    BATTERY_WITH_THRESHOLD,
    NCASM_SUBTYPE_AUTO,
    NCASM_SUBTYPE_V1,
    NCASM_SUBTYPES,
    V1,
    V2,
    EqPreset,
)

# Sony's control services (PROTOCOL.md §1). Windows lists the XM5's V2 service
# as "Serial HPC"; the XM4 offers V1.
SERVICE_V1 = uuid.UUID("96CC203E-5068-46AD-B32D-E316F5E069BA")
SERVICE_V2 = uuid.UUID("956C7B26-D49A-4BA8-B03F-B17D393CB6E2")

_PRESETS_XM5 = (
    EqPreset.OFF, EqPreset.BRIGHT, EqPreset.EXCITED, EqPreset.MELLOW, EqPreset.RELAXED, EqPreset.VOCAL,
    EqPreset.TREBLE_BOOST, EqPreset.BASS_BOOST, EqPreset.SPEECH,
    EqPreset.MANUAL, EqPreset.CUSTOM_1, EqPreset.CUSTOM_2,
)
# mos9527: the XM6 only accepts Heavy/Clear/Hard/Soft, Custom and the user settings
_PRESETS_XM6 = (
    EqPreset.OFF, EqPreset.HEAVY, EqPreset.CLEAR, EqPreset.HARD, EqPreset.SOFT,
    EqPreset.MANUAL, EqPreset.CUSTOM_1, EqPreset.CUSTOM_2,
)


@dataclass(frozen=True)
class Model:
    name: str
    protocol: str  # what the handshake should report
    ncasm_subtypes: tuple[int, ...]  # NC/ambient sub-types to try, in order
    battery_kinds: tuple[int, ...]  # battery queries to try, in order
    eq_presets: tuple[EqPreset, ...]  # what the app offers
    head_tracking: bool
    tested: bool  # checked on real headphones (by this project or a user's report)

    @property
    def service(self) -> uuid.UUID:
        return SERVICE_V1 if self.protocol == V1 else SERVICE_V2

    @property
    def wind(self) -> bool:
        """Wind noise reduction is a fourth noise control mode on protocol V1."""
        return self.protocol == V1


MODELS = (
    Model("WH-1000XM4", V1, (NCASM_SUBTYPE_V1,), (BATTERY_SINGLE,), _PRESETS_XM5,
          head_tracking=False, tested=False),
    Model("WH-1000XM5", V2, (0x17, 0x15), (BATTERY_SINGLE,), _PRESETS_XM5,
          head_tracking=True, tested=True),
    # A user reported the XM6 working with this app (2026-10-24)
    Model("WH-1000XM6", V2, (NCASM_SUBTYPE_AUTO, 0x17), (BATTERY_SINGLE, BATTERY_WITH_THRESHOLD), _PRESETS_XM6,
          head_tracking=True, tested=True),
)


def find_model(device_name: str) -> Model | None:
    for model in MODELS:
        if model.name.lower() in device_name.lower():
            return model
    return None


def generic_model(protocol: str, name: str = "Sony headphones") -> Model:
    """Best guesses for headphones we have no entry for: try everything the protocol knows."""
    if protocol == V1:
        return Model(name, V1, (NCASM_SUBTYPE_V1,), (BATTERY_SINGLE,), _PRESETS_XM5,
                     head_tracking=False, tested=False)
    return Model(name, V2, NCASM_SUBTYPES, BATTERY_KINDS, _PRESETS_XM5 + _PRESETS_XM6[1:5],
                 head_tracking=True, tested=False)


def resolve(expected: Model | None, protocol: str, device_name: str = "") -> Model:
    """The model to use once the handshake has told us the protocol."""
    if expected is not None and expected.protocol == protocol:
        return expected
    return generic_model(protocol, expected.name if expected else device_name or "Sony headphones")
