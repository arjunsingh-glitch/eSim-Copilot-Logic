import os
import re
from netlist_sanitizer import NetlistSanitizer

_MAX_INCLUDE_DEPTH = 16
_MAX_INSTANCE_DEPTH = 64
_PATH_TOKEN_RE = re.compile(r"""(?:["'](.*?)["'])|(\S+)""")


class SubcktHierarchyResolver:
    def __init__(self, netlist_dir: str, search_paths=None, virtual_files=None):
        self.netlist_dir = netlist_dir
        self.search_paths = list(search_paths) if search_paths else []
        self.virtual_files = {
            os.path.normcase(os.path.normpath(path)): (path, text)
            for path, text in (virtual_files or {}).items()
        }

    # FIX(BUG-3.1): keep visited/unresolved as resolve() locals so a second call is not stale
    def resolve(self, components, directives):
        subcircuits = {}
        visited = set()
        unresolved = []

        for d in directives:
            name = d["text"].split()[0].lower().strip('.')
            if name in {"include", "lib"}:
                self._ingest_source(d["text"], subcircuits, 0, visited, unresolved)

        tree = [
            self._expand_instance(comp, subcircuits, [], 0)
            for comp in components
            if (comp.get("kind_key") or comp.get("kind")) == "X"
        ]

        pin_errors = self._audit_pins(tree)
        return {
            "hierarchy": tree,
            "subcircuits": subcircuits,
            "pin_errors": pin_errors,
            "unresolved": unresolved,
        }

    def _ingest_source(self, directive_text, subcircuits, depth, visited, unresolved):
        if depth >= _MAX_INCLUDE_DEPTH:
            return
        m = _PATH_TOKEN_RE.search(directive_text.split(maxsplit=1)[-1])
        if not m:
            return
        candidate = m.group(1) or m.group(2)

        v_key = os.path.normcase(os.path.normpath(candidate))
        if v_key in self.virtual_files:
            orig_path, text = self.virtual_files[v_key]
            abs_path = os.path.abspath(orig_path)
            if abs_path in visited:
                return
            visited.add(abs_path)
            self._parse_library_text(text, abs_path, subcircuits, depth, visited, unresolved)
            return

        for p in [self.netlist_dir] + self.search_paths:
            full_path = os.path.abspath(os.path.join(p, candidate))
            if os.path.isfile(full_path) and full_path not in visited:
                visited.add(full_path)
                with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                    self._parse_library_text(f.read(), full_path, subcircuits, depth, visited, unresolved)
                return
        unresolved.append(candidate)

    # FIX(BUG-3.2): case-normalize .control/.endc so mixed-case control blocks are skipped
    def _parse_library_text(self, text, source_id, subcircuits, depth, visited, unresolved):
        in_control = False
        current = None
        for line_no, statement in NetlistSanitizer(text)._join_continuations(NetlistSanitizer(text)._split_physical_lines()):
            stripped = statement.strip()
            lowered = stripped.lower()
            if lowered.startswith(".control"):
                in_control = True
            elif lowered.startswith(".endc"):
                in_control = False
            elif in_control:
                continue
            elif lowered.startswith(".subckt"):
                tokens = stripped.split()
                if len(tokens) >= 2:
                    current = {"name": tokens[1], "pins": tokens[2:], "instances": [], "source": source_id}
            elif lowered.startswith(".ends"):
                if current:
                    subcircuits[current["name"].lower()] = current
                    current = None
            elif current and stripped.upper().startswith("X"):
                current["instances"].append(NetlistSanitizer(stripped)._parse_component(stripped, line_no))

    def _expand_instance(self, inst, subcircuits, ancestors, _depth=0):
        if (inst.get("kind_key") or inst.get("kind", "X")) != "X":
            return None
        key = str(inst.get("params") or "").split()[0].lower()
        node = {"name": inst["name"], "subckt": key, "nodes": inst["nodes"], "children": []}
        if _depth >= _MAX_INSTANCE_DEPTH:
            node["truncated"] = True
            return node

        definition = subcircuits.get(key)
        if definition:
            node["expected_pins"] = definition["pins"]
            child_ancestors = ancestors + [inst["name"]]
            node["children"] = [
                child
                for child in (
                    self._expand_instance(item, subcircuits, child_ancestors, _depth + 1)
                    for item in definition.get("instances", [])
                )
                if child is not None
            ]
        return node

    def _audit_pins(self, tree):
        errors = []
        for node in tree:
            if "expected_pins" in node and len(node["nodes"]) != len(node["expected_pins"]):
                errors.append(f"Pin mismatch in {node['name']}: passed {len(node['nodes'])} nodes, expected {len(node['expected_pins'])}.")
            errors.extend(self._audit_pins(node.get("children", [])))
        return errors
