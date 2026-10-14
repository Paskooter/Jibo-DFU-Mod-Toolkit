"""Shared flash policy identifiers and owner-facing copy (no device access)."""
KEEP = 'keep-calibration'
WITHOUT = 'without-calibration'
POLICIES = (KEEP, WITHOUT)
KEEP_LABEL = "Keep this Jibo's calibration (recommended)"
KEEP_DETAIL = (
    "Puts back his camera calibration and his name, plus supported motion and microphone "
    "calibration when present and his touchscreen calibration when a matching partition "
    "backup exists. Wi-Fi, pairing and settings start fresh, so he comes up at the setup screens."
)
WITHOUT_LABEL = 'Flash without calibration (unsafe)'
WITHOUT_DETAIL = (
    "He gets the image's generic settings instead of his own calibration and identity, "
    "so his camera, motion and hearing may not work properly. A backup of his current data is saved first. "
    "If the image has no valid identity, setup may not work until his identity is restored from a backup."
)
UNSAFE_TITLE = 'Confirm flash without calibration (unsafe)'
UNSAFE_DETAIL = WITHOUT_DETAIL + ' Only continue if you understand the risk and know how to restore his data.'
PHOENIX_LINE = (
    'This image connects Jibo to Phoenix (jibo.io). After flashing he starts at the setup '
    'screens; finish setup at jibo.io.'
)
