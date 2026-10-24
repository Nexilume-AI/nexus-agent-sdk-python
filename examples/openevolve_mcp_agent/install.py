from pathlib import Path
import json,subprocess
root=Path('/opt/nexus-agents/openevolve-agent')
assert json.loads((root/'runtime-ready.json').read_text())['native_engine_tested']
unit=Path.home()/'.config/systemd/user/nexus-openevolve-agent.service'
assert not unit.exists(),'Existing unit requires review'
unit.write_bytes((root/unit.name).read_bytes())
subprocess.run(['systemctl','--user','daemon-reload'],check=True)
subprocess.run(['systemctl','--user','enable','--now',unit.name],check=True)
print('INSTALLED')
