"""Makes demo files so you can try everything. These are FAKE paper trades, for testing the plumbing only."""
import csv, json, random, subprocess, sys
random.seed(3)
bots = ["A073","A081","A087","A001","A002"]
rows=[]
for d in range(1,15):
    for _ in range(random.randint(0,3)):
        b=random.choice(bots); r=round(random.gauss(-0.05,1.0),2)
        rows.append({"date":f"2026-10-{d:02d}","bot":b,"symbol":"TEST","pnl":round(r*25,2),"r":r,"cost_r":0.12,"status":"PAPER"})
with open("demo/paper_log.csv","w",newline="") as f:
    w=csv.DictWriter(f,fieldnames=["date","bot","symbol","pnl","r","cost_r","status"]); w.writeheader(); w.writerows(rows)
fleet=[{"bot_id":b,"symbol":"TEST","status":"MEME SHADOW" if b=="A002" else "RETRAIN","verdict":"REJECT","cost_r_avg":0.12,
        "slippage_vs_model":1.1,"max_drawdown_pct":-3.0,"backtest_max_dd_pct":-4.0,"consecutive_losses":0,"heartbeat_age_s":30,"data_age_s":60} for b in bots]
json.dump(fleet,open("demo/fleet.json","w"),indent=2)
subprocess.run([sys.executable,"jarvus_reporter.py","demo/paper_log.csv","--json","demo/report.json"],check=True)
