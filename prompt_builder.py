class PromptBuilder:
    def __init__(self, error_data, sanitized_netlist, resolver_data, auditor_data):
        self.error = error_data or {}
        self.netlist = sanitized_netlist
        self.resolver = resolver_data
        self.auditor = auditor_data

    def build_system_prompt(self) -> str:
        prompt = "You are an expert EDA debugging assistant for FOSSEE eSim. Treat the following SPICE context as the ABSOLUTE TRUTH, ignore vision topology if it contradicts this.\n\n"
        
        # FIX(BUG-5.1): include the eSim fix command so the LLM does not invent one
        prompt += "=== ERROR METADATA ===\n"
        prompt += f"Type: {self.error.get('category', 'None')}\n"
        prompt += f"Severity: {self.error.get('severity', 'None')}\n"
        prompt += f"Suggested eSim fix: {self.error.get('esim_fix_command', 'No fix command supplied')}\n"
        
        prompt += "\n=== SPICE TOPOLOGY ===\n"
        for comp in self.netlist.get("components", []):
            prompt += f"{comp['kind']} {comp['name']} | Nodes: {', '.join(comp['nodes'])} | Params: {comp['params']}\n"
            
        # FIX(BUG-5.2): pin_errors may be list[str] or list[dict]; do not index strings as dicts
        prompt += "\n=== SUBCIRCUIT HIERARCHY & PIN ERRORS ===\n"
        pin_errors = self.resolver.get("pin_errors") or []
        if pin_errors:
            for err in pin_errors:
                if isinstance(err, dict):
                    prompt += f"- {err.get('type', 'pin_error')}: {err.get('detail', err)}\n"
                else:
                    prompt += f"- {err}\n"
        else:
            prompt += "No pin mismatch detected.\n"
            
        prompt += "\n=== SIMULATION WAVEFORM FACTS ===\n"
        if self.auditor.get("errors"):
            for err in self.auditor["errors"]:
                prompt += f"Logic Error: {err['type']} on node {err['node']}\n"
        else:
            prompt += "Simulation stats are clean or not available.\n"
            
        prompt += "\nProvide a precise, hardware-level diagnostic using only the components and facts listed above."
        return prompt