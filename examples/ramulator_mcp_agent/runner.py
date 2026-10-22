"""Fixed entry point: JSON parameters only; no shell or user Python execution."""
import json
import sys
from pathlib import Path
import ramulator as r

CONTROLLERS = {'DDR3':'GenericDDR','DDR4':'GenericDDR','DDR5':'GenericDDR','GDDR6':'GenericDDR',
               'GDDR7':'GDDR7','LPDDR5':'LPDDR5','LPDDR6':'LPDDR6',
               'HBM1':'HBM12','HBM2':'HBM12','HBM3':'HBM34','HBM4':'HBM34'}

def presets():
    return {name:{'organizations':list(getattr(r.dram,name).org_presets),
                  'timings':list(getattr(r.dram,name).timing_presets), 'controller':controller}
            for name,controller in CONTROLLERS.items()}

def simulate(config):
    standard=config['standard'];cls=getattr(r.dram,standard)
    controllers=[]
    for _ in range(config['channels']):
        dram=cls(org_preset=config['organization'],timing_preset=config['timing'],**config['overrides'])
        refresh=r.refresh_manager.HBM34PerBankRefresh() if standard in ('HBM3','HBM4') else r.refresh_manager.AllBank()
        controllers.append(getattr(r.controller,CONTROLLERS[standard])(
            dram=dram,scheduler=r.scheduler.FRFCFS(),refresh_manager=refresh,
            row_policy=r.row_policy.Open(),addr_mapper=r.addr_mapper.RoBaRaCoCh()))
    frontend=r.frontend.LoadStoreTrace(clock_ratio=1,path=config['trace'])
    memory=r.memory_system.GenericDRAM(clock_ratio=1,controllers=controllers,
                                      channel_mapper=r.channel_mapper.CacheLineInterleave())
    Path('resolved_config.json').write_text(json.dumps({'frontend':frontend.to_config(),'memory_system':memory.to_config()},indent=2))
    print('Starting cycle-accurate simulation',flush=True)
    simulation=r.Simulation(frontend,memory);simulation.run()
    stats=simulation.stats
    Path('stats.json').write_text(json.dumps(stats,indent=2,allow_nan=False))
    Path('stats.yaml').write_text(simulation.stats_yaml)
    print('Simulation completed; stats.json and stats.yaml are ready',flush=True)

if __name__=='__main__':
    simulate(json.loads(Path(sys.argv[1]).read_text()))
