from pathlib import Path
p=Path('/opt/verisure/eda_agent/verilator_judge.py');s=p.read_text();old='"-j",\n        "0",';assert s.count(old)==1;p.write_text(s.replace(old,'"-j",\n        "2",'))
p=Path('/opt/verisure/eda_agent/sim_reviewer.py');s=p.read_text();assert 'verilator --binary -j 0' in s;p.write_text(s.replace('verilator --binary -j 0','verilator --binary -j 2'))
from pathlib import Path
p=Path('/usr/local/bin/sby')
p.write_bytes(p.read_bytes().replace(b'\r\n',b'\n'))
