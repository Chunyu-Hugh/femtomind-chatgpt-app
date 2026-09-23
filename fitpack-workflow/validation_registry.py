"""FemtoMind validation registry: stamps, lineage, and the commit gate.

The trust model: "scientists maintain, scientists validate, validation binds
the commit" (paper-961 analogy).  Nothing that PROMOTES a result - publishing
a posterior as the official next-stage prior, exposing tables to a shared
location, freezing numbers into a paper - may proceed unless a validated
stamp exists for the artifact.

Registry file:  <FEMTOMIND_ENV_HOME>/validation_registry.jsonl  (append-only)

Each line is one stamp:

{
  "stamp_id": "V20260918-0001",
  "artifact": "/w/ceph24/JAM/chunyu/jam24repro/smoke4/001/step2_v7/msr/X.msr",
  "artifact_sha256": "....",
  "claim": "E866 cross-section chi2/N = 5.10 vs paper Table 5 target 1.20",
  "status": "pending | accepted | rejected",
  "validator": "nobuo.sato@jlab.org",       # roster member (human or automated id)
  "method": "compare_with_paper | per_point_diagnostic | human_review",
  "evidence": {"computed": 5.10, "target": 1.20, "paper_table": "Table 5"},
  "parents": ["V20260918-0000"],            # lineage links
  "created_by": "agent:codex-...",
  "created_at": "2026-09-18T12:00:00Z",
  "approved_by": null,
  "approved_at": null
}

The COMMIT GATE is `authorize_promotion(artifact, purpose)`:
  - requires an ACCEPTED stamp for the artifact (or one of its parents)
  - raises GateError otherwise, telling the caller to propose validation first.

The VALIDATOR ROSTER lists who may accept/reject stamps; non-roster
approvals are rejected by approve().
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# Validator roster: human maintainers + automated validators with thresholds.
DEFAULT_ROSTER = {
    "humans": [
        "nobuo.sato@jlab.org",
        "wally.melnitchouk@jlab.org",
        "chunyu.hu@odu.edu",
    ],
    "automated": [
        "compare_with_paper",     # acceptance when within paper tolerance
        "per_point_diagnostic",   # acceptance when no catastrophic pulls remain
    ],
}

STATUS_PENDING = "pending"
STATUS_ACCEPTED = "accepted"
STATUS_REJECTED = "rejected"
_TERMINAL = {STATUS_ACCEPTED, STATUS_REJECTED}


class GateError(RuntimeError):
    """Raised when a promotion is attempted without a validated stamp."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _registry_path() -> Path:
    env_home = os.environ.get("FEMTOMIND_ENV_HOME", "")
    if env_home:
        base = Path(env_home)
    else:
        base = Path(__file__).resolve().parents[2] / ".femtomind-data"
    return base / "validation_registry.jsonl"


