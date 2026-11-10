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

    # FIX(1A): heuristic to detect whether the first physical line is a SPICE title card.
    # SPICE only recognises a title on the very first physical line; comments (*), directives
    # (.) and continuation lines (+) are NOT title cards.
    def _detect_title(self, physical_lines):
        """Return the line_no of the title card, or 0 if no title is present."""
        if not physical_lines:
            return 0
        line_no, text = physical_lines[0]
        s = text.strip()
        if not s or s.startswith(("*", ".", "+")):
            return 0  # no title card — first content line is a comment/directive/continuation
        return line_no

    def sanitize(self):
        physical = self._split_physical_lines()
        # FIX(1A): use _detect_title instead of blindly assuming has_title=True
        title_line_no = self._detect_title(physical)
        title = ""
        # FIX(N1): only extract the title text when physical[0] actually IS the title line,
        # preventing a stale title_line_no from indexing the wrong line after a continuation join.
        if title_line_no and physical and physical[0][0] == title_line_no:
            title = physical[0][1].rstrip()

        logical_lines = self._join_continuations(physical)
        components = []
        directives = []
        
        for line_no, text in logical_lines:
            # FIX(1A): skip the title line only when one was detected
            if title_line_no and line_no == title_line_no:
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
        # FIX(1B): count bare tokens (no "=" and not starting with ".") to disambiguate
        # substrate/body node from model name.
        # BJT:   c b e [sub] model  → 4 pins if ≥5 bare tokens, else 3
        # MOSFET: d g s [body] model → 4 pins if ≥5 bare tokens, else 3
        # The substrate/body node is present only when there are exactly 5 bare tokens
        # (4 node pins + 1 model name) before any key=value parameters.
        arity = 2
        if kind_key == "Q":
            bare = [t for t in tokens[1:] if "=" not in t and not t.startswith(".")]
            arity = 4 if len(bare) >= 5 else 3
        elif kind_key == "M":
            bare = [t for t in tokens[1:] if "=" not in t and not t.startswith(".")]
            arity = 4 if len(bare) >= 5 else 3

        # FIX(1D): X-lines have variable node count — the last bare token is the subckt name
        # and everything between the instance name and it are the connected nodes.
        # Store subckt_name explicitly so the resolver never needs to re-tokenize params.
        # FIX(N2): derive the subckt token position directly from bare_idxs (positions of all
        # non-param tokens in the original token list) instead of a fragile next() search.
        if kind_key == "X":
            # bare_idxs: positions in `tokens` (skipping tokens[0]=name) with no "="
            bare_idxs = [i for i, t in enumerate(tokens) if i > 0 and "=" not in t]
            if not bare_idxs:
                return {
                    "kind": COMPONENT_KINDS.get("X", "subcircuit"),
                    "kind_key": "X",
                    "name": name,
                    "nodes": [],
                    "subckt_name": "",
                    "params": "",
                    "line": line_no,
                }
            subckt_token_idx = bare_idxs[-1]          # last bare token = subckt name
            subckt_name = tokens[subckt_token_idx]
            nodes = [tokens[i] for i in bare_idxs[:-1]]  # all bare tokens before it = nodes
            params = " ".join(tokens[subckt_token_idx + 1:])  # key=value pairs after name
            return {
                "kind": COMPONENT_KINDS.get("X", "subcircuit"),
                "kind_key": "X",
                "name": name,
                "nodes": nodes,
                "subckt_name": subckt_name,
                "params": params,
                "line": line_no,
            }

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