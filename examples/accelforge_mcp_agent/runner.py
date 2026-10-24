"""Run one native AccelForge job. This process only exists inside a caller sandbox."""
import json,sys,math,traceback
from pathlib import Path
import accelforge as af
from accelforge.frontend.arch import Component

af.set_n_parallel_jobs(1)
config=json.loads(Path(sys.argv[1]).read_text())
print('Loading architecture and workload',flush=True)
spec=af.Spec.from_yaml(*config['files'],jinja_parse_data=config['parameters'])
if spec.config.expression_custom_functions or spec.config.component_models:
    raise ValueError('Custom Python plugins are not exposed by this hosted interface; use installed HWComponents models.')
spec.mapper.metrics={'energy':af.Metrics.ENERGY,'latency':af.Metrics.LATENCY,'energy_latency':af.Metrics.ENERGY|af.Metrics.LATENCY}[config['objective']]
summary={'mode':config['mode'],'upstream_commit':'7312a7d915625552bf6eb1db577941f8317b6ac7',
         'selectors':config['selectors'],'parameters':config['parameters'],'objective':config['objective'],
         'units':{'energy':'J (unless input deliberately uses normalized/synthetic costs)','latency':'s (depends on input throughput units)','area':'m^2'},
         'assumptions':'Analytical model results, not physical measurement. simple.yaml uses synthetic unit costs and zero area.'}
def clean(v):
    if isinstance(v,dict):return {str(k):clean(x) for k,x in v.items()}
    if isinstance(v,(list,tuple)):return [clean(x) for x in v]
    if hasattr(v,'item'):return clean(v.item())
    if isinstance(v,float) and not math.isfinite(v):return None
    if v is None or isinstance(v,(str,int,float,bool)):return v
    return str(v)

def save_summary():Path('summary.json').write_text(json.dumps(clean(summary),indent=2,allow_nan=False))
if config['mode']=='validate':
    evaluated=spec._spec_eval_expressions()
    summary.update({'validated':True,'einsums':list(evaluated.workload.einsum_names),'components':[n.name for n in evaluated.arch.get_nodes_of_type(Component)]})
    Path('validated-spec.json').write_text(json.dumps(clean(evaluated.model_dump()),indent=2,allow_nan=False))
    save_summary();print('Validation complete',flush=True);sys.exit(0)
print('Calculating component energy, area and throughput',flush=True)
cost_spec=spec.calculate_component_costs()
summary['area_m2']=cost_spec.arch.total_area
summary['component_area_m2']=cost_spec.arch.per_component_total_area
Path('component-costs.json').write_text(json.dumps(clean(cost_spec.model_dump()),indent=2,allow_nan=False))
if config['mode']=='costs':
    save_summary();print('Component costs complete',flush=True);sys.exit(0)
print('Searching mappings' if config['mode']=='map' else 'Evaluating supplied mapping',flush=True)
result=spec.map_workload_to_arch(print_progress=True) if config['mode']=='map' else spec.evaluate_mapping()
if len(result)==0:raise ValueError('No feasible mapping found for this architecture and workload')
energies=clean(result.energy(list_if_one_mapping=True));latencies=clean(result.latency(list_if_one_mapping=True))
energies=energies if isinstance(energies,list) else [energies];latencies=latencies if isinstance(latencies,list) else [latencies]
values=latencies if config['objective']=='latency' else energies
best=min(range(len(values)),key=lambda i:float('inf') if values[i] is None else values[i])
summary.update({'mapping_count':len(result),'selected_mapping_index':best,'selected_energy_j':energies[best],'selected_latency_s':latencies[best],
 'min_energy_j':min(x for x in energies if x is not None),'min_latency_s':min(x for x in latencies if x is not None),
 'energy_per_mapping_j':energies[:1000],'latency_per_mapping_s':latencies[:1000],
 'summary_rows_truncated':len(energies)>1000,'energy_by_component_j':clean(result[best].energy(per_component=True)),
 'latency_by_component_s':clean(result[best].latency(per_component=True))})
# Mapping objects are exported separately as reusable YAML, not opaque Python reprs.
cols=[c for c in result.data.columns if str(c).split('<SEP>')[-1]!='mapping']
table=result.data[cols]
table.to_csv('results.csv',index=False)
Path('results.json').write_text(table.to_json(orient='records',default_handler=str))
# Export public mapping fields with native YAML tags. Exclude transient references
# to architecture objects, whose upstream serializer is not a reusable mapping.
import yaml
from pydantic import BaseModel
from accelforge.frontend.mapping.mapping import MappingNode
from accelforge.util._setexpressions import InvertibleSet
class Tagged(dict):
    def __init__(self, tag, fields):super().__init__(fields);self.tag=tag
class Dumper(yaml.SafeDumper):pass
Dumper.add_representer(Tagged,lambda d,v:d.represent_mapping('!'+v.tag,dict(v)))
def export(v):
    if isinstance(v,InvertibleSet):return sorted(str(x) for x in v.instance)
    if isinstance(v,BaseModel):
        fields={k:export(getattr(v,k)) for k in type(v).model_fields if not k.endswith('_object') and getattr(v,k) is not None}
        if type(v).__name__=='Storage' and not fields.get('binding'):fields['binding']={}
        return Tagged(type(v).__name__,fields) if isinstance(v,MappingNode) else fields
    if isinstance(v,dict):return {str(k):export(x) for k,x in v.items()}
    if isinstance(v,(list,tuple,set,frozenset)):return [export(x) for x in v]
    if isinstance(v,str):return str(v)
    if v is None or type(v) in (int,float,bool):return v
    if hasattr(v,'item'):return export(v.item())
    return str(v)
selected=export(result.mapping(best))
Path('mapping.yaml').write_text(yaml.dump({'mapping':dict(selected)},Dumper=Dumper,sort_keys=False))
af.Spec.from_yaml(*config['files'][:2],'mapping.yaml',jinja_parse_data=config['parameters'])
Path('mapping.svg').write_text(result[best].render())
save_summary();print('Analysis complete: '+str(len(result))+' mapping(s)',flush=True)
