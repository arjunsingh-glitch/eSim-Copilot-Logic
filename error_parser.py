"""
Offline eSim/Ngspice Copilot helpers.

This module provides two local, rule-based stages:

1. Netlist Input Structuring  — ``NetlistSanitizer``
2. Rule-Based Fault Identification — ``FaultIdentifier``

No network or cloud API calls are used.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple


# SPICE element-letter → human-readable kind.
# First character of a device name selects the element type.
_COMPONENT_KINDS: Dict[str, str] = {
    "A": "special",
    "B": "behavioral_source",
    "C": "capacitor",
    "D": "diode",
    "E": "vcvs",
    "F": "cccs",
    "G": "vccs",
    "H": "ccvs",
    "I": "current_source",
    "J": "jfet",
    "K": "mutual_inductor",
    "L": "inductor",
    "M": "mosfet",
    "N": "numerical_device",
    "O": "lossy_line",
    "P": "port",
    "Q": "bjt",
    "R": "resistor",
    "S": "voltage_switch",
    "T": "transmission_line",
    "U": "uniform_rc",
    "V": "voltage_source",
    "W": "current_switch",
    "X": "subcircuit",
    "Y": "admittance",
    "Z": "mesfet",
}

_DIRECTIVE_RE = re.compile(r"^\s*\.", re.IGNORECASE)
_COMMENT_RE = re.compile(r"^\s*(\*|;)")
_CONTINUATION_RE = re.compile(r"^\s*\+")

# Typical pin counts used to separate nodes from values / models.
_NODE_ARITY: Dict[str, int] = {
    "B": 2,
    "C": 2,
    "D": 2,
    "E": 4,
    "F": 2,
    "G": 4,
    "H": 2,
    "I": 2,
    "J": 3,
    "L": 2,
    "N": 2,
    "O": 4,
    "R": 2,
    "S": 4,
    "T": 4,
    "U": 2,
    "V": 2,
    "W": 2,
    "Y": 2,
    "Z": 3,
}

# Independent-source keywords that start the parameter list after two nodes.
_SOURCE_PARAM_START = {
    "DC",
    "AC",
    "TRAN",
    "PULSE",
    "SIN",
    "SINE",
    "EXP",
    "PWL",
    "SFFM",
    "AM",
}


@dataclass(frozen=True)
class Fault:
    """Structured diagnosis produced from an Ngspice log snippet."""

    category: str
    severity: str
    suggested_action: str
    matched_pattern: str
    evidence: str = ""
    line_numbers: Tuple[int, ...] = field(default_factory=tuple)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize the fault for JSON-friendly downstream use."""
        payload = asdict(self)
        payload["line_numbers"] = list(self.line_numbers)
        return payload


