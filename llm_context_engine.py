"""
Offline eSim/Ngspice LLM context engine.

Builds a structured Ollama system prompt from a sanitized netlist and
local error metadata. No network or cloud API calls are used.
"""

from __future__ import annotations

import re
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


class LLMPromptBuilder:
    """Assemble a structured Ollama system prompt from local copilot data.

    Accepts ``error_data`` (category, severity, eSim fix command) and the
    dictionary produced by ``NetlistSanitizer.sanitize``. Call
    ``build_system_prompt()`` or ``build_prompt()``; both return the same
    prompt string. No LLM endpoint is contacted from this class.
    """

    _TOPOLOGY_GROUPS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
        ("Resistors", ("resistor",)),
        ("Capacitors", ("capacitor",)),
        ("Transistors", ("bjt", "mosfet", "jfet", "mesfet")),
        ("ICs", ("subcircuit",)),
    )
    _HIGHLIGHT_DIRECTIVES = ("tran", "ac", "lib", "include")

    def __init__(
        self,
        error_data: Optional[Dict[str, Any]] = None,
        sanitized_netlist: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.error_data: Dict[str, Any] = dict(error_data or {})
        self.sanitized_netlist: Dict[str, Any] = dict(sanitized_netlist or {})

    def build_prompt(
        self,
        error_data: Optional[Dict[str, Any]] = None,
        sanitized_netlist: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Optionally bind dictionaries, then return the system prompt."""
        if error_data is not None:
            self.error_data = dict(error_data)
        if sanitized_netlist is not None:
            self.sanitized_netlist = dict(sanitized_netlist)
        return self.build_system_prompt()

    def build_system_prompt(self) -> str:
        """Return a structured system prompt for a local Ollama model."""
        title = str(self.sanitized_netlist.get("title") or "Untitled circuit")
        sections = [
            self._role_preamble(title),
            self._error_section(),
            self._topology_section(),
            self._directive_section(),
            self._output_contract(),
        ]
        return "\n\n".join(section for section in sections if section)

    def _role_preamble(self, title: str) -> str:
        return (
            "You are an eSim Circuit Debugging Expert running fully offline "
            "beside Ngspice. Use only the structured context below. Do not "
            "invent components, nodes, or directives that are not listed. "
            "Prefer concrete schematic edits and SPICE cards the student can "
            "apply in eSim.\n"
            f"Circuit title: {title}"
        )

    def _error_section(self) -> str:
        data = self.error_data
        category = data.get("category") or "Unspecified"
        severity = data.get("severity") or "Unknown"
        fix = (
            data.get("esim_fix_command")
            or data.get("suggested_action")
            or data.get("fix_command")
            or "No eSim fix command was supplied."
        )
        extra_lines: List[str] = []
        for key in ("matched_pattern", "evidence", "label"):
            value = data.get(key)
            if value:
                extra_lines.append(f"- {key}: {value}")
        extras = ("\n" + "\n".join(extra_lines)) if extra_lines else ""
        return (
            "=== Identified Error & Suggested Manual Fixes ===\n"
            f"- Category: {category}\n"
            f"- Severity: {severity}\n"
            f"- eSim fix command: {fix}"
            f"{extras}\n"
            "Explain why this fault occurs on the topology below, then give "
            "numbered manual steps that apply the eSim fix command."
        )

    def _topology_section(self) -> str:
        components = list(self.sanitized_netlist.get("components") or [])
        grouped: Dict[str, List[Dict[str, Any]]] = {
            label: [] for label, _kinds in self._TOPOLOGY_GROUPS
        }
        assigned_ids = set()
        for label, kinds in self._TOPOLOGY_GROUPS:
            kind_set = set(kinds)
            for comp in components:
                if id(comp) in assigned_ids:
                    continue
                if str(comp.get("kind", "")).lower() in kind_set:
                    grouped[label].append(comp)
                    assigned_ids.add(id(comp))

        others = [c for c in components if id(c) not in assigned_ids]
        blocks = ["=== Cleaned Component Topology ==="]
        for label, _kinds in self._TOPOLOGY_GROUPS:
            blocks.append(self._format_component_group(label, grouped[label]))
        if others:
            blocks.append(self._format_component_group("Other devices", others))
        if not components:
            blocks.append("(no component declarations extracted)")
        return "\n".join(blocks)

    @staticmethod
    def _format_component_group(
        label: str, devices: Sequence[Dict[str, Any]]
    ) -> str:
        if not devices:
            return f"{label}: (none)"
        lines = [f"{label}:"]
        for device in devices:
            name = device.get("name", "?")
            nodes = device.get("nodes") or []
            node_txt = ", ".join(str(n) for n in nodes) if nodes else "—"
            params = device.get("parameters") or ""
            kind = device.get("kind") or ""
            suffix = f"  params={params}" if params else ""
            lines.append(f"  - {name} ({kind})  nodes=[{node_txt}]{suffix}")
        return "\n".join(lines)

    def _directive_section(self) -> str:
        directives = list(self.sanitized_netlist.get("directives") or [])
        control = list(self.sanitized_netlist.get("control_commands") or [])
        lines = ["=== Simulation Directives (.tran, .ac, .lib) ==="]
        if not directives:
            lines.append("(no dot-commands extracted)")
        else:
            highlighted = [
                d
                for d in directives
                if str(d.get("name", "")).lower() in self._HIGHLIGHT_DIRECTIVES
            ]
            remainder = [
                d
                for d in directives
                if str(d.get("name", "")).lower() not in self._HIGHLIGHT_DIRECTIVES
            ]
            if highlighted:
                lines.append("Primary analysis / library cards:")
                for item in highlighted:
                    lines.append(f"  - {LLMPromptBuilder._format_directive(item)}")
            if remainder:
                lines.append("Other directives:")
                for item in remainder:
                    lines.append(f"  - {LLMPromptBuilder._format_directive(item)}")
        if control:
            lines.append("Ngspice .control block:")
            for cmd in control:
                raw = cmd.get("raw") or cmd.get("name") or ""
                lines.append(f"  - {raw}")
        return "\n".join(lines)

    @staticmethod
    def _format_directive(item: Dict[str, Any]) -> str:
        raw = item.get("raw")
        if raw:
            return str(raw)
        name = item.get("name", "")
        arguments = item.get("arguments", "")
        return f".{name} {arguments}".rstrip()

    @staticmethod
    def _output_contract() -> str:
        return (
            "=== Response Format ===\n"
            "1) Diagnosis (2-4 sentences)\n"
            "2) Manual schematic / netlist edits\n"
            "3) Optional Ngspice .options or .tran/.ac/.lib changes\n"
            "Keep the answer concise and specific to this circuit."
        )


def _demo() -> None:
    dummy_netlist = """\
RC Amplifier Demo
* Mixed analog topology for copilot context
V1 in 0 DC 5
R1 in n1 10k
C1 n1 0 100n
Q1 n2 n1 0 2N2222
Xop n2 out 0 lm741
.lib 2N2222.lib
.ac dec 10 1 1Meg
.tran 1u 2m
.control
run
plot v(out)
.endc
.end
"""

    error_data = {
        "category": "Connectivity Fault",
        "severity": "Critical",
        "esim_fix_command": (
            "Place a 1G leakage resistor from node n2 to ground (Node 0) "
            "and confirm the LM741 subcircuit is included."
        ),
        "matched_pattern": "singular matrix",
        "evidence": (
            "Doing analysis at TEMP = 27.000000\n"
            "Warning: singular matrix:  check nodes n2 and 0\n"
            "run simulation(s) aborted"
        ),
    }

    sanitized_netlist = NetlistSanitizer(dummy_netlist).sanitize()
    prompt = LLMPromptBuilder(error_data, sanitized_netlist).build_system_prompt()
    print(prompt)


if __name__ == "__main__":
    _demo()
