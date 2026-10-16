import http from 'node:http';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { dirname,join } from 'node:path';
import { randomBytes,randomUUID,timingSafeEqual } from 'node:crypto';
import { openStore } from './lib/store.mjs';
import { makeState,account,log,placeOrder,cycle,validateLimits,finite,indicators } from './lib/core.mjs';
import { parseCSV,backtest,demoBacktest } from './lib/backtest.mjs';
import { council,askModel,ROLES } from './lib/agents.mjs';
import { refreshMarkets,applyQuote,tokenResearch,briefing,twiml,validSignature,phoneConfigured,callOwner,canNotify } from './lib/providers.mjs';

const root=dirname(fileURLToPath(import.meta.url)),port=Number(process.env.PORT||4317);
const store=openStore(process.env.JARVIS_DATA_DIR||join(root,'data')),s=store.state;
s.notifications??=makeState().notifications;
const sessionKey=randomBytes(32).toString('hex'),calls=new Map();
let refreshing=false,lastRefresh=0,councilJob=null,lastResearch=0,chatBusy=false,callBusy=false;
let notificationCursor=Date.now();
const same=(a,b)=>{const x=Buffer.from(a||''),y=Buffer.from(b||'');return x.length===y.length&&timingSafeEqual(x,y);};
const origin=`http://127.0.0.1:${port}`;
function status(){return {ai:process.env.OLLAMA_MODEL?'Configured; connection tested on use':'Not configured',model:process.env.OLLAMA_MODEL||null,
  phone:phoneConfigured()?'Configured; end-to-end test required':'Not configured',stockData:process.env.ALPACA_API_KEY&&process.env.ALPACA_SECRET_KEY?'Configured':'Not configured',
  execution:'Local paper ledger only',region:'Canada · province not specified',currency:'USD',bridge:(process.env.BRIDGE_TOKEN||'').length>=32?'Read-only bridge configured':'Not configured'};}
function snapshot(symbol){return {mode:s.mode,feed:s.feed,account:account(s),limits:s.limits,positions:s.positions,orders:s.orders.slice(0,100),events:s.events.slice(0,80),
  markets:s.markets.map(m=>({...m,bars:m.bars.slice(-180),indicators:indicators(m.bars.filter(b=>b.time<Math.floor(Date.now()/60000)*60000))})),
  equity:s.equity.slice(-300),autopilot:s.autopilot,halted:s.halted,alerts:s.alerts,notifications:s.notifications,council:council(s,symbol),agentReports:s.agentReports,
  councilJob: councilJob?{running:councilJob.running,done:councilJob.done,total:councilJob.total}:null,status:status(),serverTime:Date.now()};}
async function refresh(){if(refreshing||s.feed!=='connected')return;refreshing=true;
  try{const results=await refreshMarkets(s);if(s.feed!=='connected')return;
    for(const r of results){const m=s.markets.find(x=>x.symbol===r.symbol);try{if(r.error)throw new Error(r.error);applyQuote(m,r.quote);}catch(e){m.status='unavailable';m.error=e.message;}}
    store.save();
  }finally{refreshing=false;lastRefresh=Date.now();}}
