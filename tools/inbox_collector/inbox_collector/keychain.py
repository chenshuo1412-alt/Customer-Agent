"""Secret storage. The Mac path uses the public Security framework, not argv."""

from __future__ import annotations

import sys
from typing import Protocol

SERVICE_NAME = "inbox-collector.grok-routine"
ACCOUNT_NAME = "routine-sender"


class SecretStore(Protocol):
    def set(self, value: str) -> None:
        """Store one sender key."""

    def get(self) -> str | None:
        """Return the sender key, or None when it is absent."""


class MemorySecretStore:
    """Test double. It is not the macOS Keychain and it is not a config file."""

    def __init__(self) -> None:
        self._value: str | None = None
        self.writes = 0

    def set(self, value: str) -> None:
        self._value = value
        self.writes += 1

    def get(self) -> str | None:
        return self._value


class KeychainUnavailable(RuntimeError):
    pass


class MacKeychainStore:
    """macOS Keychain via SecKeychain* . The password is not a process argument."""

    def __init__(self, service: str = SERVICE_NAME, account: str = ACCOUNT_NAME) -> None:
        self.service = service
        self.account = account

    @staticmethod
    def available() -> bool:
        if sys.platform != "darwin":
            return False
        try:
            _framework()
        except OSError:
            return False
        return True

    def set(self, value: str) -> None:
        if not value:
            raise KeychainUnavailable("empty_secret_refused")
        framework = _framework()
        item = _find_item(framework, self.service, self.account)
        if item is not None:
            framework.SecKeychainItemDelete(item)
            framework.CFRelease(item)
        status = framework.SecKeychainAddGenericPassword(
            None,
            len(self.service.encode()),
            self.service.encode(),
            len(self.account.encode()),
            self.account.encode(),
            len(value.encode()),
            value.encode(),
            None,
        )
        if status != 0:
            raise KeychainUnavailable("keychain_write_failed")

    def get(self) -> str | None:
        raw = _copy_password(_framework(), self.service, self.account)
        if not raw:
            return None
        return raw.decode("utf-8")


def _framework():
    import ctypes

    if sys.platform != "darwin":
        raise KeychainUnavailable("keychain_unavailable")
    library = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
    foundation = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    library.CFRelease = foundation.CFRelease
    return library


def _find_item(framework, service: str, account: str):
    import ctypes

    service_bytes = service.encode()
    account_bytes = account.encode()
    length = ctypes.c_uint32()
    data = ctypes.c_void_p()
    item = ctypes.c_void_p()
    status = framework.SecKeychainFindGenericPassword(
        None,
        len(service_bytes),
        service_bytes,
        len(account_bytes),
        account_bytes,
        ctypes.byref(length),
        ctypes.byref(data),
        ctypes.byref(item),
    )
    if data.value:
        framework.SecKeychainItemFreeContent(None, data)
    if status != 0 or not item.value:
        return None
    return item


def _copy_password(framework, service: str, account: str):
    import ctypes

    service_bytes = service.encode()
    account_bytes = account.encode()
    length = ctypes.c_uint32()
    data = ctypes.c_void_p()
    item = ctypes.c_void_p()
    status = framework.SecKeychainFindGenericPassword(
        None,
        len(service_bytes),
        service_bytes,
        len(account_bytes),
        account_bytes,
        ctypes.byref(length),
        ctypes.byref(data),
        ctypes.byref(item),
    )
    if status != 0 or not data.value:
        return None
    raw = ctypes.string_at(data, length.value)
    framework.SecKeychainItemFreeContent(None, data)
    if item.value:
        framework.CFRelease(item)
    return raw
