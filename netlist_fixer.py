"""One-click SPICE netlist patcher. Pure Python, no Qt imports."""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from typing import List, Optional


# Deck terminator only — not .ends (subckt) or .endc (control).
_END_CARD_RE = re.compile(r"^\s*\.end(\s|$|;|\*|\$)", re.IGNORECASE)


class NetlistFixer:
    """Insert a SPICE directive immediately before the deck ``.end`` card."""

    def apply_fix(self, netlist_path: str, fix_directive: str) -> dict:
        if not netlist_path:
            raise ValueError("netlist_path is empty")
        if not os.path.isfile(netlist_path):
            raise FileNotFoundError(f"netlist not found: {netlist_path}")

        directive = (fix_directive or "").strip()
        if not directive:
            raise ValueError("fix_directive is empty")
        # FIX(3D): reject multi-line directives to prevent card injection
        if "\n" in directive or "\r" in directive:
            raise ValueError("fix_directive must be a single line")

        abs_path = os.path.abspath(netlist_path)
        backup_path = abs_path + ".bak"

        # FIX(N8): read the file FIRST (before copying), then copy the backup, then verify
        # that the backup byte-for-byte matches what we read. This eliminates the TOCTOU
        # window where the file could change between shutil.copy2 and our read.
        with open(abs_path, "r", encoding="utf-8", errors="replace") as handle:
            original = handle.read()

        shutil.copy2(abs_path, backup_path)

        with open(backup_path, "r", encoding="utf-8", errors="replace") as handle:
            backup_contents = handle.read()
        if backup_contents != original:
            raise RuntimeError(
                f"Backup verification failed: {abs_path} changed between read and backup. "
                "No changes were written; please retry the fix."
            )

        newline = "\r\n" if "\r\n" in original else "\n"
        lines = original.splitlines()
        insert_at = self._find_end_index(lines)
        # FIX(N7): both branches of the old if/else did exactly the same thing (append);
        # collapse to a single append and a clear placement label.
        if insert_at is None:
            lines.append(directive)
            placement = "appended (no .end card found)"
        else:
            lines.insert(insert_at, directive)
            placement = "inserted immediately before .end"

        body = newline.join(lines)
        if original.endswith(("\n", "\r\n")):
            body += newline

        # FIX(3A): atomic write — write to a temp file in the same directory, then
        # os.replace() atomically swaps it in. If the process dies mid-write the
        # original file is untouched (the .bak copy is the safety net).
        dest_dir = os.path.dirname(abs_path)
        fd, tmp_path = tempfile.mkstemp(dir=dest_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(body)
            os.replace(tmp_path, abs_path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        return {
            "status": "success",
            "message": (
                f"Applied '{directive}' to {abs_path} ({placement}). "
                f"Backup saved at {backup_path}."
            ),
            "backup_path": backup_path,
        }

    @staticmethod
    def _find_end_index(lines: List[str]) -> Optional[int]:
        """Return the index of the last deck ``.end`` card, or None."""
        last = None
        for index, line in enumerate(lines):
            if _END_CARD_RE.match(line):
                last = index
        return last
