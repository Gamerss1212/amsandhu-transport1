import { createHmac, timingSafeEqual } from 'node:crypto';
import { account } from './core.mjs';

const getJSON=async(url,headers={})=>{const r=await fetch(url,{headers,signal:AbortSignal.timeout(8000)});if(!r.ok)throw new Error(`Provider HTTP ${r.status}`);return r.json();};
export async function fetchQuote(m,env=process.env) {
  if(m.kind==='stock'){
    if(!env.ALPACA_API_KEY||!env.ALPACA_SECRET_KEY)throw new Error('Stock data key required');
    const d=await getJSON(`https://data.alpaca.markets/v2/stocks/${encodeURIComponent(m.symbol)}/quotes/latest?feed=iex`,{'APCA-API-KEY-ID':env.ALPACA_API_KEY,'APCA-API-SECRET-KEY':env.ALPACA_SECRET_KEY});
    const q=d.quote;return {price:(q.ap+q.bp)/2,ask:q.ap,bid:q.bp,asOf:Date.parse(q.t),source:'Alpaca IEX quote (partial market)'};
  }
  const q=await getJSON(`https://api.exchange.coinbase.com/products/${encodeURIComponent(m.symbol)}/ticker`,{'User-Agent':'JarvisResearch/0.1'});
  return {price:Number(q.price),bid:Number(q.bid),ask:Number(q.ask),asOf:Date.parse(q.time),source:'Coinbase Exchange ticker'};
}
export function applyQuote(m,q) {
  if(![q.price,q.bid,q.ask,q.asOf].every(Number.isFinite)||Math.min(q.price,q.bid,q.ask)<=0||q.bid>q.ask||q.asOf>Date.now()+5000)throw new Error('Invalid provider quote');
  if(q.asOf<m.asOf&&m.status==='connected')throw new Error('Out-of-order provider quote');
  Object.assign(m,q,{status:'connected',error:null});
  const time=Math.floor(q.asOf/60000)*60000,last=m.bars.at(-1);
  if(last?.time===time){last.close=q.price;last.high=Math.max(last.high,q.price);last.low=Math.min(last.low,q.price);}
  else if(!last||time>last.time)m.bars.push({time,open:q.price,high:q.price,low:q.price,close:q.price,volume:0});
  m.bars=m.bars.slice(-600);
}
export async function refreshMarkets(s,env=process.env) {
  // At most two requests at a time to avoid provider bursts.
  const results=[];
  for(let i=0;i<s.markets.length;i+=2)results.push(...await Promise.all(s.markets.slice(i,i+2).map(async m=>{
    try{return {symbol:m.symbol,quote:await fetchQuote(m,env)};}catch(e){return {symbol:m.symbol,error:e.message};}
  })));
  return results;
}
export async function tokenResearch(chain,address) {
  if(!['solana','ethereum','base','bsc'].includes(chain))throw new Error('Select a supported chain.');
  if(!/^[a-zA-Z0-9]{20,64}$/.test(address))throw new Error('Enter an exact token contract address.');
  const pairs=await getJSON(`https://api.dexscreener.com/token-pairs/v1/${chain}/${encodeURIComponent(address)}`);
  if(!Array.isArray(pairs))throw new Error('Unexpected DEX Screener response.');
  return {chain,address,retrievedAt:Date.now(),source:'DEX Screener',tradeable:false,
    missingChecks:['Sell simulation / honeypot','Mint and freeze authority','Holder concentration','LP ownership and lock','Contract audit'],
    pairs:pairs.slice(0,12).map(p=>({dex:p.dexId,pair:p.pairAddress,symbol:p.baseToken?.symbol,url:typeof p.url==='string'&&p.url.startsWith('https://dexscreener.com/')?p.url:null,
      priceUsd:Number(p.priceUsd)||null,liquidityUsd:p.liquidity?.usd??null,volume24h:p.volume?.h24??null,createdAt:p.pairCreatedAt??null}))};
}
export const escapeXML=s=>String(s).replace(/[<>&'\"]/g,c=>({'<':'&lt;','>':'&gt;','&':'&amp;',"'":'&apos;','"':'&quot;'}[c]));
export function validSignature(url,params,signature,token) {
  if(!signature||!token)return false;
  let payload=url;for(const key of [...new Set(params.keys())].sort())for(const value of [...new Set(params.getAll(key))].sort())payload+=key+value;
  const expected=createHmac('sha1',token).update(payload).digest('base64'),a=Buffer.from(expected),b=Buffer.from(signature);
  return a.length===b.length&&timingSafeEqual(a,b);
}
export function phoneConfigured(env=process.env) {
  return Boolean(/^AC[a-fA-F0-9]{32}$/.test(env.TWILIO_ACCOUNT_SID||'')&&env.TWILIO_AUTH_TOKEN&&
    /^\+[1-9]\d{7,14}$/.test(env.TWILIO_FROM_NUMBER||'')&&/^\+[1-9]\d{7,14}$/.test(env.OWNER_PHONE_NUMBER||'')&&
    /^\d{6,12}$/.test(env.PHONE_PIN||'')&&/^https:\/\/[^/]+$/.test(env.PUBLIC_VOICE_URL||''));
}
export function canNotify(settings,now=Date.now()) {
  const parts=new Intl.DateTimeFormat('en-CA',{timeZone:settings.timeZone,hour:'2-digit',hourCycle:'h23',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(new Date(now));
  const get=k=>parts.find(p=>p.type===k).value,hour=Number(get('hour')),day=`${get('year')}-${get('month')}-${get('day')}`;
  const quiet=settings.quietStart===settings.quietEnd?false:settings.quietStart<settings.quietEnd?hour>=settings.quietStart&&hour<settings.quietEnd:hour>=settings.quietStart||hour<settings.quietEnd;
  return {allowed:!quiet&&(settings.day!==day||settings.attempts<settings.dailyLimit),day};
}
export function briefing(s) {
  const a=account(s);return `Jarvis online. This is your simulated trading account, in U.S. dollars. Equity ${a.equity.toFixed(2)} dollars. Daily profit or loss ${a.dailyPnl.toFixed(2)} dollars. ${s.positions.length} open positions. ${s.halted?'Emergency stop is active.':s.autopilot?'Rule based paper autopilot is running.':'Autopilot is paused.'} Data is ${s.feed==='demo'?'synthetic demo data':'from external feeds; stale or unavailable quotes cannot be traded'}.`;
}
export function twiml(message,action='/voice/talk',pin=false) {
  return `<?xml version="1.0" encoding="UTF-8"?><Response><Gather input="${pin?'dtmf':'speech'}" ${pin?'finishOnKey="#"':'speechTimeout="auto"'} timeout="6" method="POST" action="${escapeXML(action)}"><Say>${escapeXML(message)}</Say></Gather><Say>Goodbye.</Say><Hangup/></Response>`;
}
export async function callOwner(env=process.env) {
  if(!phoneConfigured(env))throw new Error('Configure Twilio, owner number, private PIN and HTTPS voice URL first.');
  const body=new URLSearchParams({To:env.OWNER_PHONE_NUMBER,From:env.TWILIO_FROM_NUMBER,Url:`${env.PUBLIC_VOICE_URL}/voice/incoming`,Method:'POST',TimeLimit:'300'});
  const r=await fetch(`https://api.twilio.com/2010-04-01/Accounts/${env.TWILIO_ACCOUNT_SID}/Calls.json`,{method:'POST',signal:AbortSignal.timeout(15000),headers:{Authorization:`Basic ${Buffer.from(`${env.TWILIO_ACCOUNT_SID}:${env.TWILIO_AUTH_TOKEN}`).toString('base64')}`,'Content-Type':'application/x-www-form-urlencoded'},body});
  if(!r.ok)throw new Error(`Phone provider returned HTTP ${r.status}; check your Twilio console before retrying.`);
  const d=await r.json();return {sid:d.sid,status:d.status};
}
