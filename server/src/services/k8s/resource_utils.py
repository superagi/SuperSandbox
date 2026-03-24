"""Utilities for parsing Kubernetes resource quantities."""

import re

_QUANTITY_RE = re.compile(
    r"^(\d+(?:\.\d+)?)\s*(E|P|T|G|M|K|Ei|Pi|Ti|Gi|Mi|Ki)?$"
)

_SUFFIXES = {
    "": 1,
    "K": 10**3,
    "M": 10**6,
    "G": 10**9,
    "T": 10**12,
    "P": 10**15,
    "E": 10**18,
    "Ki": 2**10,
    "Mi": 2**20,
    "Gi": 2**30,
    "Ti": 2**40,
    "Pi": 2**50,
    "Ei": 2**60,
}


def parse_k8s_quantity(value: str) -> int:
    """Parse a Kubernetes resource quantity string to bytes (or base units).

    Examples:
        parse_k8s_quantity("5Gi") -> 5368709120
        parse_k8s_quantity("512Mi") -> 536870912
        parse_k8s_quantity("100") -> 100
    """
    m = _QUANTITY_RE.match(value.strip())
    if not m:
        raise ValueError(f"Invalid Kubernetes resource quantity: {value}")
    number = float(m.group(1))
    suffix = m.group(2) or ""
    multiplier = _SUFFIXES.get(suffix)
    if multiplier is None:
        raise ValueError(f"Unknown suffix '{suffix}' in quantity: {value}")
    return int(number * multiplier)