class NetlistSanitizer:
    """Segment a raw SPICE netlist into a structured dictionary.

    Distinguishes device declarations (``R1``, ``C1``, ``M1``, …) from
    simulator directives (``.tran``, ``.control``, ``.model``, …), while
    preserving title, comments, and ``+`` continuation lines.
    """

    def __init__(self, raw_netlist: str) -> None:
        if raw_netlist is None:
            raise TypeError("raw_netlist must be a string, not None")
        self.raw_netlist = raw_netlist

    def sanitize(self) -> Dict[str, Any]:
        """Parse the netlist and return a clean, structured mapping.

        Returned keys
        -------------
        title : str
            First non-empty, non-comment line (Ngspice title card).
        comments : list[str]
            Full-line comments (``*`` / ``;``).
        components : list[dict]
            Device cards with name, kind, nodes, parameters, and raw text.
        directives : list[dict]
            Dot-commands (``.tran``, ``.control``, ``.end``, …).
        control_commands : list[dict]
            Statements inside ``.control`` … ``.endc`` blocks.
        unknown : list[dict]
            Lines that could not be classified.
        stats : dict
            Simple counts for copilot telemetry.
        """
        logical_lines = self._join_continuations(self._split_physical_lines())

        title = ""
        comments: List[str] = []
        components: List[Dict[str, Any]] = []
        directives: List[Dict[str, Any]] = []
        control_commands: List[Dict[str, Any]] = []
        unknown: List[Dict[str, Any]] = []
        title_consumed = False
        in_control = False

        for line_no, text in logical_lines:
            stripped = text.strip()
            if not stripped:
                continue

            if _COMMENT_RE.match(stripped):
                comments.append(stripped)
                continue

            # Ngspice: the first non-comment card is the circuit title.
            if not title_consumed:
                title = stripped
                title_consumed = True
                continue

            statement = self._strip_inline_comment(stripped)

            if _DIRECTIVE_RE.match(statement):
                directive = self._parse_directive(statement, line_no)
                directives.append(directive)
                if directive["name"] == "control":
                    in_control = True
                elif directive["name"] in {"endc", "end"}:
                    in_control = False
                continue

            if in_control:
                parts = statement.split(None, 1)
                control_commands.append(
                    {
                        "line": line_no,
                        "name": parts[0].lower() if parts else "",
                        "arguments": parts[1] if len(parts) > 1 else "",
                        "raw": statement,
                    }
                )
                continue

            if self._looks_like_component(statement):
                components.append(self._parse_component(statement, line_no))
                continue

            unknown.append({"line": line_no, "raw": statement})

        return {
            "title": title,
            "comments": comments,
            "components": components,
            "directives": directives,
            "control_commands": control_commands,
            "unknown": unknown,
            "stats": {
                "component_count": len(components),
                "directive_count": len(directives),
                "control_command_count": len(control_commands),
                "comment_count": len(comments),
                "unknown_count": len(unknown),
            },
        }

    def _split_physical_lines(self) -> List[Tuple[int, str]]:
        """Yield 1-based (line_number, text) pairs, dropping trailing CR."""
        lines: List[Tuple[int, str]] = []
        for idx, raw in enumerate(self.raw_netlist.splitlines(), start=1):
            lines.append((idx, raw.rstrip("\r")))
        return lines

    def _join_continuations(
        self, physical: Sequence[Tuple[int, str]]
    ) -> List[Tuple[int, str]]:
        """Fold ``+`` continuation cards into the preceding logical line."""
        folded: List[Tuple[int, str]] = []
        for line_no, text in physical:
            if _CONTINUATION_RE.match(text) and folded:
                start_no, existing = folded[-1]
                continuation = _CONTINUATION_RE.sub(" ", text, count=1).strip()
                folded[-1] = (start_no, f"{existing.rstrip()} {continuation}")
            else:
                folded.append((line_no, text))
        return folded

    @staticmethod
    def _strip_inline_comment(text: str) -> str:
        """Drop trailing ``;`` or ``$`` comments, keeping quoted strings."""
        in_quotes = False
        for i, ch in enumerate(text):
            if ch == '"':
                in_quotes = not in_quotes
                continue
            if in_quotes:
                continue
            if ch in {";", "$"}:
                return text[:i].rstrip()
        return text

    @staticmethod
    def _looks_like_component(statement: str) -> bool:
        token = statement.split(None, 1)[0] if statement.strip() else ""
        if not token or token.startswith("."):
            return False
        return token[0].upper() in _COMPONENT_KINDS

    @staticmethod
    def _parse_directive(statement: str, line_no: int) -> Dict[str, Any]:
        body = statement.lstrip()
        if body.startswith("."):
            body = body[1:]
        parts = body.split(None, 1)
        name = parts[0].lower() if parts else ""
        arguments = parts[1] if len(parts) > 1 else ""
        return {
            "line": line_no,
            "name": name,
            "arguments": arguments,
            "raw": statement,
        }

    @staticmethod
    def _parse_component(statement: str, line_no: int) -> Dict[str, Any]:
        tokens = statement.split()
        name = tokens[0]
        kind_key = name[0].upper()
        nodes, parameters = NetlistSanitizer._split_nodes_and_params(
            kind_key, tokens[1:]
        )
        return {
            "line": line_no,
            "name": name,
            "kind": _COMPONENT_KINDS.get(kind_key, "unknown"),
            "nodes": nodes,
            "parameters": parameters,
            "raw": statement,
        }

    @staticmethod
    def _split_nodes_and_params(
        kind_key: str, tokens: Sequence[str]
    ) -> Tuple[List[str], str]:
        """Split instance pins from the remaining value / model / options."""
        if kind_key == "X":
            # Subcircuit: last non key=value token is the subckt name.
            eq_index = next(
                (i for i, tok in enumerate(tokens) if "=" in tok), len(tokens)
            )
            head = list(tokens[:eq_index])
            tail = list(tokens[eq_index:])
            if head:
                return head[:-1], " ".join([head[-1], *tail]).strip()
            return [], " ".join(tail)

        if kind_key == "Q":
            arity = 4 if len(tokens) >= 4 and "=" not in tokens[3] else 3
        elif kind_key == "M":
            arity = 4
        else:
            arity = _NODE_ARITY.get(kind_key)

        nodes: List[str] = []
        param_start = 0
        for i, tok in enumerate(tokens):
            if "=" in tok or tok.upper() in _SOURCE_PARAM_START:
                param_start = i
                break
            if arity is not None and i >= arity:
                param_start = i
                break
            nodes.append(tok)
            param_start = i + 1
        parameters = " ".join(tokens[param_start:])
        return nodes, parameters


