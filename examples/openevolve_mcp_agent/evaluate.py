"""Runs ONLY in a separate resource-limited, networkless evaluator container."""
import contextlib,importlib.util,json,math,sys
with contextlib.redirect_stdout(sys.stderr):
 spec=importlib.util.spec_from_file_location('user_evaluator','/input/evaluator.py')
 m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
 value=m.evaluate('/input/candidate.py')
if not isinstance(value,dict) or 'combined_score' not in value or len(value)>32:raise ValueError('Evaluator must return numeric metrics with combined_score')
if any(not isinstance(k,str) or len(k)>64 or type(v) not in (float,int) or not math.isfinite(v) for k,v in value.items()):raise ValueError('Metrics must be finite numbers')
print(json.dumps(value,allow_nan=False))
