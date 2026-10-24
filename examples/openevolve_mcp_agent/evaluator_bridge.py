from pathlib import Path
from ipc_client import request
def evaluate(program_path):
 code=Path(program_path).read_text()
 return request('/evaluate',{'code':code})['metrics']
