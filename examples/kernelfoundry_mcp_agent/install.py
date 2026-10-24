from pathlib import Path
import subprocess
root=Path(__file__).resolve().parent
unit=Path.home()/'.config/systemd/user/nexus-kernelfoundry-agent.service'
assert not unit.exists(),'Review existing deployment before replacement'
unit.write_bytes((root/unit.name).read_bytes())
subprocess.run(['systemctl','--user','daemon-reload'],check=True)
subprocess.run(['systemctl','--user','enable','--now',unit.name],check=True)
print('REGISTERED; readiness remains gated on native acceptance')
