import fs from 'node:fs';
import path from 'node:path';
import {createReadTool,createBashTool,createWriteTool,createEditTool} from '@earendil-works/pi-coding-agent';
let raw='';for await(const chunk of process.stdin){raw+=chunk;if(raw.length>131072)throw new Error('INPUT_LIMIT');}
const call=JSON.parse(raw);
if(call.name==='export'){
 const target=fs.realpathSync(path.resolve('/workspace',call.args.path));
 if(!target.startsWith('/workspace/')||!fs.statSync(target).isFile()||fs.statSync(target).size>8388608)throw new Error('INVALID_EXPORT');
 console.log(JSON.stringify({name:path.basename(target),data:fs.readFileSync(target).toString('base64')}));
}else{
 const tools=[createReadTool('/workspace'),createBashTool('/workspace'),createWriteTool('/workspace'),createEditTool('/workspace')];
 const tool=tools.find(t=>t.name===call.name);if(!tool)throw new Error('UNKNOWN_TOOL');
 if(call.name==='bash')call.args.timeout=Math.min(60,Math.max(1,call.args.timeout||60));
 const value=await tool.execute(call.id||'nexus',call.args,AbortSignal.timeout(65000));
 console.log(JSON.stringify(value));
}
