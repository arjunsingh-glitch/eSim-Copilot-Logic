from hardware_engine import HardwareContextEngine
import os

# (a) raw netlist string
netlist = """RC Test
V1 in 0 DC 5
R1 in out 10k
C1 out 0 100n
.tran 1u 1m
.end
"""
r1 = HardwareContextEngine().run(netlist)
assert set(["sanitized_netlist", "resolver_data", "auditor_data", "prompt"]).issubset(r1.keys())

# (b) file path
os.makedirs("/tmp", exist_ok=True)
with open("/tmp/_smoke.cir", "w") as f:
    f.write(netlist)
r2 = HardwareContextEngine().run("/tmp/_smoke.cir")
assert "prompt" in r2

# (c) raw_path with a small ASCII .raw file
raw = """Title: RC Test
Plotname: Transient Analysis
Flags: real
No. Variables: 2
No. Points: 2
Variables:
	0	time	time
	1	v(out)	voltage
Values:
0	0.0	0.0
1	1e-06	0.0
"""
with open("/tmp/_smoke.raw", "w") as f:
    f.write(raw)
r3 = HardwareContextEngine().run("/tmp/_smoke.cir", raw_path="/tmp/_smoke.raw")
assert any(e.get("type") in ("all_zero_output", "flatline_output") for e in r3["auditor_data"]["errors"]), r3["auditor_data"]

print("ALL SMOKE TESTS PASSED")
