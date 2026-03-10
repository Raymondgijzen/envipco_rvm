# /config/custom_components/envipco_rvm/const.py

"""Constants for the Envipco RVM integration."""

from __future__ import annotations

DOMAIN = "envipco_rvm"
NAME = "Envipco RVM"

#
# Config keys
#
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_MACHINES = "machines"
CONF_MACHINE_RATES = "machine_rates"
CONF_MACHINE_BIN_LIMITS = "machine_bin_limits"
CONF_MACHINE_META = "machine_meta"
CONF_RVMSTATS_INTERVAL = "rvmstats_interval"
CONF_REJECTS_INTERVAL = "rejects_interval"

#
# Defaults
#
DEFAULT_RVMSTATS_INTERVAL = 300
DEFAULT_REJECTS_INTERVAL = 1800

DEFAULT_RATE_CAN = 0.15
DEFAULT_RATE_PET = 0.15

#
# API field prefixes
#
BIN_MATERIAL_PREFIX = "BinInfoMaterialBin"
BIN_LIMIT_PREFIX = "BinInfoLimitBin"
BIN_FULL_PREFIX = "BinInfoFullBin"
BIN_COUNT_PREFIX = "BinInfoCountBin"
ACCEPT_FIELDS_PREFIX = "accepted"

#
# Main status fields
#
STATUS_STATE_KEY = "StatusInfoState"
STATUS_LAST_REPORT_PRIMARY_KEY = "StatusInfoLastReport"
STATUS_LAST_REPORT_FALLBACK_KEYS = (
    "RVMStatusReportDate",
    "RVMStatusLastTime",
    "RVMStatusStateDateGMT",
)

#
# Accepted counters from rvmStats
#
KEY_ACCEPTED_CANS = "cans_accepted"
KEY_ACCEPTED_PET = "pet_accepted"
KEY_ACCEPTED_GLASS = "glass_accepted"

#
# Reject fields
# Houd deze lijst gelijk aan wat jouw rejects-endpoint echt teruggeeft.
#
REJECT_KEYS = [
    "binFull",
    "noBarcode",
    "notInDb",
    "sortingErr",
    "shape",
    "weight",
    "wrongMaterial",
]

REJECT_LABELS_NL = {
    "binFull": "Bak vol",
    "noBarcode": "Geen barcode",
    "notInDb": "Niet in database",
    "sortingErr": "Sorteerfout",
    "shape": "Vorm",
    "weight": "Gewicht",
    "wrongMaterial": "Verkeerd materiaal",
}

#
# Materiaalmapping
# API gebruikt o.a. ALU / PET / GLASS
#
MATERIAL_MAP = {
    "ALU": "CAN",
    "CAN": "CAN",
    "METAL": "CAN",
    "STEEL": "CAN",
    "PET": "PET",
    "PLASTIC": "PLASTIC",
    "GLASS": "GLASS",
    "GLS": "GLASS",
}

MATERIAL_LABELS_NL = {
    "CAN": "Blik",
    "PET": "PET",
    "PLASTIC": "Plastic",
    "GLASS": "Glas",
}

#
# Fallback capaciteiten per materiaal
# Alleen gebruikt als de API geen limiet geeft en jij niets handmatig hebt ingesteld.
# Pas deze gerust aan naar wat voor jouw machines logisch is.
#
DEFAULT_BIN_CAPACITY_BY_MATERIAL = {
    "CAN": 1000,
    "PET": 1000,
    "PLASTIC": 1000,
    "GLASS": 1000,
}
