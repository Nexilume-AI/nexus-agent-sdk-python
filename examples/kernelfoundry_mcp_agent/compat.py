"""Semantics-preserving Python3.10 compatibility for pinned upstream."""
from pathlib import Path
import kernelfoundry
p=Path(kernelfoundry.__file__).parent/'algorithm/utils/eval_helper.py'
lines=p.read_text().splitlines()
found=False
for i,line in enumerate(lines):
 if 'logging.debug(f"_classify_clang_lines: src files:' in line:
  indent=line[:len(line)-len(line.lstrip())]
  lines[i]=indent+'logging.debug("_classify_clang_lines: src files: %s", "\\n    ".join(src_files))';found=True
assert found
p.write_text('\n'.join(lines)+'\n')
for p in Path(kernelfoundry.__file__).parent.rglob('*.py'):
 s=p.read_text()
 if p.name=='schemas.py' and 'eval(param_str)' in s:
  s='import ast\n'+s.replace('eval(param_str)','ast.literal_eval(param_str)');p.write_text(s)
 if p.name=='problem_logger.py' and 'eval(runtime_stats_str)' in s:
  s='import ast\n'+s.replace('eval(runtime_stats_str)','ast.literal_eval(runtime_stats_str)');p.write_text(s)
 if ', delete=not os.environ.get("DBG_KEEP_TEMPDIRS", False)' in s:
  s=s.replace(', delete=not os.environ.get("DBG_KEEP_TEMPDIRS", False)','');p.write_text(s)
 if 'from typing import override, Optional' in s:
  p.write_text(s.replace('from typing import override, Optional','from typing import Optional\nfrom typing_extensions import override'))
 elif 'from typing import override' in s:
  p.write_text(s.replace('from typing import override','from typing_extensions import override'))
