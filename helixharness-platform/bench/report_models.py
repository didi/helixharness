"""Typed current execution records."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DeterministicCheckOutcome:
    """One hidden deterministic check retained independently from Judge output."""

    check_id: str
    status: str
    score: float
    evidence_id: str

@dataclass(frozen=True)
class ClaimedJob:
    """One atomically claimed side Job with its frozen Run configuration."""

    job_id: str
    run_id: str
    case_id: str
    side: str
    sequence_number: int
    run_config: dict[str, Any]
    episode_id: str
    attempt_id: str
    attempt_number: int
    lease_token: str
