import { randomUUID } from 'node:crypto';

export const ASSETS = [
  ['BTC-USD','Bitcoin','crypto',67000], ['ETH-USD','Ethereum','crypto',3200],
  ['SOL-USD','Solana','crypto',145], ['DOGE-USD','Dogecoin','meme',.12],
  ['SHIB-USD','Shiba Inu','meme',.000018], ['PEPE-USD','Pepe','meme',.000008],
  ['BONK-USD','Bonk','meme',.000024], ['AVAX-USD','Avalanche','crypto',28],
  ['NVDA','NVIDIA','stock',130], ['AAPL','Apple','stock',220],
  ['MSFT','Microsoft','stock',420], ['TSLA','Tesla','stock',250],
  ['SPY','S&P 500 ETF','stock',560], ['QQQ','Nasdaq 100 ETF','stock',480]
].map(([symbol,name,kind,seed])=>({symbol,name,kind,seed,currency:'USD'}));
export const DEFAULT_LIMITS = { maxOrder:2500, maxPositionPct:15, maxExposurePct:60,
  maxMemePct:2, dailyLossPct:2, riskPerTradePct:.5, stopPct:2, takePct:4, feeBps:10, slippageBps:5 };
export const finite = (v,min,max,name) => {
  if(typeof v!=='number'||!Number.isFinite(v)||v<min||v>max) throw new Error(`${name} must be between ${min} and ${max}.`);
  return v;
};
export function seedBars(asset, count=180) {
  let seed=Array.from(asset.symbol).reduce((s,c)=>s+c.charCodeAt(0),29), price=asset.seed;
  const rand=()=>{seed=(1664525*seed+1013904223)>>>0;return seed/4294967296;};
  const start=Math.floor(Date.now()/60000)*60000-count*60000;
  return Array.from({length:count},(_,i)=>{const open=price;price*=1+(rand()-.48)*.006;
    return {time:start+i*60000,open,close:price,high:Math.max(open,price)*(1+rand()*.002),low:Math.min(open,price)*(1-rand()*.002),volume:Math.round(rand()*10000+1000)};
  });
}
export function makeState() {
  const markets=ASSETS.map(a=>{const bars=seedBars(a),price=bars.at(-1).close;return {...a,price,bid:price*.9995,ask:price*1.0005,asOf:Date.now(),source:'Synthetic demo',status:'demo',bars};});
  return {version:1,mode:'paper',feed:'demo',cash:100000,initialCash:100000,dayStart:100000,
    day:new Date().toISOString().slice(0,10),limits:{...DEFAULT_LIMITS},markets,positions:[],orders:[],events:[],
    equity:[],autopilot:false,halted:false,lastAuto:0,alerts:[],agentReports:[],lastCall:0,
    notifications:{onRisk:false,onAlert:false,dailyLimit:3,quietStart:22,quietEnd:8,timeZone:'America/Denver',day:'',attempts:0}};
}
export function account(s) {
  let exposure=0,unrealized=0,memeExposure=0;
  for(const p of s.positions){const m=s.markets.find(x=>x.symbol===p.symbol),value=p.qty*m.price;
    exposure+=value;unrealized+=value-p.cost;if(m.kind==='meme')memeExposure+=value;}
  const equity=s.cash+exposure;
  return {cash:s.cash,equity,exposure,unrealized,memeExposure,pnl:equity-s.initialCash,
    dailyPnl:equity-s.dayStart,drawdownPct:Math.max(0,(s.dayStart-equity)/s.dayStart*100),
    realized:s.orders.filter(o=>o.side==='sell').reduce((a,o)=>a+o.realized,0),
    fees:s.orders.reduce((a,o)=>a+o.fee,0),currency:'USD',valuation:'Last observed prices; unavailable quotes may be stale'};
}
export function log(s,type,message,extra={}) {
  const e={id:randomUUID(),time:Date.now(),type,message,...extra}; s.events.unshift(e);s.events=s.events.slice(0,1500);return e;
}
export function assertFresh(m,now=Date.now()) {
  if(!m||!Number.isFinite(m.price)||m.price<=0||!['demo','connected'].includes(m.status))throw new Error('Market data unavailable. No fill was simulated.');
  if(!Number.isFinite(m.asOf)||now-m.asOf>90000||m.asOf>now+5000) throw new Error('Quote is stale or timestamp is invalid. Refresh market data.');
}
export function validateLimits(input) {
  const next={}; const ranges={maxOrder:[10,25000],maxPositionPct:[1,25],maxExposurePct:[1,90],maxMemePct:[0,5],dailyLossPct:[.1,5],riskPerTradePct:[.1,2],stopPct:[.2,15],takePct:[.2,30],feeBps:[0,200],slippageBps:[0,500]};
  for(const [k,r] of Object.entries(ranges)){next[k]=finite(input[k],...r,k);}return next;
}
export function placeOrder(s,input,{now=Date.now(),actor='Manual'}={}) {
  if(s.mode!=='paper')throw new Error('This build supports paper trading only.');
  const {symbol,side,clientId}=input;
  if(typeof clientId!=='string'||clientId.length<8||clientId.length>100)throw new Error('An idempotency key of 8–100 characters is required.');
  const fingerprint=JSON.stringify([symbol,side,input.notional??null,input.qty??null]);
  const duplicate=s.orders.find(o=>o.clientId===clientId);
  if(duplicate){if(duplicate.fingerprint!==fingerprint)throw new Error('Idempotency key already used for a different order.');return duplicate;}
  if(!['buy','sell'].includes(side))throw new Error('Side must be buy or sell.');
  const m=s.markets.find(x=>x.symbol===symbol);assertFresh(m,now);
  const a=account(s),l=s.limits,p=s.positions.find(x=>x.symbol===symbol);
  const base=side==='buy'?m.ask:m.bid;
  if(!Number.isFinite(base)||base<=0)throw new Error('Executable quote unavailable.');
  const price=base*(1+(side==='buy'?1:-1)*l.slippageBps/10000);
  let qty,notional,fee,realized=0;
  if(side==='buy') {
    if(s.halted)throw new Error('Emergency stop is active. New entries are blocked.');
    if(a.drawdownPct>=l.dailyLossPct)throw new Error('Daily loss limit reached. New entries are blocked.');
    s.positions.forEach(pos=>assertFresh(s.markets.find(x=>x.symbol===pos.symbol),now));
    notional=finite(input.notional,1,l.maxOrder,'Order notional');qty=notional/price;fee=notional*l.feeBps/10000;
    if(notional+fee>s.cash)throw new Error('Insufficient simulated cash, including fees.');
    if((p?.qty||0)*m.price+notional>a.equity*l.maxPositionPct/100)throw new Error('Position concentration limit exceeded.');
    if(a.exposure+notional>a.equity*l.maxExposurePct/100)throw new Error('Portfolio exposure limit exceeded.');
    if(m.kind==='meme'&&a.memeExposure+notional>a.equity*l.maxMemePct/100)throw new Error('Combined meme-coin exposure limit exceeded.');
    const existingStopRisk=s.positions.filter(pos=>pos.symbol!==symbol).reduce((sum,pos)=>sum+pos.qty*Math.max(0,s.markets.find(x=>x.symbol===pos.symbol).price-pos.stop),0);
    const positionStopRisk=((p?.qty||0)*m.price+notional)*l.stopPct/100;
    if(existingStopRisk+positionStopRisk>a.equity*l.riskPerTradePct/100*5)throw new Error('Portfolio stop-risk budget exceeded.');
    if(positionStopRisk>a.equity*l.riskPerTradePct/100)throw new Error('Per-trade stop-risk budget exceeded.');
    s.cash-=notional+fee;
    if(p){const total=p.qty+qty;p.avg=(p.avg*p.qty+price*qty)/total;p.qty=total;p.cost+=notional+fee;p.stop=p.avg*(1-l.stopPct/100);p.take=p.avg*(1+l.takePct/100);}
    else s.positions.push({symbol,qty,avg:price,cost:notional+fee,stop:price*(1-l.stopPct/100),take:price*(1+l.takePct/100),opened:now});
  } else {
    if(!p)throw new Error('No position to close. Short selling is disabled.');
    qty=finite(input.qty??p.qty,Number.MIN_VALUE,p.qty,'Sell quantity');notional=qty*price;fee=notional*l.feeBps/10000;
    const cost=p.cost*qty/p.qty;realized=notional-fee-cost;s.cash+=notional-fee;p.qty-=qty;p.cost-=cost;
    if(p.qty<1e-12)s.positions=s.positions.filter(x=>x!==p);
  }
  const order={id:randomUUID(),clientId,fingerprint,time:now,symbol,side,qty,price,notional,fee,realized,actor,status:'Simulated fill',source:m.source,quoteTime:m.asOf};
  s.orders.unshift(order);log(s,'trade',`${actor}: ${side.toUpperCase()} ${symbol} · $${notional.toFixed(2)} simulated`,{orderId:order.id});return order;
}
export function indicators(bars) {
  if(bars.length<26)return {ready:false,signal:'WAIT',score:0,rsi:50,trend:0,momentum:0,volatility:0};
  const c=bars.map(b=>b.close),avg=n=>c.slice(-n).reduce((a,b)=>a+b,0)/n;
  let gains=0,losses=0;for(let i=c.length-14;i<c.length;i++){const d=c[i]-c[i-1];gains+=Math.max(0,d);losses+=Math.max(0,-d);}
  const rsi=gains+losses===0?50:losses===0?100:100-100/(1+gains/losses);
  const trend=(avg(5)/avg(20)-1)*100,momentum=(c.at(-1)/c.at(-11)-1)*100;
  const returns=c.slice(-21).slice(1).map((v,i)=>v/c[c.length-21+i]-1),mean=returns.reduce((a,b)=>a+b,0)/returns.length;
  const volatility=Math.sqrt(returns.reduce((a,b)=>a+(b-mean)**2,0)/returns.length)*100;
  const score=Math.max(-100,Math.min(100,Math.round(trend*80+momentum*20)));
  const signal=trend>.07&&rsi>45&&rsi<72?'BUY':trend<-.06||rsi>82?'EXIT':'WAIT';
  return {ready:true,signal,score,rsi,trend,momentum,volatility,fast:avg(5),slow:avg(20)};
}
export function cycle(s,now=Date.now()) {
  const day=new Date(now).toISOString().slice(0,10);
  if(day!==s.day){s.day=day;s.dayStart=account(s).equity;log(s,'system','UTC trading day rolled over. Daily baseline updated.');}
  if(s.feed==='demo')for(const m of s.markets){const previous=m.price;m.price*=1+(Math.random()-.5)*.0015;m.bid=m.price*.9995;m.ask=m.price*1.0005;m.asOf=now;m.status='demo';
    const bucket=Math.floor(now/60000)*60000,last=m.bars.at(-1);
    if(last?.time===bucket){last.close=m.price;last.high=Math.max(last.high,m.price);last.low=Math.min(last.low,m.price);last.volume+=10;}
    else m.bars.push({time:bucket,open:previous,close:m.price,high:Math.max(previous,m.price),low:Math.min(previous,m.price),volume:10});m.bars=m.bars.slice(-600);}
  for(const p of [...s.positions]){
    const m=s.markets.find(x=>x.symbol===p.symbol);
    if(m.price<=p.stop||m.price>=p.take){try{placeOrder(s,{symbol:p.symbol,side:'sell',clientId:randomUUID()},{actor:m.price<=p.stop?'Stop loss':'Take profit',now});}catch{/* Stale quotes cannot fill stops. UI retains position and status. */}}
  }
  if(account(s).drawdownPct>=s.limits.dailyLossPct&&!s.halted){s.halted=true;s.autopilot=false;log(s,'risk','Daily loss circuit breaker activated. Entries stopped; positions remain open.');}
  if(s.autopilot&&!s.halted&&now-s.lastAuto>=60000){s.lastAuto=now;
    for(const m of s.markets){const i=indicators(m.bars.filter(b=>b.time<Math.floor(now/60000)*60000)),p=s.positions.find(x=>x.symbol===m.symbol);
      try{if(p&&i.signal==='EXIT')placeOrder(s,{symbol:m.symbol,side:'sell',clientId:randomUUID()},{actor:'Trend autopilot',now});
        if(!p&&i.signal==='BUY'){placeOrder(s,{symbol:m.symbol,side:'buy',notional:Math.min(500,s.limits.maxOrder),clientId:randomUUID()},{actor:'Trend autopilot',now});break;}}
      catch(e){log(s,'blocked',`${m.symbol}: ${e.message}`);}}
  }
  for(const alert of s.alerts){if(alert.fired)continue;const m=s.markets.find(x=>x.symbol===alert.symbol);
    try{assertFresh(m,now);}catch{continue;}
    if(alert.direction==='above'?m.price>=alert.price:m.price<=alert.price){alert.fired=now;log(s,'alert',`${alert.symbol} crossed ${alert.direction} $${alert.price}.`);}}
  if(!s.equity.length||now-s.equity.at(-1).time>=15000){s.equity.push({time:now,value:account(s).equity});s.equity=s.equity.slice(-2500);}
}
