"""Per-service deployment settings; environment is read at construction time."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv6Address
from urllib.parse import urlsplit

import idna


def _canonical_host(host: str) -> str:
    if ":" in host:
        address = IPv6Address(host)
        # Python 3.14 renders mapped addresses with dotted IPv4; browsers use hex.
        if address.ipv4_mapped is not None:
            mapped = int(address.ipv4_mapped)
            return f"[::ffff:{mapped >> 16:x}:{mapped & 65535:x}]"
        return f"[{address.compressed}]"
    host = idna.encode(host, uts46=True).decode("ascii").lower()
    # WHATWG treats a domain ending in a number as IPv4, including legacy
    # shortened, hexadecimal and octal forms; never whitelist their raw spelling.
    labels = host.removesuffix(".").split(".")
    if re.fullmatch(r"[0-9]+|0x[0-9a-f]*", labels[-1]):
        if len(labels) > 4:
            raise ValueError("Invalid IPv4 host")
        numbers = []
        for label in labels:
            base = 16 if label.startswith("0x") else 8 if len(label) > 1 and label.startswith("0") else 10
            digits = label[2:] if base == 16 else label[1:] if base == 8 else label
            if not re.fullmatch({16: r"[0-9a-f]*", 8: r"[0-7]*", 10: r"[0-9]+"}[base], digits):
                raise ValueError("Invalid IPv4 host")
            numbers.append(int(digits or "0", base))
        if any(number > 255 for number in numbers[:-1]) or numbers[-1] >= 256 ** (5 - len(numbers)):
            raise ValueError("Invalid IPv4 host")
        address = numbers[-1] + sum(number << (24 - 8 * index) for index, number in enumerate(numbers[:-1]))
        return str(IPv4Address(address))
    return host


def _https_origin(value: str) -> str | None:
    if not value or any(c.isspace() or ord(c) < 33 or ord(c) == 127 for c in value) or "\\" in value:
        return None
    try:
        parts = urlsplit(value)
        if not (parts.scheme == "https" and parts.hostname and parts.username is None
                and parts.password is None and "?" not in value and "#" not in value
                and "%" not in parts.netloc
                and re.fullmatch(r"(?:\[[0-9A-Fa-f:.]+\]|[^:\[\]@]+)(?::[0-9]+)?", parts.netloc)
                and (parts.port is None or 0 < parts.port <= 65535)):
            return None
        host = _canonical_host(parts.hostname)
        port = f":{parts.port}" if parts.port not in (None, 443) else ""
        return f"https://{host}{port}"
    except (ValueError, idna.IDNAError):
        return None


def safe_https_url(value: str) -> bool:
    return _https_origin(value) is not None


@dataclass(frozen=True)
class DeploymentConfig:
    command_guild: str = ""
    membership_guild: str = ""
    membership_role: str = ""
    site_url: str = ""
    workbench_url: str = ""
    redirect_uri: str = ""

    def __post_init__(self):
        site = self.site_url.rstrip("/") + "/" if self.site_url else ""
        workbench = self.workbench_url or (site + "workbench/" if site else "")
        object.__setattr__(self, "site_url", site)
        object.__setattr__(self, "workbench_url", workbench.rstrip("/") + "/" if workbench else "")

    @classmethod
    def from_env(cls):
        return cls(command_guild=os.getenv("REGISTRATION_COMMAND_GUILD", ""),
                   membership_guild=os.getenv("REGISTRATION_MEMBERSHIP_GUILD", ""),
                   membership_role=os.getenv("REGISTRATION_MEMBERSHIP_ROLE", ""),
                   site_url=os.getenv("SITE_URL", ""), workbench_url=os.getenv("WORKBENCH_URL", ""),
                   redirect_uri=os.getenv("DISCORD_REDIRECT_URI", ""))

    @property
    def allowed_origins(self):
        return frozenset(origin for url in (self.site_url, self.workbench_url)
                         if (origin := _https_origin(url)) is not None)

    @property
    def auth_ready(self):
        return all(safe_https_url(url) for url in (self.site_url, self.workbench_url, self.redirect_uri))

    @property
    def registration_ready(self):
        return bool(self.auth_ready and all(re.fullmatch(r"[1-9][0-9]{16,19}", value)
                    for value in (self.command_guild, self.membership_guild, self.membership_role)))
