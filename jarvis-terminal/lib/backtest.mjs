import { indicators, seedBars, ASSETS, finite } from './core.mjs';

export function validateBars(bars) {
  if(!Array.isArray(bars)||bars.length<60||bars.length>50000)throw new Error('Provide 60–50,000 OHLCV bars.');
  let previous=-Infinity;const gaps=[];
  for(const [index,b] of bars.entries()) {
    for(const k of ['time','open','high','low','close','volume'])if(typeof b[k]!=='number'||!Number.isFinite(b[k]))throw new Error(`Bar ${index+1}: ${k} must be finite.`);
    if(b.time<=previous)throw new Error(`Bar ${index+1}: duplicate or out-of-order timestamp.`);
    if(b.time>Date.now()+60000)throw new Error(`Bar ${index+1}: future timestamp.`);
    if(Math.min(b.open,b.low,b.high,b.close)<=0||b.volume<0||b.low>Math.min(b.open,b.close)||b.high<Math.max(b.open,b.close)||b.low>b.high)throw new Error(`Bar ${index+1}: invalid OHLCV range.`);
    if(previous>0)gaps.push(b.time-previous);previous=b.time;
  }
  const sorted=[...gaps].sort((a,b)=>a-b),interval=sorted[Math.floor(sorted.length/2)];
  return {rows:bars.length,first:bars[0].time,last:bars.at(-1).time,medianIntervalMs:interval,largeGaps:gaps.filter(g=>g>interval*1.5).length,
    warnings:['Source accuracy and corporate-action adjustments are not independently verified.','Gaps may represent missing data or market closures.']};
}
export function parseCSV(csv) {
  if(typeof csv!=='string'||csv.length>8_000_000)throw new Error('CSV exceeds 8 MB.');
  const lines=csv.trim().replace(/^\uFEFF/,'').split(/\r?\n/),headers=lines.shift().toLowerCase().split(',').map(x=>x.trim());
  if(headers.join(',')!=='time,open,high,low,close,volume')throw new Error('CSV header must be time,open,high,low,close,volume. Use ISO timestamps or epoch milliseconds.');
  const bars=lines.map((line,i)=>{const a=line.split(',').map(v=>v.trim());if(a.length!==6||a.some(v=>v===''))throw new Error(`CSV row ${i+2}: six nonempty columns required.`);
    const time=/^\d{13}$/.test(a[0])?Number(a[0]):/^\d{4}-\d\d-\d\dT.*(?:Z|[+-]\d\d:\d\d)$/.test(a[0])?Date.parse(a[0]):NaN;
    return {time,open:Number(a[1]),high:Number(a[2]),low:Number(a[3]),close:Number(a[4]),volume:Number(a[5])};});
  validateBars(bars);return bars;
}
export function backtest({bars,feeBps=10,slippageBps=5,stopPct=2,takePct=4,testFraction=.3},signal=history=>indicators(history).signal) {
  const quality=validateBars(bars);finite(feeBps,0,200,'Fees');finite(slippageBps,0,500,'Slippage');finite(stopPct,.2,15,'Stop');finite(takePct,.2,30,'Target');finite(testFraction,.1,.9,'Evaluation fraction');
  let cash=100000,position=null,peak=cash,maxDrawdown=0;const trades=[],curve=[];
  const start=Math.max(26,Math.floor(bars.length*(1-testFraction))),fee=feeBps/10000,slip=slippageBps/10000;
  const close=(price,time,reason)=>{price*=1-slip;const proceeds=position.qty*price*(1-fee),pnl=proceeds-position.cost;cash+=proceeds;
    trades.push({entryTime:position.time,exitTime:time,entry:position.entry,exit:price,pnl,reason});position=null;};
  for(let i=start;i<bars.length;i++) {
    const b=bars[i],decision=signal(bars.slice(Math.max(0,i-200),i)); // Only completed prior bars.
    if(position&&decision==='EXIT')close(b.open,b.time,'Signal at next open');
    if(!position&&decision==='BUY'){const budget=cash*.1,price=b.open*(1+slip),qty=budget/(price*(1+fee));cash-=budget;
      position={qty,cost:budget,entry:price,stop:price*(1-stopPct/100),take:price*(1+takePct/100),time:b.time};}
    if(position){if(b.open<=position.stop)close(b.open,b.time,'Gap through stop');
      else if(b.low<=position.stop)close(position.stop,b.time,'Stop (first if both touched)');
      else if(b.high>=position.take)close(position.take,b.time,'Target');}
    const equity=cash+(position?position.qty*b.close:0);peak=Math.max(peak,equity);maxDrawdown=Math.max(maxDrawdown,(peak-equity)/peak*100);curve.push({time:b.time,value:equity});
  }
  if(position){close(bars.at(-1).close,bars.at(-1).time,'End of evaluation');curve.at(-1).value=cash;maxDrawdown=Math.max(maxDrawdown,(peak-cash)/peak*100);}
  const wins=trades.filter(t=>t.pnl>0),losses=trades.filter(t=>t.pnl<0),grossLoss=-losses.reduce((a,b)=>a+b.pnl,0);
  const buyHoldQty=100000/(bars[start].open*(1+slip)*(1+fee));
  return {quality,startingEquity:100000,endingEquity:cash,returnPct:(cash/100000-1)*100,maxDrawdown,
    winRate:trades.length?wins.length/trades.length*100:null,profitFactor:grossLoss?wins.reduce((a,b)=>a+b.pnl,0)/grossLoss:null,
    buyHoldPct:(buyHoldQty*bars.at(-1).close*(1-slip)*(1-fee)/100000-1)*100,tradeCount:trades.length,trades,curve,
    evaluationStart:bars[start].time,evaluationBars:bars.length-start,warmupBars:start,
    assumptions:'Fixed SMA/RSI strategy; final 30% by default; no parameter fitting. Signals use completed bars, fills use next open. 10% cash per entry; fees and adverse slippage both sides. Pessimistic stop-first OHLC ordering. No market impact, partial fills, taxes, FX, or borrow costs. This is a research simulator, not performance validation.'};
}
export const demoBacktest=()=>backtest({bars:seedBars(ASSETS[0],600)});
