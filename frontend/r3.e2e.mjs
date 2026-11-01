import {chromium} from '@playwright/test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
const base=process.env.RAILSTORM_URL||'http://127.0.0.1:8859';
const browser=await chromium.launch({headless:true});
const page=await browser.newPage({viewport:{width:1600,height:1100},reducedMotion:'reduce'});
const errors=[],consoleErrors=[],records=[];
page.on('pageerror',e=>errors.push(e.message));
page.on('console',m=>{if(m.type()==='error')consoleErrors.push(m.text());});
await page.goto(base);
await page.getByRole('button',{name:/Crisis lab/}).click();
let root=page.getByTestId('live-crisis');
async function click(name){const response=page.waitForResponse(r=>r.url().includes('/live-crisis/sessions')&&r.request().method()==='POST');await root.getByRole('button',{name,exact:true}).click();const r=await response;await root.getByRole('button',{name:'Initialize live scenario',exact:true}).waitFor({state:'visible'});await page.waitForFunction(()=>!document.querySelector('[data-testid="live-crisis"] button')?.disabled);return r;}
async function detail(){const id=await root.getByTestId('live-session').textContent();const r=await page.request.get(base+'/api/v1/live-crisis/sessions/'+id);assert(r.ok());return r.json();}
async function initialize(scenario){await root.getByLabel('Live scenario').selectOption(scenario);assert((await click('Initialize live scenario')).ok());return detail();}
async function action(name){const r=await click(name);assert(r.ok(),await r.text());return detail();}
async function verify(){let d=await action('Run financial audit');assert(d.session.audit.independent.pass);assert.equal(d.session.audit.native.financial_errors,0);await root.getByTestId('live-audit').filter({hasText:'PASS'}).waitFor();d=await action('Replay durable history');assert(d.session.replay.match);await root.getByTestId('live-replay').filter({hasText:'PASS'}).waitFor();return d;}
for(const scenario of ['atomic','correspondent','instant','multicurrency','expired','shortage']){
 await initialize(scenario);let d;
 for(let i=0;i<20;i++){d=await action('Advance one step');const p=d.financial.payments.payment;if(['SETTLED','REJECTED','EXPIRED'].includes(p.economic_status))break;}
 const expected=scenario==='expired'?'EXPIRED':scenario==='shortage'?'REJECTED':'SETTLED';
 assert.equal(d.financial.payments.payment.economic_status,expected);
 if(scenario==='multicurrency'){assert.equal(d.financial.payments.payment.quote.target,'EUR');assert.equal(d.financial.payments.payment.quote.output_minor,9200);assert.equal(d.route_decision.bound_rail,'ATOMIC_PVP');}
 d=await verify();records.push({scenario,expected,observed:d});
}
await initialize('instant');await action('Advance one step');await action('Inject destination outage');await action('Advance one step');let d=await action('Advance one step');
assert(d.financial.payments.payment.source_final&&!d.financial.payments.payment.destination_final);assert.equal(d.financial.payments.payment.obligation.amount_minor,13300);
await root.getByTestId('live-obligation').filter({hasText:'13300'}).waitFor();await page.screenshot({path:'../docs/screenshots/r3-live-pending.png',fullPage:true});records.push({scenario:'outage-pending',observed:d});
await page.reload();await page.getByRole('button',{name:/Crisis lab/}).click();root=page.getByTestId('live-crisis');await root.getByTestId('live-obligation').filter({hasText:'13300'}).waitFor();
await action('Restore participant');await action('Recover payment');await action('Retry payment');d=await verify();assert.equal(d.financial.positions['MERIDIAN_SG:SGD'].posted,13300);assert.equal(Object.values(d.financial.journals).filter(j=>j.payment_id==='payment').length,2);records.push({scenario:'restored-recovered-duplicate',observed:d});await page.screenshot({path:'../docs/screenshots/r3-live-recovered.png',fullPage:true});
await initialize('instant');await action('Advance one step');await action('Inject lost acknowledgment');await action('Advance one step');d=await action('Advance one step');assert(Object.values(d.financial.instant_messages).some(m=>m.ack_status==='LOST'));await action('Recover payment');d=await verify();assert.equal(d.financial.positions['MERIDIAN_SG:SGD'].posted,13300);records.push({scenario:'lost-ack-recovery',observed:d});
await initialize('instant');const invalid=await click('Restore participant');assert.equal(invalid.status(),409);await root.getByTestId('live-error').waitFor();d=await detail();assert.equal(Object.keys(d.financial.payments).length,0);records.push({scenario:'invalid-transition',status:409,observed:d});
assert.deepEqual(errors,[]);
fs.writeFileSync('../docs/THUNDERBOLT_R3_browser.json',JSON.stringify({records,pageErrors:errors,consoleErrors,scope:'BR01–BR15 via live native sessions; expected409 console entry retained'},null,2));
await browser.close();console.log('R3 live browser acceptance passed',records.length,'workflows; zero page runtime errors');