class FaultIdentifier:
    """Map Ngspice log text onto local, rule-based fault categories."""

    _RULES: Tuple[Dict[str, Any], ...] = (
        {
            "pattern": re.compile(r"singular\s+matrix", re.IGNORECASE),
            "category": "Connectivity Fault",
            "severity": "Critical",
            "suggested_action": (
                "Check for floating nodes or missing ground references. "
                "Add leakage resistors if necessary."
            ),
            "label": "singular matrix",
        },
        {
            "pattern": re.compile(r"model\s+not\s+found", re.IGNORECASE),
            "category": "Component Misconfiguration",
            "severity": "Critical",
            "suggested_action": (
                "Verify that all components have properly defined "
                ".model definitions."
            ),
            "label": "model not found",
        },
        {
            "pattern": re.compile(
                r"time\s*step\s+too\s+small|transient\s+analysis\s+failed",
                re.IGNORECASE,
            ),
            "category": "Convergence Fault",
            "severity": "Critical",
            "suggested_action": (
                "Transient integration failed to take a stable step. Relax "
                "tolerances with `.options RELTOL=1e-3 ABSTOL=1e-6 VNTOL=1e-3`, "
                "switch to `.options METHOD=GEAR`, and/or reduce source rise "
                "times. If discontinuities remain, add UIC to `.tran`."
            ),
            "label": "Timestep Too Small",
        },
        {
            "pattern": re.compile(r"iteration\s+limit\s+reached", re.IGNORECASE),
            "category": "Convergence Fault",
            "severity": "Critical",
            "suggested_action": (
                "The Newton-Raphson loop hit its cap. Raise transient "
                "iterations with `.options ITL4=100` (and ITL1=500 / ITL2=100 "
                "for DC). If it still stalls, loosen RELTOL or add a small "
                "series resistance at stiff nonlinear devices."
            ),
            "label": "Iteration Limit Reached",
        },
        {
            "pattern": re.compile(r"gmin\s+stepping\s+failed", re.IGNORECASE),
            "category": "Convergence Fault",
            "severity": "Critical",
            "suggested_action": (
                "DC Gmin homotopy did not converge. Increase the homotopy "
                "budget with `.options GMINSTEPS=200` and try a slightly "
                "larger conductance via `.options GMIN=1e-10`. Combine with "
                "`.options SRCSTEPS=100` and ITL1=500 if the bias point is "
                "still unreachable."
            ),
            "label": "Gmin Stepping Failed",
        },
        {
            "pattern": re.compile(
                r"less than\s+2\s+connections\s+at\s+node|node\s+.*\s+is\s+floating",
                re.IGNORECASE,
            ),
            "category": "Connectivity Fault",
            "severity": "High",
            "suggested_action": (
                "Check for unconnected pins or attach a high-value leakage "
                "resistor (1G) to ground."
            ),
            "label": "Floating Node",
        },
        {
            "pattern": re.compile(
                r"no ground node|ground node 0 not found",
                re.IGNORECASE,
            ),
            "category": "Connectivity Fault",
            "severity": "Critical",
            "suggested_action": (
                "Add a reference ground (Node 0) to the schematic."
            ),
            "label": "Missing Ground",
        },
        {
            "pattern": re.compile(
                r"voltage source .* and inductor .* formed a loop|"
                r"parallel voltage sources",
                re.IGNORECASE,
            ),
            "category": "Connectivity Fault",
            "severity": "Critical",
            "suggested_action": (
                "Avoid ideal voltage sources in parallel or in an inductor "
                "loop; add small series resistance."
            ),
            "label": "Voltage Source Loop",
        },
        {
            "pattern": re.compile(
                r"unknown subckt|subcircuit .* not found",
                re.IGNORECASE,
            ),
            "category": "Subcircuit Definition Missing",
            "severity": "High",
            "suggested_action": (
                "Verify .subckt name, instance prefix (X), or check if model "
                "file is included via .include."
            ),
            "label": "Undefined Subcircuit",
        },
        {
            "pattern": re.compile(
                r"unknown parameter|syntax error in line",
                re.IGNORECASE,
            ),
            "category": "Netlist Syntax Error",
            "severity": "Medium",
            "suggested_action": (
                "Inspect component parameter spelling, missing value, or "
                "malformed directive."
            ),
            "label": "Syntax Error",
        },
        {
            "pattern": re.compile(
                r"cannot open|no such file or directory",
                re.IGNORECASE,
            ),
            "category": "File System Error",
            "severity": "High",
            "suggested_action": (
                "Verify model/lib file path, case-sensitivity, and relative "
                "directory location."
            ),
            "label": "File Not Found",
        },
        {
            "pattern": re.compile(
                r"vector .* not found|unable to find vector",
                re.IGNORECASE,
            ),
            "category": "Control Command Fault",
            "severity": "Medium",
            "suggested_action": (
                "Ensure node name in .control plot/print statements matches "
                "actual netlist node names."
            ),
            "label": "Vector Not Found",
        },
        {
            "pattern": re.compile(
                r"event-driven simulation failed",
                re.IGNORECASE,
            ),
            "category": "Digital Simulation Fault",
            "severity": "High",
            "suggested_action": (
                "Check digital node states, clock timing, or bridge model "
                "configurations."
            ),
            "label": "Digital Simulation Failed",
        },
        {
            "pattern": re.compile(
                r"node .* is not an a/d node|mismatch in bridge connection",
                re.IGNORECASE,
            ),
            "category": "Bridge Misconfiguration",
            "severity": "High",
            "suggested_action": (
                "Ensure ADC/DAC bridge components are correctly inserted "
                "between analog and digital domains."
            ),
            "label": "Bridge Node Mismatch",
        },
        {
            "pattern": re.compile(
                r"division by zero|nan detected in vector",
                re.IGNORECASE,
            ),
            "category": "Math Evaluation Error",
            "severity": "High",
            "suggested_action": (
                "Check custom behavioral source (B-source) expressions for "
                "log/division boundaries."
            ),
            "label": "Division by Zero / NaN",
        },
        {
            "pattern": re.compile(
                r"out of memory|malloc failed",
                re.IGNORECASE,
            ),
            "category": "System Resource Fault",
            "severity": "Critical",
            "suggested_action": (
                "Reduce simulation duration, increase step size, or clear "
                "memory logs."
            ),
            "label": "Out of Memory",
        },
    )

    def __init__(self, error_log: str) -> None:
        if error_log is None:
            raise TypeError("error_log must be a string, not None")
        self.error_log = error_log

    def identify(self) -> List[Fault]:
        """Return every matching fault, in log-line order (deduplicated)."""
        seen: set = set()
        faults: List[Fault] = []

        for line_no, line in enumerate(self.error_log.splitlines(), start=1):
            for rule in self._RULES:
                match = rule["pattern"].search(line)
                if not match:
                    continue
                key = (rule["label"], line.strip())
                if key in seen:
                    continue
                seen.add(key)
                faults.append(
                    Fault(
                        category=rule["category"],
                        severity=rule["severity"],
                        suggested_action=rule["suggested_action"],
                        matched_pattern=rule["label"],
                        evidence=line.strip(),
                        line_numbers=(line_no,),
                    )
                )
        return faults

    def identify_first(self) -> Optional[Fault]:
        """Convenience: first (highest-priority by rule order) match, or None."""
        found = self.identify()
        return found[0] if found else None


def _demo() -> None:
    dummy_netlist = """\
RC Low-Pass Demo
* Passive filter with a behavioral load
V1 in 0 DC 1
R1 in mid 1k
C1 mid 0 1u
+ IC=0
Xload mid 0 myload
.model Dmod D
.tran 1u 1m
.control
run
plot v(mid)
.endc
.end
"""

    dummy_error_log = """\
Circuit: RC Low-Pass Demo
Doing analysis at TEMP = 27.000000 and TNOM = 27.000000
Warning: singular matrix:  check nodes mid and 0
doAnalyses: iteration limit reached
run simulation(s) aborted
"""

    sanitizer = NetlistSanitizer(dummy_netlist)
    structured = sanitizer.sanitize()

    identifier = FaultIdentifier(dummy_error_log)
    faults = identifier.identify()

    print("=== Sanitized netlist ===")
    print(json.dumps(structured, indent=2))
    print("\n=== Categorized faults ===")
    print(json.dumps([f.to_dict() for f in faults], indent=2))


if __name__ == "__main__":
    _demo()