def _sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class ValidationRegistry:
    """Append-only JSONL registry with in-process cache and a file lock."""

    def __init__(self, path: Optional[Path] = None, roster: Optional[Dict[str, List[str]]] = None):
        self.path = path or _registry_path()
        self.roster = roster or DEFAULT_ROSTER
        self._lock = threading.Lock()

    # -- storage ----------------------------------------------------------
    def _read_all(self) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        rows = []
        with open(self.path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # tolerate a torn tail line
        return rows

    def _append(self, row: Dict[str, Any]) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a") as f:
                f.write(json.dumps(row, sort_keys=True) + "\n")

    def _latest(self) -> Dict[str, Dict[str, Any]]:
        """stamp_id -> latest row (append-only log: last write wins)."""
        latest: Dict[str, Dict[str, Any]] = {}
        for r in self._read_all():
            sid = r.get("stamp_id")
            if sid:
                latest[sid] = r
        return latest

    def stamps(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        rows = list(self._latest().values())
        if status:
            rows = [r for r in rows if r.get("status") == status]
        return rows

    def get(self, stamp_id: str) -> Optional[Dict[str, Any]]:
        return self._latest().get(stamp_id)

    # -- stamp lifecycle --------------------------------------------------
    def next_id(self) -> str:
        n = len(self._read_all()) + 1
        return f"V{datetime.now(timezone.utc).strftime('%Y%m%d')}-{n:04d}"

    def stamp(
        self,
        artifact: str,
        claim: str,
        method: str,
        evidence: Optional[Dict[str, Any]] = None,
        parents: Optional[List[str]] = None,
        created_by: str = "agent",
        validator: Optional[str] = None,
        hash_artifact: bool = True,
    ) -> Dict[str, Any]:
        """Create a PENDING stamp for an artifact."""
        row = {
            "stamp_id": self.next_id(),
            "artifact": artifact,
            "artifact_sha256": _sha256_of(artifact) if hash_artifact and os.path.isfile(artifact) else None,
            "claim": claim,
            "status": STATUS_PENDING,
            "validator": validator,
            "method": method,
            "evidence": evidence or {},
            "parents": parents or [],
            "created_by": created_by,
            "created_at": _now(),
            "approved_by": None,
            "approved_at": None,
        }
        self._append(row)
        return row

    def approve(self, stamp_id: str, approver: str, note: str = "") -> Dict[str, Any]:
        """Accept a pending stamp; approver must be in the roster."""
        if approver not in self.roster.get("humans", []) and approver not in self.roster.get("automated", []):
            raise GateError(f"approver '{approver}' is not in the validator roster")
        target = self.get(stamp_id)
        if target is None:
            raise KeyError(f"unknown stamp {stamp_id}")
        if target.get("status") in _TERMINAL:
            raise GateError(f"stamp {stamp_id} already {target.get('status')}")
        target["status"] = STATUS_ACCEPTED
        target["approved_by"] = approver
        target["approved_at"] = _now()
        target.setdefault("approval_note", note)
        self._append(target)
        return target

    def reject(self, stamp_id: str, approver: str, note: str) -> Dict[str, Any]:
        if approver not in self.roster.get("humans", []):
            raise GateError(f"only human maintainers may reject stamps; '{approver}' is not one")
        target = self.get(stamp_id)
        if target is None:
            raise KeyError(f"unknown stamp {stamp_id}")
        target["status"] = STATUS_REJECTED
        target["approved_by"] = approver
        target["approved_at"] = _now()
        target["approval_note"] = note
        self._append(target)
        return target

    # -- lineage ----------------------------------------------------------
    def lineage(self, stamp_id: str) -> List[Dict[str, Any]]:
        """Walk parents chain (oldest first, ending with the stamp itself)."""
        out: List[Dict[str, Any]] = []
        seen = set()
        cur = self.get(stamp_id)
        chain: List[Dict[str, Any]] = []
        while cur and cur["stamp_id"] not in seen:
            seen.add(cur["stamp_id"])
            chain.append(cur)
            parent_id = (cur.get("parents") or [None])[0]
            cur = self.get(parent_id) if parent_id else None
        chain.reverse()
        return chain

    def lineage_of_artifact(self, artifact: str) -> List[Dict[str, Any]]:
        for r in self._latest().values():
            if r.get("artifact") == artifact:
                return self.lineage(r["stamp_id"])
        return []

    # -- the commit gate --------------------------------------------------
    def authorize_promotion(self, artifact: str, purpose: str, accepted_by: Optional[str] = None) -> Dict[str, Any]:
        """Gate for promoting an artifact (publish, chain-as-prior, deploy).

        Returns the ACCEPTED stamp that authorizes the promotion, or raises
        GateError when no accepted validation exists for the artifact.
        """
        for r in self._latest().values():
            if r.get("artifact") == artifact and r.get("status") == STATUS_ACCEPTED:
                if accepted_by and accepted_by not in self.roster.get("humans", []) + self.roster.get("automated", []):
                    raise GateError(f"'{accepted_by}' is not in the validator roster")
                return r
        raise GateError(
            f"promotion denied for '{artifact}' ({purpose}): no accepted validation stamp. "
            "Propose validation (stamp) and wait for a roster member to accept it."
        )


_default: Optional[ValidationRegistry] = None


def get_registry() -> ValidationRegistry:
    global _default
    if _default is None:
        _default = ValidationRegistry()
    return _default
