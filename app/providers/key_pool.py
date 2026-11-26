"""Safe, in-memory API-key rotation primitives for free-tier provider adapters.

Raw credentials never appear in status output or logs. Provider adapters and the
service own persistence and provider-specific quota semantics.
"""
from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone


def parse_api_keys(*values: str | None) -> list[str]:
    """Parse comma/newline-separated keys without changing their case."""
    keys: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not value:
            continue
        for candidate in str(value).replace("\r", "\n").replace(",", "\n").splitlines():
            key = candidate.strip()
            if key and key not in seen:
                seen.add(key)
                keys.append(key)
    return keys


@dataclass(frozen=True)
class KeyLease:
    index: int
    key: str
    fingerprint: str
    account_group: str = "default"


def parse_api_key_entries(*values: str | None) -> list[tuple[str, str]]:
    """Parse optional group|key entries; ungrouped keys share one account quota."""
    entries: list[tuple[str, str]] = []
    seen_keys: set[str] = set()
    for value in values:
        if not value:
            continue
        for candidate in str(value).replace("\r", "\n").replace(",", "\n").splitlines():
            item = candidate.strip()
            if not item:
                continue
            if "|" in item:
                group, key = item.split("|", 1)
                group, key = group.strip().lower(), key.strip()
                if not group or not key:
                    continue
            else:
                group, key = "default", item
            if key not in seen_keys:
                seen_keys.add(key)
                entries.append((group, key))
    return entries


class NoAvailableKeys(RuntimeError):
    def __init__(self, provider: str, next_available_in: float | None = None):
        self.provider = provider
        self.next_available_in = next_available_in
        detail = "all configured keys are unavailable"
        if next_available_in is not None:
            detail += f" (next retry in {max(0, int(next_available_in))} seconds)"
        super().__init__(f"{provider}: {detail}")


