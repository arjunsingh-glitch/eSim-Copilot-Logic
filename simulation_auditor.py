import math
import re


class SimulationAuditor:
    _FLAT_ABS = 1e-12
    _FLAT_REL = 1e-4
    _RAIL_PREFIXES = ("vdd", "vcc", "avdd", "dvdd", "vpwr", "vsupply", "vss", "vee", "avss", "vddio", "vddcore")
    _OUTPUT_PREFIXES = ("out", "vout", "dout", "aout", "vo")

    # FIX(BUG-2.2): skip heuristics on NaN/Inf-dirty vectors; keep stats even when empty
    def audit(self, raw_text: str = None) -> dict:
        if not raw_text:
            return {"notes": ["no plot data supplied"], "errors": [], "stats": []}

        parsed = self._parse_ascii_raw(raw_text)
        nan_vectors = parsed.get("nan_vectors", set())
        stats = []
        for name, values in parsed.get("vectors", {}).items():
            if not values:
                continue
            mean = sum(values) / len(values)
            vmin, vmax = min(values), max(values)
            scale = max(abs(mean), abs(vmin), abs(vmax))
            peak = max(abs(vmax - mean), abs(mean - vmin))
            stats.append({
                "name": name,
                "mean": mean,
                "min": vmin,
                "max": vmax,
                "flat": peak <= max(self._FLAT_ABS, self._FLAT_REL * scale) if scale > 0 else True,
                "all_zero": abs(vmax) < 1e-12 and abs(vmin) < 1e-12,
                "count": len(values),
                "nan_dirty": name in nan_vectors,
            })

        errors = []
        for item in stats:
            if item["count"] == 0 or item.get("nan_dirty"):
                continue
            key = item["name"].lower()
            if self._is_rail(key) and item.get("max") is not None and abs(item["max"]) < 0.05:
                errors.append({"type": "near_zero_rail", "node": key})
            if item.get("all_zero") and self._is_output(key):
                errors.append({"type": "all_zero_output", "node": key})
            if item.get("flat") and self._is_output(key) and not item.get("all_zero"):
                errors.append({"type": "flatline_output", "node": key})

        return {"stats": stats, "errors": errors, "notes": parsed.get("notes", [])}

    def _is_rail(self, key):
        compact = key.replace("+", "").replace("-", "").replace("!", "")
        return any(compact == tok or compact.startswith(tok) for tok in self._RAIL_PREFIXES)

    def _is_output(self, key):
        tail = key.split(".")[-1]
        # Ngspice ASCII RAW names look like v(out); compare the node, not the wrapper
        if tail.startswith("v(") and tail.endswith(")"):
            tail = tail[2:-1]
        return any(tail.startswith(p) for p in self._OUTPUT_PREFIXES)

    # FIX(BUG-2.1): accept float point indices, skip complex RAW, never coerce NaN/Inf to 0.0
    def _parse_ascii_raw(self, text: str):
        flags_match = re.search(r"(?im)^Flags:\s*(.*)$", text)
        flags = flags_match.group(1).strip().lower() if flags_match else ""

        nvars_match = re.search(r"No\.\s*Variables:\s*(\d+)", text)
        nvars = int(nvars_match.group(1)) if nvars_match else 0

        if "complex" in flags:
            return {
                "vectors": {},
                "nan_vectors": set(),
                "notes": ["complex ASCII raw not audited (only real plots supported)"],
            }

        variables = []
        var_section = re.search(r"(?ims)^Variables:\s*(.*?)^(Values:|Binary:)", text)
        if var_section:
            for line in var_section.group(1).splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[0].isdigit():
                    variables.append(parts[1])

        values_match = re.search(r"(?im)^Values:\s*$", text)
        samples = {name: [] for name in variables}
        nan_vectors = set()
        truncated = False

        if values_match and nvars > 0:
            body = text[values_match.end():]
            tokens = body.split()
            stride = nvars + 1
            cursor = 0
            while cursor + stride <= len(tokens):
                try:
                    float(tokens[cursor])
                except ValueError:
                    break
                cursor += 1
                for idx in range(nvars):
                    try:
                        raw_val = float(tokens[cursor])
                        cursor += 1
                    except (ValueError, IndexError):
                        truncated = True
                        break
                    if not math.isfinite(raw_val):
                        if idx < len(variables):
                            nan_vectors.add(variables[idx])
                        continue
                    if idx < len(variables):
                        samples[variables[idx]].append(raw_val)
                if truncated:
                    break

        notes = []
        if truncated:
            notes.append("Truncated due to malformed data")
        if nan_vectors:
            notes.append("Non-finite samples skipped in: " + ", ".join(sorted(nan_vectors)))
        return {"vectors": samples, "nan_vectors": nan_vectors, "notes": notes}
