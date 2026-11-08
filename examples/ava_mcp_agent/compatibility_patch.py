"""Explicit CSR extension required by modern RISC-V binutils for native harness."""
from pathlib import Path
p=Path('/opt/ava/AGENT_G/asm_builder.py');s=p.read_text()
assert s.count('"-march=rv32im"')==1 and s.count('"--isa=rv32im"')==1
p.write_text(s.replace('"-march=rv32im"','"-march=rv32im_zicsr"').replace('"--isa=rv32im"','"--isa=rv32im_zicsr"'))