class ProviderKeyPool:
    """Thread-safe round-robin key pool with quota cooldown and key disablement.

    A 429 (or explicit quota-exhaustion response) cools down only that key.
    Authentication failures disable only the affected key until the process is
    restarted/configuration is reloaded. Other errors do not poison a key.
    """

    def __init__(self, provider: str, keys: list[str], *, clock=time.monotonic):
        self.provider = str(provider)
        entries = parse_api_key_entries(*keys)
        self._groups = [group for group, _ in entries]
        self._keys = [key for _, key in entries]
        self._clock = clock
        self._lock = threading.Lock()
        self._cursor = 0
        self._blocked_until: dict[int, float] = {}
        self._disabled: dict[int, str] = {}
        self._last_error: dict[int, str] = {}
        self._usage: dict[int, dict] = {}

    @property
    def configured(self) -> bool:
        return bool(self._keys)

    @staticmethod
    def _fingerprint(key: str) -> str:
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]

    def acquire(self, allowed_account_groups: set[str] | None = None) -> KeyLease:
        with self._lock:
            if not self._keys:
                raise NoAvailableKeys(self.provider)
            now = self._clock()
            available = []
            retry_times = []
            for offset in range(len(self._keys)):
                index = (self._cursor + offset) % len(self._keys)
                if index in self._disabled:
                    continue
                if allowed_account_groups is not None and self._groups[index] not in allowed_account_groups:
                    continue
                blocked_until = self._blocked_until.get(index, 0.0)
                if blocked_until > now:
                    retry_times.append(blocked_until - now)
                    continue
                available.append(index)
            if not available:
                raise NoAvailableKeys(
                    self.provider, min(retry_times) if retry_times else None
                )
            index = available[0]
            self._cursor = (index + 1) % len(self._keys)
            key = self._keys[index]
            return KeyLease(index, key, self._fingerprint(key), self._groups[index])

    def record_usage(self, lease: KeyLease, usage: dict | None, minimum_remaining: dict | None = None) -> None:
        """Attach non-secret quota counters to the exact key that made the call."""
        if not isinstance(usage, dict):
            return
        safe = {}
        for name, value in usage.items():
            normalized = str(name).lower()
            if any(secret in normalized for secret in ("key", "token", "secret", "email", "customer", "password", "authorization", "credential", "bearer")):
                continue
            if isinstance(value, (str, int, float, bool)) or value is None:
                safe[str(name)] = value
        safe.setdefault("usage_observed_at", datetime.now(timezone.utc).isoformat())
        exhausted = False
        for name in (
            "requests_remaining", "credits_remaining", "monthly_objects_remaining",
            "datapoints_remaining", "remaining_requests", "remaining_credits",
            "remaining_objects",
        ):
            if name in safe and safe[name] is not None:
                try:
                    exhausted = exhausted or float(safe[name]) <= 0
                except (TypeError, ValueError):
                    pass
        for name, threshold in (minimum_remaining or {}).items():
            if name not in safe or safe[name] is None:
                continue
            try:
                if float(safe[name]) <= float(threshold):
                    exhausted = True
                    safe["quota_floor_triggered"] = name
                    safe["quota_floor_threshold"] = int(threshold)
            except (TypeError, ValueError, OverflowError):
                continue
        if exhausted:
            delay = None
            now_utc = datetime.now(timezone.utc)
            for name in (
                "period_resets_at", "monthly_reset", "daily_reset",
                "datapoints_reset_at", "credits_reset_at", "reset_at",
            ):
                value = safe.get(name)
                if not value:
                    continue
                try:
                    reset = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                    if reset.tzinfo is None:
                        reset = reset.replace(tzinfo=timezone.utc)
                    delay = max(1.0, (reset.astimezone(timezone.utc) - now_utc).total_seconds())
                    break
                except (TypeError, ValueError):
                    continue
            if delay is None:
                elapsed = 0.0
                try:
                    observed_at = datetime.fromisoformat(
                        str(safe.get("usage_observed_at")).replace("Z", "+00:00")
                    )
                    if observed_at.tzinfo is None:
                        observed_at = observed_at.replace(tzinfo=timezone.utc)
                    elapsed = max(0.0, (now_utc - observed_at.astimezone(timezone.utc)).total_seconds())
                except (TypeError, ValueError):
                    pass
                for name in ("resets_in_seconds", "retry_after_seconds"):
                    try:
                        if safe.get(name) is not None:
                            delay = max(1.0, float(safe[name]) - elapsed)
                            break
                    except (TypeError, ValueError):
                        pass
            if delay is None:
                delay = max(1.0, 86400.0 - elapsed)

        with self._lock:
            self._usage[lease.index] = safe
            if exhausted:
                blocked_until = self._clock() + delay
                for index, group in enumerate(self._groups):
                    if group == lease.account_group:
                        self._blocked_until[index] = max(
                            self._blocked_until.get(index, 0.0), blocked_until
                        )
                self._last_error[lease.index] = (
                    "Provider quota exhausted; key group paused until its quota resets."
                )

    def restore_usage(self, key_id: str, usage: dict | None, minimum_remaining: dict | None = None) -> None:
        """Restore last known non-secret quota counters for a configured key."""
        with self._lock:
            for index, key in enumerate(self._keys):
                if self._fingerprint(key) == key_id:
                    lease = KeyLease(index, key, key_id, self._groups[index])
                    break
            else:
                return
        self.record_usage(lease, usage, minimum_remaining=minimum_remaining)

    def mark_success(self, lease: KeyLease) -> None:
        with self._lock:
            usage = self._usage.get(lease.index, {})
            exhausted = bool(usage.get("quota_floor_triggered"))
            for name in (
                "requests_remaining", "credits_remaining", "monthly_objects_remaining",
                "datapoints_remaining", "remaining_requests", "remaining_credits",
                "remaining_objects",
            ):
                try:
                    exhausted = exhausted or (
                        usage.get(name) is not None and float(usage[name]) <= 0
                    )
                except (TypeError, ValueError):
                    continue
            if not exhausted:
                self._blocked_until.pop(lease.index, None)
                self._last_error.pop(lease.index, None)

    def mark_failure(
        self,
        lease: KeyLease,
        *,
        status_code: int | None = None,
        message: str = "",
        retry_after_seconds: float | None = None,
        default_cooldown_seconds: float = 60.0,
    ) -> str:
        """Return 'disabled', 'cooldown', or 'unchanged'."""
        detail = str(message or "provider request failed")[:240]
        lowered = detail.lower()
        quota_error = (
            status_code == 429
            or status_code == 402
            or "out_of_usage_credits" in lowered
            or "quota exceeded" in lowered
            or "insufficient credits" in lowered
            or "usage limit" in lowered
        )
        with self._lock:
            self._last_error[lease.index] = detail
            if quota_error:
                try:
                    delay = max(1.0, float(retry_after_seconds))
                except (TypeError, ValueError):
                    monthly_or_period_quota = any(
                        token in lowered
                        for token in ("out_of_usage_credits", "monthly", "daily allowance")
                    )
                    fallback = max(
                        float(default_cooldown_seconds),
                        86400.0 if monthly_or_period_quota else 1.0,
                    )
                    delay = fallback
                blocked_until = self._clock() + delay
                # All keys assigned to the same account share that account's
                # free-tier allowance; do not hop keys to bypass a quota.
                for index, group in enumerate(self._groups):
                    if group == lease.account_group:
                        self._blocked_until[index] = blocked_until
                return "cooldown"
            if status_code == 401:
                self._disabled[lease.index] = "authentication rejected; check this key"
                return "disabled"
            return "unchanged"

    def status(self) -> dict:
        with self._lock:
            now = self._clock()
            keys = []
            for index, key in enumerate(self._keys):
                blocked = self._blocked_until.get(index, 0.0)
                disabled = index in self._disabled
                keys.append({
                    "key_id": self._fingerprint(key),
                    "account_group": self._groups[index],
                    "status": "disabled" if disabled else (
                        "quota_cooldown" if blocked > now else "available"
                    ),
                    "retry_in_seconds": max(0, int(blocked - now)) if blocked > now else 0,
                    "last_error": self._last_error.get(index),
                    "usage": self._usage.get(index, {}),
                })
            groups = {}
            for item in keys:
                group = item["account_group"]
                summary = groups.setdefault(group, {
                    "account_group": group,
                    "configured_keys": 0,
                    "available_keys": 0,
                    "quota_cooldown_keys": 0,
                    "disabled_keys": 0,
                    "usage": {},
                })
                summary["configured_keys"] += 1
                if item["status"] == "available":
                    summary["available_keys"] += 1
                elif item["status"] == "quota_cooldown":
                    summary["quota_cooldown_keys"] += 1
                else:
                    summary["disabled_keys"] += 1
                observed = str(item.get("usage", {}).get("usage_observed_at") or "")
                previous = str(summary["usage"].get("usage_observed_at") or "")
                if item.get("usage") and observed >= previous:
                    summary["usage"] = dict(item["usage"])
            return {
                "provider": self.provider,
                "configured_keys": len(self._keys),
                "available_keys": sum(item["status"] == "available" for item in keys),
                "account_groups": [groups[name] for name in sorted(groups)],
                "keys": keys,
            }