function respond(res,code,data,type='application/json'){
  res.writeHead(code,{'Content-Type':type,'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer',
    'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"});
  res.end(type==='application/json'?JSON.stringify(data):data);
}
async function body(req,max=65536){let size=0;const chunks=[];for await(const chunk of req){size+=chunk.length;if(size>max)throw new Error('Request body is too large.');chunks.push(chunk);}return Buffer.concat(chunks).toString('utf8');}
const json=async(req,max)=>{if(!req.headers['content-type']?.startsWith('application/json'))throw new Error('JSON content type required.');return JSON.parse(await body(req,max));};
async function chat(message,{useAI=false,phone=false}={}){
  if(typeof message!=='string'||message.length>2000)throw new Error('Message must be at most 2,000 characters.');
  const t=message.trim().toLowerCase().replace(/^jarvis[, ]*/,'');
  if(/^(stop|halt|pause)( trading| autopilot| everything)?[.!]?$/.test(t)){s.halted=true;s.autopilot=false;log(s,'risk','Emergency stop requested by voice or chat.');store.save();return {text:'Entries stopped and autopilot paused. Existing positions remain open; simulated stop monitoring continues.'};}
  if(/\b(buy|sell|trade|purchase|execute)\b/.test(t))return {text:'Use the paper order ticket to review the symbol and amount, then explicitly submit it. Voice and AI replies cannot execute trades.'};
  if(/brief|status|portfolio|balance|profit|how.*doing/.test(t))return {text:briefing(s)};
  if(/risk|limit|drawdown/.test(t))return {text:`Daily loss limit ${s.limits.dailyLossPct} percent. Position cap ${s.limits.maxPositionPct} percent. Combined meme cap ${s.limits.maxMemePct} percent. Maximum order ${s.limits.maxOrder} U.S. dollars. Current daily drawdown ${account(s).drawdownPct.toFixed(2)} percent.`};
  if(/scan|opportunit|market/.test(t)&&!useAI){const ready=s.markets.filter(m=>m.status!=='unavailable'&&m.indicators?.ready);const ranked=s.markets.map(m=>({symbol:m.symbol,...indicators(m.bars)})).filter(m=>m.ready).sort((a,b)=>b.score-a.score).slice(0,3);
    return {text:`${s.feed==='demo'?'Synthetic demo scan':'Observed-data scan'}: ${ranked.length?ranked.map(m=>`${m.symbol}, ${m.signal}, RSI ${m.rsi.toFixed(0)}`).join('; '):'Not enough observed bars yet'}. These rule signals have no validated predictive edge.`};}
  if(/time|date/.test(t))return {text:`It is ${new Date().toLocaleString('en-CA',{timeZone:s.notifications.timeZone})} in ${s.notifications.timeZone}.`};
  if(useAI&&!phone){if(chatBusy)throw new Error('An AI response is already running.');chatBusy=true;try{return {text:await askModel(message,{account:account(s),feed:s.feed,council:council(s)},null),engine:'Local AI'};}finally{chatBusy=false;}}
  return {text:'I can give a portfolio briefing, scan the watchlist, explain risk limits, or stop trading. Try “brief me”, “scan markets”, or “stop trading”. For open-ended conversation, connect a local Ollama model and enable Local AI.'};
}
async function voice(req,res,path){
  if(req.method!=='POST'||!phoneConfigured())return respond(res,403,'Unavailable','text/plain');
  const params=new URLSearchParams(await body(req));
  if(!validSignature(`${process.env.PUBLIC_VOICE_URL}${path}`,params,req.headers['x-twilio-signature'],process.env.TWILIO_AUTH_TOKEN))return respond(res,403,'Invalid signature','text/plain');
  const outbound=params.get('Direction')?.startsWith('outbound');
  const owner=outbound?params.get('To'):params.get('From'),other=outbound?params.get('From'):params.get('To');
  if(owner!==process.env.OWNER_PHONE_NUMBER||other!==process.env.TWILIO_FROM_NUMBER||params.get('AccountSid')!==process.env.TWILIO_ACCOUNT_SID)return respond(res,403,'Not allowed','text/plain');
  const sid=params.get('CallSid');if(!/^CA[a-fA-F0-9]{32}$/.test(sid||''))return respond(res,403,'Invalid call','text/plain');
  for(const [key,value] of calls)if(Date.now()-value.created>300000)calls.delete(key);
  let call=calls.get(sid);if(!call){call={created:Date.now(),tries:0,authorized:false};calls.set(sid,call);}
  if(path==='/voice/incoming')return respond(res,200,twiml('Jarvis. Enter your private PIN followed by the pound key.','/voice/auth',true),'text/xml');
  if(path==='/voice/auth'){
    call.tries++;
    if(call.tries>3)return respond(res,200,'<Response><Say>Access denied.</Say><Hangup/></Response>','text/xml');
    if(!same(params.get('Digits'),process.env.PHONE_PIN))return respond(res,200,twiml('Incorrect PIN. Try again, followed by the pound key.','/voice/auth',true),'text/xml');
    call.authorized=true;return respond(res,200,twiml(`${briefing(s)} Ask for a briefing, risk limits, or say stop trading.`),'text/xml');
  }
  if(path==='/voice/talk'&&call.authorized){
    const message=(params.get('SpeechResult')||'').slice(0,2000);
    if(process.env.OLLAMA_MODEL&&!/\b(brief|status|portfolio|balance|profit|risk|limit|stop|halt|pause|buy|sell|trade|execute|scan|market)\b/i.test(message)){
      if(call.pending)return respond(res,200,'<Response><Pause length="2"/><Redirect method="POST">/voice/result</Redirect></Response>','text/xml');
      call.pending=true;call.answer=null;
      askModel(message,{account:account(s),feed:s.feed,council:council(s)},null).then(answer=>{call.answer=answer;}).catch(()=>{call.answer='The local AI is unavailable. Ask for a briefing, risk limits, or stop trading.';}).finally(()=>{call.pending=false;});
      return respond(res,200,'<Response><Say>Let me review that.</Say><Pause length="2"/><Redirect method="POST">/voice/result</Redirect></Response>','text/xml');
    }
    const result=await chat(message,{phone:true});return respond(res,200,twiml(result.text),'text/xml');
  }
  if(path==='/voice/result'&&call.authorized){
    if(call.pending)return respond(res,200,'<Response><Pause length="2"/><Redirect method="POST">/voice/result</Redirect></Response>','text/xml');
    return respond(res,200,twiml(call.answer||'Ask for a briefing or risk limits.'),'text/xml');
  }
  return respond(res,403,'Not authorized','text/plain');
}
const server=http.createServer(async(req,res)=>{
  try{
    const url=new URL(req.url,origin),path=url.pathname;
    if(path.startsWith('/voice/'))return await voice(req,res,path);
    if(path==='/bridge/context'&&req.method==='GET'){
      if((process.env.BRIDGE_TOKEN||'').length<32||!same(req.headers.authorization,`Bearer ${process.env.BRIDGE_TOKEN}`))return respond(res,403,{error:'Bridge disabled or unauthorized.'});
      return respond(res,200,{mode:'paper',account:account(s),feed:s.feed,council:council(s)});
    }
    if(![`127.0.0.1:${port}`,`localhost:${port}`].includes(req.headers.host)||['cross-site','same-site'].includes(req.headers['sec-fetch-site']))return respond(res,403,{error:'Local same-origin access only.'});
    if(req.headers.origin&&![origin,`http://localhost:${port}`].includes(req.headers.origin))return respond(res,403,{error:'Origin rejected.'});
    if(path==='/api/session'&&req.method==='GET')return respond(res,200,{key:sessionKey});
    if(path.startsWith('/api/')){
      if(!same(req.headers['x-jarvis-key'],sessionKey))return respond(res,403,{error:'Session expired. Reload the dashboard.'});
      if(path==='/api/state'&&req.method==='GET')return respond(res,200,snapshot(url.searchParams.get('symbol')));
      if(path==='/api/orders.csv'&&req.method==='GET'){
        const keys=['time','symbol','side','qty','price','notional','fee','realized','actor','source'];
        const csv=[keys.join(','),...s.orders.map(o=>keys.map(k=>`"${String(o[k]??'').replaceAll('"','""')}"`).join(','))].join('\r\n');return respond(res,200,csv,'text/csv');}
      if(req.method!=='POST')return respond(res,405,{error:'Method not allowed.'});
      const data=await json(req,path==='/api/backtest'?8_500_000:65536);let result={ok:true};
      if(path==='/api/order')result=placeOrder(s,data);
      else if(path==='/api/control'){
        if(data.action==='halt'){s.halted=true;s.autopilot=false;log(s,'risk','Emergency stop activated. Positions remain open.');}
        else if(data.action==='resume'){if(account(s).drawdownPct>=s.limits.dailyLossPct)throw new Error('Daily loss circuit breaker still applies.');s.halted=false;log(s,'risk','Entry lock cleared manually. Autopilot remains paused.');}
        else if(data.action==='autopilot'){if(typeof data.enabled!=='boolean')throw new Error('Boolean enabled required.');if(data.enabled&&s.halted)throw new Error('Clear emergency stop first.');s.autopilot=data.enabled;log(s,'system',`Rule-based paper autopilot ${s.autopilot?'started':'paused'}.`);}
        else throw new Error('Unknown control action.');
      }
      else if(path==='/api/limits'){s.limits=validateLimits(data);log(s,'risk','Risk limits updated. Existing position stops retain their entry settings.');}
      else if(path==='/api/feed'){
        if(!['demo','connected'].includes(data.feed))throw new Error('Unknown feed.');
        if(s.positions.length)throw new Error('Close all simulated positions before changing data sources.');
        if(refreshing)throw new Error('A feed refresh is still running. Try again shortly.');
        s.feed=data.feed;s.autopilot=false;s.markets=makeState().markets;
        if(s.feed==='connected')for(const m of s.markets){m.bars=[];m.status='unavailable';m.source='Awaiting provider';m.asOf=0;}
        log(s,'system',`Feed changed to ${s.feed}. Price history cleared and autopilot paused.`);lastRefresh=0;
      }
      else if(path==='/api/refresh'){if(refreshing)throw new Error('Refresh already in progress.');if(Date.now()-lastRefresh<15000)throw new Error('Wait 15 seconds between provider refreshes.');await refresh();}
      else if(path==='/api/chat')result=await chat(data.message,{useAI:data.useAI===true});
      else if(path==='/api/alert'){
        if(s.alerts.length>=50)throw new Error('Maximum 50 alerts reached.');if(!s.markets.some(m=>m.symbol===data.symbol)||!['above','below'].includes(data.direction))throw new Error('Invalid alert.');
        const price=finite(data.price,Number.MIN_VALUE,1e9,'Alert price');s.alerts.push({id:randomUUID(),symbol:data.symbol,direction:data.direction,price,fired:null});
      }
      else if(path==='/api/alert/delete')s.alerts=s.alerts.filter(a=>a.id!==data.id);
      else if(path==='/api/notifications'){
        if(typeof data.onRisk!=='boolean'||typeof data.onAlert!=='boolean')throw new Error('Notification switches must be booleans.');
        if((data.onRisk||data.onAlert)&&!phoneConfigured())throw new Error('Configure and test Twilio before enabling automatic calls.');
        finite(data.dailyLimit,1,5,'Daily call limit');finite(data.quietStart,0,23,'Quiet start hour');finite(data.quietEnd,0,23,'Quiet end hour');
        if(![data.dailyLimit,data.quietStart,data.quietEnd].every(Number.isInteger))throw new Error('Use whole hours and whole call counts.');
        if(typeof data.timeZone!=='string'||data.timeZone.length>80)throw new Error('Invalid timezone.');
        new Intl.DateTimeFormat('en-CA',{timeZone:data.timeZone});
        Object.assign(s.notifications,{onRisk:data.onRisk,onAlert:data.onAlert,dailyLimit:data.dailyLimit,quietStart:data.quietStart,quietEnd:data.quietEnd,timeZone:data.timeZone});notificationCursor=Date.now();log(s,'system','Automatic owner-call preferences updated.');
      }
      else if(path==='/api/backtest'){result=data.csv?{...backtest({bars:parseCSV(data.csv),feeBps:data.feeBps??10,slippageBps:data.slippageBps??5}),source:'User-provided OHLCV; not independently verified'}:{...demoBacktest(),source:'Synthetic demo data — mechanics test only'};}
      else if(path==='/api/token'){
        if(Date.now()-lastResearch<5000)throw new Error('Wait five seconds between token lookups.');lastResearch=Date.now();result=await tokenResearch(data.chain,data.address);
      }
      else if(path==='/api/council'){
        if(!process.env.OLLAMA_MODEL)throw new Error('Configure a local Ollama model before running AI agents.');
        if(councilJob?.running)throw new Error('A council run is already in progress.');
        const selected=data.all===true?ROLES:ROLES.filter(r=>r.id===(data.role||'conductor'));
        if(!selected.length)throw new Error('Unknown analyst role.');
        const context={account:account(s),feed:s.feed,evidence:council(s,data.symbol)},job={running:true,done:0,total:selected.length};councilJob=job;s.agentReports=[];
        (async()=>{for(let n=0;n<selected.length;n+=2){await Promise.all(selected.slice(n,n+2).map(async role=>{
          try{s.agentReports.push({id:role.id,name:role.name,time:Date.now(),text:await askModel('Review your assigned evidence. Give a concise conclusion, limitations, and next verification step.',context,role),status:'complete'});}
          catch(e){s.agentReports.push({id:role.id,name:role.name,time:Date.now(),text:e.message,status:'error'});}job.done++;store.save();}));}job.running=false;store.save();})().catch(()=>{job.running=false;});result={started:true,count:selected.length};
      }
      else if(path==='/api/call'){
        if(data.confirm!==true)throw new Error('Confirm the call to your configured owner number.');
        if(callBusy||Date.now()-s.lastCall<60000)throw new Error('Calls are limited to one attempt per minute.');
        callBusy=true;s.lastCall=Date.now();store.save();try{result=await callOwner();log(s,'phone','Owner briefing call requested.');}finally{callBusy=false;}
      }
      else return respond(res,404,{error:'Unknown endpoint.'});
      store.save();return respond(res,200,result);
    }
    if(req.method!=='GET')return respond(res,405,{error:'Method not allowed.'});
    const files={'/':['index.html','text/html'],'/app.js':['app.js','text/javascript'],'/styles.css':['styles.css','text/css'],'/favicon.svg':['favicon.svg','image/svg+xml']};
    const target=files[path];if(!target)return respond(res,404,'Not found','text/plain');
    respond(res,200,await readFile(join(root,'public',target[0])),target[1]);
  }catch(e){respond(res,400,{error:e.message||'Request failed.'});}
});
async function notifyOwner(){
  const now=Date.now(),newEvents=s.events.filter(e=>e.time>notificationCursor);notificationCursor=now;
  const n=s.notifications,eligible=newEvents.some(e=>(n.onAlert&&e.type==='alert')||(n.onRisk&&e.type==='risk'&&/^(Emergency stop|Daily loss circuit breaker activated)/i.test(e.message)));
  if(!eligible||!phoneConfigured()||callBusy||now-s.lastCall<300000)return;
  const gate=canNotify(n,now);if(!gate.allowed){log(s,'phone','Automatic call skipped: quiet hours or daily attempt limit.');return;}
  callBusy=true;s.lastCall=now;if(n.day!==gate.day){n.day=gate.day;n.attempts=0;}n.attempts++;store.save();
  try{await callOwner();log(s,'phone','Automatic owner briefing call requested after an alert or stop.');}catch{log(s,'phone','Automatic call failed; check Twilio. No automatic retry.');}finally{callBusy=false;store.save();}
}
const timer=setInterval(()=>{try{cycle(s);store.save();notifyOwner().catch(()=>{});if(s.feed==='connected'&&Date.now()-lastRefresh>=30000)refresh().catch(()=>{});}catch(e){s.halted=true;s.autopilot=false;console.error('Engine halted:',e.message);}},3000);
server.listen(port,'127.0.0.1',()=>console.log(`Jarvis is ready at ${origin} — PAPER TRADING ONLY`));
function shutdown(){clearInterval(timer);s.autopilot=false;store.save();server.close(()=>{store.close();process.exit(0);});}
process.on('SIGINT',shutdown);process.on('SIGTERM',shutdown);
