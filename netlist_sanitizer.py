import re

COMMENT_RE = re.compile(r"^\*")
DIRECTIVE_RE = re.compile(r"^\.")

# FIX(BUG-4.2): restore human-readable kind names; kind_key stays the SPICE letter
COMPONENT_KINDS = {
    "C": "capacitor", "D": "diode", "E": "vcvs", "F": "cccs", "G": "vccs",
    "H": "ccvs", "I": "current_source", "J": "jfet", "K": "mutual_inductor",
    "L": "inductor", "M": "mosfet", "Q": "bjt", "R": "resistor",
    "S": "voltage_switch", "T": "transmission_line", "V": "voltage_source",
    "W": "current_switch", "X": "subcircuit", "Y": "admittance", "Z": "mesfet",
}

class NetlistSanitizer:
    def __init__(self, netlist_text: str):
        self.raw_text = netlist_text
        self.has_title = True

    def _split_physical_lines(self):
        return [(i + 1, line) for i, line in enumerate(self.raw_text.splitlines()) if line.strip()]

    def _join_continuations(self, physical_lines):
        logical = []
        for line_no, text in physical_lines:
            if text.startswith("+") and logical:
                logical[-1][1] += " " + text[1:].strip()
            else:
                logical.append([line_no, text.strip()])
        return logical

    def sanitize(self):
        physical = self._split_physical_lines()
        title = ""
        title_line_no = 0
        if self.has_title and physical:
            title_line_no, title = physical[0]
            title = title[1].rstrip()

        logical_lines = self._join_continuations(physical)
        components = []
        directives = []
        
        for line_no, text in logical_lines:
            if line_no == title_line_no:
                continue
            
            stripped = text.strip()
            if COMMENT_RE.match(stripped):
                continue
            
            statement = self._strip_inline_comment(stripped)
            if DIRECTIVE_RE.match(statement):
                directives.append({"line": line_no, "text": statement})
            else:
                components.append(self._parse_component(statement, line_no))
                
        return {
            "title": title,
            "components": components,
            "directives": directives,
            "stats": {"component_count": len(components), "directive_count": len(directives)}
        }

    def _strip_inline_comment(self, text: str) -> str:
        in_quotes = False
        brace_depth = 0
        for i, ch in enumerate(text):
            if ch == '"':
                in_quotes = not in_quotes
                continue
            if in_quotes:
                continue
            if ch == "{":
                brace_depth += 1
            elif ch == "}":
                brace_depth = max(0, brace_depth - 1)
            elif ch in {";", "$"} and brace_depth == 0:
                return text[:i].rstrip()
        return text

    def _parse_component(self, text: str, line_no: int):
        tokens = text.split()
        if not tokens:
            return {"kind": "unknown", "kind_key": "", "name": "Unknown", "nodes": [], "params": text}
        name = tokens[0]
        kind_key = name[0].upper()
        
        # FIX(BUG-4.1): 3-pin MOSFET (M d g s model) must not treat the model name as a node
        arity = 2
        if kind_key == "Q":
            if len(tokens) >= 5 and "=" not in tokens[3] and "=" not in tokens[4]:
                arity = 4
            else:
                arity = 3
        elif kind_key == "M":
            arity = 4 if len(tokens) >= 5 and "=" not in tokens[4] else 3

        nodes = tokens[1:arity+1] if len(tokens) > 1 else []
        params = " ".join(tokens[arity+1:]) if len(tokens) > arity + 1 else ""
        return {
            "kind": COMPONENT_KINDS.get(kind_key, "unknown"),
            "kind_key": kind_key,
            "name": name,
            "nodes": nodes,
            "params": params,
            "line": line_no,
        }