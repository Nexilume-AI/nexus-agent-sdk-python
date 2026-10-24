from pathlib import Path
import json,subprocess
root=Path('/opt/nexus-agents/diffsynth-agent')
ready=json.loads((root/'runtime-ready.json').read_text())
assert ready['state']=='tested' and ready['recipe']=='wan2.1-t2v-1.3b'
units=Path.home()/'.config/systemd/user';units.mkdir(parents=True,exist_ok=True)
unit=units/'nexus-diffsynth-agent.service'
assert not unit.exists(),'Review existing service before replacement'
unit.write_bytes((root/'nexus-diffsynth-agent.service').read_bytes())
subprocess.run(['systemctl','--user','daemon-reload'],check=True)
subprocess.run(['systemctl','--user','enable','--now',unit.name],check=True)
print('INSTALLED',flush=True)
