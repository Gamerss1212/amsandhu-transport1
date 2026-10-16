import { spawn } from 'node:child_process';
import { dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
const root=dirname(fileURLToPath(import.meta.url)),port=Number(process.env.PORT||4317);
if(!Number.isInteger(port)||port<1024||port>65535)throw new Error('PORT must be an integer between 1024 and 65535.');
const url=`http://127.0.0.1:${port}`;
const ready=async()=>{try{const r=await fetch(url,{signal:AbortSignal.timeout(600)});return r.ok&&(await r.text()).includes('JARVIS · Trading Command');}catch{return false;}};
function openBrowser(){const args=process.platform==='win32'?['cmd.exe',['/d','/c','start','',url]]:process.platform==='darwin'?['open',[url]]:['xdg-open',[url]];const opener=spawn(...args,{windowsHide:true,stdio:'ignore'});opener.on('error',()=>console.log(`Open ${url} in your browser.`));opener.unref();}
if(await ready()){console.log(`Jarvis is already running at ${url}`);openBrowser();}
else{const server=spawn(process.execPath,['server.mjs'],{cwd:root,env:process.env,stdio:'inherit',windowsHide:true});let opened=false;
  for(let i=0;i<50;i++){if(server.exitCode!==null)break;if(await ready()){openBrowser();opened=true;break;}await new Promise(r=>setTimeout(r,100));}
  if(!opened)console.error(`Server not ready. Check the error above; then open ${url}.`);
  process.on('SIGINT',()=>server.kill('SIGINT'));process.on('SIGTERM',()=>server.kill('SIGTERM'));server.on('exit',code=>process.exit(code||0));
}
