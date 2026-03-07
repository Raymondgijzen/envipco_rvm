from __future__ import annotations

DOMAIN = "envipco_rvm"
NAME = "Envipco RVM"
VERSION = "1.0.9"

CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_MACHINES = "machines"
CONF_MACHINE_RATES = "machine_rates"
CONF_MACHINE_BIN_LIMITS = "machine_bin_limits"
CONF_MACHINE_META = "machine_meta"

DEFAULT_SCAN_INTERVAL = 300
DEFAULT_RATE_CAN = 0.0107
DEFAULT_RATE_PET = 0.0331

EP_BASE = "https://ePortal.envipco.com/api"
PLATFORMS = ["sensor", "number"]

STATUS_STATE_KEY = "StatusInfoState"
STATUS_LAST_REPORT_PRIMARY_KEY = "RVMStatusLastTime"
STATUS_LAST_REPORT_FALLBACK_KEYS: list[str] = ["StatusInfoLastReport"]

BIN_MATERIAL_PREFIX = "BinInfoMaterialBin"
BIN_FULL_PREFIX = "BinInfoFullBin"
BIN_COUNT_PREFIX = "BinInfoCountBin"
BIN_LIMIT_PREFIX = "BinInfoLimitBin"

KEY_ACCEPTED_CANS = "cans_accepted"
KEY_ACCEPTED_PET = "pet_accepted"
KEY_ACCEPTED_GLASS = "glass_accepted"

REJECT_KEYS = [
    "noBarcode",
    "notInDb",
    "bcMove",
    "sortingErr",
    "notAccepted",
    "shape",
    "weight",
    "collision",
    "binFull",
    "notPermitted",
    "wrongMaterial",
    "mode",
]

REJECT_LABELS_NL: dict[str, str] = {
    "noBarcode": "Afkeur geen barcode",
    "notInDb": "Afkeur niet in database",
    "bcMove": "Afkeur barcode verplaatst",
    "sortingErr": "Afkeur sorteerfout",
    "notAccepted": "Afkeur niet geaccepteerd",
    "shape": "Afkeur vorm",
    "weight": "Afkeur gewicht",
    "collision": "Afkeur botsing",
    "binFull": "Afkeur bak vol",
    "notPermitted": "Afkeur niet toegestaan",
    "wrongMaterial": "Afkeur fout materiaal",
    "mode": "Afkeur modus",
}

ACCEPT_FIELDS_PREFIX = "Accept"

MATERIAL_MAP: dict[str, str] = {
    "ALU": "CAN",
    "ALU STEEL": "CAN",
    "ALUSTEEL": "CAN",
    "STEEL": "CAN",
    "CAN": "CAN",
    "CANS": "CAN",
    "PET": "PET",
    "GLASS": "GLASS",
    "GLS": "GLASS",
}

MATERIAL_LABELS_NL: dict[str, str] = {
    "CAN": "Blik",
    "PET": "PET",
    "GLASS": "Glas",
}

DEFAULT_BIN_CAPACITY_BY_MATERIAL: dict[str, int] = {
    "CAN": 1200,
    "PET": 600,
    "GLASS": 400,
}
