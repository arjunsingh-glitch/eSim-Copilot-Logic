import logging
import os
from typing import Optional

from netlist_fixer import NetlistFixer
from netlist_sanitizer import NetlistSanitizer
from prompt_builder import PromptBuilder
from simulation_auditor import SimulationAuditor
from subckt_resolver import SubcktHierarchyResolver


class HardwareContextEngine:
    def __init__(self):
        self.logger = logging.getLogger(__name__)

    # FIX(BUG-1.1): always return resolver_data/auditor_data so Qt callers never KeyError
    def run(self, netlist_path, raw_path: Optional[str] = None, error_data: Optional[dict] = None) -> dict:
        try:
            return self._run_inner(netlist_path, raw_path=raw_path, error_data=error_data)
        except Exception as exc:
            self.logger.exception("HardwareContextEngine.run failed")
            degraded_sanitized = {
                "title": "Error Processing",
                "components": [], "directives": [],
                "stats": {"component_count": 0, "directive_count": 0},
            }
            degraded_error = {
                "category": "Engine Failure",
                "severity": "Critical",
                "esim_fix_command": f"{type(exc).__name__}: {exc}",
            }
            fallback_prompt = PromptBuilder(
                degraded_error, degraded_sanitized, {}, {}
            ).build_system_prompt()
            return {
                "sanitized_netlist": degraded_sanitized,
                "resolver_data": {},
                "auditor_data": {},
                "prompt": fallback_prompt,
                "error": {"type": type(exc).__name__, "message": str(exc)},
            }

    # FIX(BUG-1.2): accept a filesystem path or a raw multi-line netlist string
    def _run_inner(self, netlist_path, raw_path=None, error_data=None) -> dict:
        if isinstance(netlist_path, os.PathLike):
            netlist_path = os.fspath(netlist_path)
        if not netlist_path:
            raise ValueError("netlist_path is empty")

        if isinstance(netlist_path, str) and os.path.isfile(netlist_path):
            # FIX(4B): guard against oversized netlists blocking the GUI thread on a sync read
            _NETLIST_SIZE_LIMIT = 5 * 1024 * 1024  # 5 MB
            netlist_size = os.path.getsize(netlist_path)
            if netlist_size > _NETLIST_SIZE_LIMIT:
                raise ValueError(
                    f"netlist too large ({netlist_size:,} bytes > {_NETLIST_SIZE_LIMIT:,}); "
                    "open in a worker thread"
                )
            with open(netlist_path, "r", encoding="utf-8", errors="replace") as f:
                netlist_text = f.read()
            netlist_dir = os.path.dirname(os.path.abspath(netlist_path))
        elif isinstance(netlist_path, str) and "\n" in netlist_path:
            netlist_text = netlist_path
            netlist_dir = os.getcwd()
        else:
            raise FileNotFoundError(f"netlist not found: {netlist_path}")

        sanitizer = NetlistSanitizer(netlist_text).sanitize()

        resolver = SubcktHierarchyResolver(netlist_dir).resolve(
            sanitizer["components"], sanitizer["directives"]
        )

        auditor_data = {"stats": [], "errors": [], "notes": []}
        if raw_path and os.path.isfile(raw_path):
            # FIX(4B): guard against oversized raw files; SimulationAuditor has its own
            # per-text cap (20 MB) but we avoid even reading the file into memory here.
            _RAW_SIZE_LIMIT = 20 * 1024 * 1024  # 20 MB
            raw_size = os.path.getsize(raw_path)
            if raw_size > _RAW_SIZE_LIMIT:
                auditor_data = {
                    "stats": [], "errors": [],
                    "notes": [
                        f"raw file too large ({raw_size:,} bytes > {_RAW_SIZE_LIMIT:,}); "
                        "run auditor in a worker thread"
                    ],
                }
            else:
                with open(raw_path, "r", encoding="utf-8", errors="replace") as f:
                    auditor_data = SimulationAuditor().audit(f.read())

        prompt = PromptBuilder(error_data, sanitizer, resolver, auditor_data).build_system_prompt()
        return {
            "sanitized_netlist": sanitizer,
            "resolver_data": resolver,
            "auditor_data": auditor_data,
            "prompt": prompt,
        }

    def patch_netlist(self, netlist_path, fix_directive):
        """Backup the netlist and insert ``fix_directive`` before ``.end``."""
        return NetlistFixer().apply_fix(netlist_path, fix_directive)


# FIX(BUG-1.3): keep raw_path instead of swallowing it as a positional error_data
def get_llm_context(netlist_path, raw_path=None, error_data=None):
    return HardwareContextEngine().run(
        netlist_path, raw_path=raw_path, error_data=error_data
    )


def _demo() -> None:
    dummy_netlist = """\
RC Amplifier Demo
V1 in 0 DC 5
R1 in n1 10k
C1 n1 0 100n
Xop n2 out 0 lm741
.tran 1u 2m
.end
"""
    error_data = {
        "category": "Connectivity Fault",
        "severity": "Critical",
        "esim_fix_command": "Wire LM741 to in+, in-, vcc, vee, out.",
    }
    result = HardwareContextEngine().run(dummy_netlist, error_data=error_data)
    print(result["prompt"])
    print("\n--- pipeline stats ---")
    print("components:", result["sanitized_netlist"].get("stats"))
    print("resolver:", result.get("resolver_data"))
    print("auditor:", result.get("auditor_data"))


if __name__ == "__main__":
    _demo()
