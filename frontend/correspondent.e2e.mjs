import {chromium} from '@playwright/test';
import assert from 'node:assert/strict';
import fs from 'node:fs';

const browser=await chromium.launch({headless:true});
const page=await browser.newPage({viewport:{width:1600,height:1200},reducedMotion:'reduce'});
const errors=[];page.on('pageerror',e=>errors.push(e.message));
const base=process.env.RAILSTORM_URL||'http://127.0.0.1:8840';
await page.goto(base);
async function create(preset){
 await page.getByRole('button',{name:/Scenarios/}).click();
 await page.getByLabel('Preset',{exact:true}).selectOption(preset);
 await page.getByRole('button',{name:'Validate & create run'}).click();
 await page.waitForFunction(()=>document.querySelector('.status')?.textContent==='READY');
 const run=await page.getByLabel('Persisted run').inputValue();
 await page.getByRole('button',{name:'Run',exact:true}).click();
 await page.waitForFunction(()=>document.querySelector('.status')?.textContent==='COMPLETED');
 await page.getByRole('button',{name:'p000000 ↗'}).click();
 await page.getByText('Correspondent payment chain · independent hop commits').waitFor();
 return run;
}
async function detail(run){return (await page.request.get(base+`/api/v1/runs/${run}/payments/p000000`)).json();}
const healthy=await create('cb_healthy');
let d=await detail(healthy);
assert(d.chain.end_to_end_completed);assert.equal(d.hops.length,3);assert.equal(d.journal.length,15);
assert.equal(d.payment.beneficiary_minor,9825);
await page.screenshot({path:'../docs/screenshots/p1a-healthy-chain.png',fullPage:true});
await page.getByRole('button',{name:/Audit/}).click();
await page.getByRole('button',{name:'Verify replay'}).click();
await page.waitForFunction(()=>document.querySelector('.verification')?.textContent?.includes('"match": true'));
const exposed=await create('cb_exposure');
d=await detail(exposed);assert.equal(d.payment.economic_status,'PENDING_EXPOSURE');
assert.deepEqual(d.chain.pending_exposure_minor,{USD:9900});
const firstJournal=d.journal.filter(j=>j.id.includes(':hop:0:'));
await page.getByText('Pending exposure: {"USD":9900} · currencies kept separate').waitFor();
await page.screenshot({path:'../docs/screenshots/p1a-pending-exposure.png',fullPage:true});
// The scheduled FAULT_OFF is already committed; the queue intentionally ends
// before another tick, so recovery is an explicit user action.
for(let i=0;i<4;i++){
 await page.getByRole('button',{name:'Advance / recover next stage'}).click();
 await page.waitForTimeout(150);
}
d=await detail(exposed);assert(d.chain.end_to_end_completed);assert.deepEqual(d.chain.pending_exposure_minor,{});
assert.deepEqual(d.journal.filter(j=>j.id.includes(':hop:0:')),firstJournal);
await page.screenshot({path:'../docs/screenshots/p1a-forward-recovery.png',fullPage:true});
await page.getByRole('button',{name:/Network/}).click();
await page.getByRole('heading',{name:'Correspondent chain',exact:true}).waitFor();
await page.screenshot({path:'../docs/screenshots/p1a-network.png',fullPage:true});
const replay=await (await page.request.post(base+`/api/v1/runs/${exposed}/replay-check`,{data:{}})).json();
const audit=await (await page.request.post(base+`/api/v1/runs/${exposed}/reconcile`,{data:{}})).json();
assert(replay.match);assert.equal(audit.financial_errors,0);assert.deepEqual(errors,[]);
fs.writeFileSync('../docs/p1a-browser-observed.json',JSON.stringify({healthy,exposed,replay,audit,errors,checks:['healthy chain','journal inspector','downstream outage','open payable','scheduled restoration','explicit forward recovery','original posting retained','network path','replay']},null,2));
await browser.close();console.log('Correspondent browser verification passed');
