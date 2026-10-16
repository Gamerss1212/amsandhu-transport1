import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdtempSync,mkdirSync } from 'node:fs';
import { dirname,resolve,join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { once } from 'node:events';
import { createHmac } from 'node:crypto';
const root=resolve(dirname(fileURLToPath(import.meta.url)),'..'),work=resolve(root,'../../work');mkdirSync(work,{recursive:true});
const data=mkdtempSync(join(work,'jarvis-test-')),port=45000+Math.floor(Math.random()*5000),url=`http://127.0.0.1:${port}`;
const phone={AccountSid:'AC'+'1'.repeat(32),CallSid:'CA'+'2'.repeat(32),From:'+15555550100',To:'+15555550101',Direction:'inbound'};
const env={...process.env,PORT:String(port),JARVIS_DATA_DIR:data,OLLAMA_MODEL:'',ALPACA_API_KEY:'',ALPACA_SECRET_KEY:'',TWILIO_ACCOUNT_SID:phone.AccountSid,TWILIO_AUTH_TOKEN:'test-token-only',TWILIO_FROM_NUMBER:phone.To,OWNER_PHONE_NUMBER:phone.From,PHONE_PIN:'739154',PUBLIC_VOICE_URL:'https://voice.example.test',BRIDGE_TOKEN:'test-only-bridge-token-'.repeat(2)};
let child,key;
async function start(){child=spawn(process.execPath,['server.mjs'],{cwd:root,env,stdio:['ignore','pipe','pipe']});await new Promise((resolve,reject)=>{const timer=setTimeout(()=>reject(new Error('Server startup timed out')),10000);child.stdout.on('data',chunk=>{if(chunk.toString().includes('Jarvis is ready')){clearTimeout(timer);resolve();}});child.once('error',reject);child.once('exit',code=>{clearTimeout(timer);reject(new Error(`Server exited ${code}`));});});key=(await(await fetch(url+'/api/session')).json()).key;}
async function stop(){if(child?.exitCode===null){const exited=once(child,'exit');child.kill();await exited;}}
const request=(path,value,headers={})=>fetch(url+path,{method:value===undefined?'GET':'POST',headers:{'x-jarvis-key':key,...(value===undefined?{}:{'Content-Type':'application/json'}),...headers},...(value===undefined?{}:{body:JSON.stringify(value)})});
const voice=(path,params,bad=false)=>{const p={...phone,...params};const text='https://voice.example.test'+path+Object.keys(p).sort().map(k=>k+p[k]).join('');const signature=createHmac('sha1',env.TWILIO_AUTH_TOKEN).update(text).digest('base64');return fetch(url+path,{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded','X-Twilio-Signature':bad?'bad':signature},body:new URLSearchParams(p)});};

test('HTTP workflows, trust boundaries, telephone authentication and persistence',{timeout:30000},async t=>{
  await start();t.after(stop);
  await t.test('dashboard serves; API requires session; cross-origin and secret file reads fail',async()=>{
    assert.equal((await fetch(url)).status,200);assert.equal((await fetch(url+'/api/state')).status,403);
    assert.equal((await request('/api/control',{action:'halt'},{Origin:'https://evil.example'})).status,403);
    assert.equal((await fetch(url+'/.env')).status,404);assert.equal((await fetch(url+'/lib/core.mjs')).status,404);
  });
  await t.test('simultaneous duplicate orders produce one fill',async()=>{
    const order={symbol:'BTC-USD',side:'buy',notional:500,clientId:'http-duplicate-1'};
    const results=await Promise.all([request('/api/order',order),request('/api/order',order)]);assert.ok(results.every(r=>r.ok));
    const state=await(await request('/api/state')).json();assert.equal(state.orders.length,1);assert.equal(state.positions.length,1);assert.equal(state.council.length,25);
  });
  await t.test('feed mixing is rejected; chat trade intent does not trade',async()=>{
    assert.equal((await request('/api/feed',{feed:'connected'})).status,400);
    const result=await(await request('/api/chat',{message:'buy 500 dollars of bitcoin'})).json();assert.match(result.text,/paper order ticket/);
    assert.equal((await(await request('/api/state')).json()).orders.length,1);
  });
  await t.test('voice signatures, owner restriction, PIN and stop action are enforced',async()=>{
    assert.equal((await voice('/voice/incoming',{},true)).status,403);
    assert.equal((await voice('/voice/incoming',{From:'+15555550999'})).status,403);
    const incoming=await(await voice('/voice/incoming',{})).text();assert.match(incoming,/private PIN/);assert.doesNotMatch(incoming,/Equity/);
    assert.equal((await voice('/voice/talk',{SpeechResult:'brief me'})).status,403);
    assert.match(await(await voice('/voice/auth',{Digits:'000000'})).text(),/Incorrect PIN/);
    assert.match(await(await voice('/voice/auth',{Digits:env.PHONE_PIN})).text(),/simulated trading account/);
    assert.match(await(await voice('/voice/talk',{SpeechResult:'stop trading'})).text(),/Entries stopped/);
    assert.equal((await request('/api/order',{symbol:'ETH-USD',side:'buy',notional:500,clientId:'stopped-entry'})).status,400);
  });
  await t.test('phone brute-force lockout and read-only bridge authentication',async()=>{
    const otherSid='CA'+'3'.repeat(32);for(let i=0;i<3;i++)await voice('/voice/auth',{CallSid:otherSid,Digits:'000000'});
    assert.match(await(await voice('/voice/auth',{CallSid:otherSid,Digits:env.PHONE_PIN})).text(),/Access denied/);
    assert.equal((await fetch(url+'/bridge/context')).status,403);
    const r=await fetch(url+'/bridge/context',{headers:{Authorization:`Bearer ${env.BRIDGE_TOKEN}`}});assert.equal(r.status,200);assert.equal((await r.json()).mode,'paper');
  });
  await t.test('backtest produces honest synthetic labeling; missing AI rejects council',async()=>{
    const r=await(await request('/api/backtest',{})).json();assert.match(r.source,/Synthetic/);assert.equal(r.evaluationBars,180);
    assert.equal((await request('/api/council',{all:true})).status,400);
  });
  await t.test('call preferences validate without placing any calls',async()=>{
    const prefs={onRisk:false,onAlert:false,dailyLimit:3,quietStart:22,quietEnd:8,timeZone:'America/Toronto'};
    assert.equal((await request('/api/notifications',prefs)).status,200);
    assert.equal((await request('/api/notifications',{...prefs,dailyLimit:50})).status,400);
    assert.equal((await request('/api/notifications',{...prefs,timeZone:'Not/A_Zone'})).status,400);
  });
  await t.test('database survives restart and retains emergency stop',async()=>{
    await stop();await start();const state=await(await request('/api/state')).json();assert.equal(state.orders.length,1);assert.equal(state.halted,true);assert.equal(state.autopilot,false);assert.equal(state.cash,undefined);assert.ok(state.account.cash<100000);
    const close=await request('/api/order',{symbol:'BTC-USD',side:'sell',clientId:'close-after-restart'});assert.equal(close.status,200);
  });
});
