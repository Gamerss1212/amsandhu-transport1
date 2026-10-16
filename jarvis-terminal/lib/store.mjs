import { DatabaseSync } from 'node:sqlite';
import { mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { makeState,log } from './core.mjs';

export function openStore(directory) {
  mkdirSync(directory,{recursive:true});const db=new DatabaseSync(join(directory,'jarvis.sqlite'));
  db.exec('PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; CREATE TABLE IF NOT EXISTS state(id INTEGER PRIMARY KEY CHECK(id=1), json TEXT NOT NULL);');
  const row=db.prepare('SELECT json FROM state WHERE id=1').get();
  const state=row?JSON.parse(row.json):makeState();
  if(state.version!==1)throw new Error('Unsupported state version. Preserve the database before migrating.');
  state.autopilot=false; // Deliberate restart interlock.
  log(state,'system','Jarvis started. Paper autopilot is paused after restart.');
  const save=()=>db.prepare('INSERT INTO state(id,json) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET json=excluded.json').run(JSON.stringify(state));
  save();return {state,save,close:()=>db.close()};
}
