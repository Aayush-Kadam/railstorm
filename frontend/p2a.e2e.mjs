import {chromium} from '@playwright/test';
import assert from 'node:assert/strict';
import fs from 'node:fs';

const base=process.env.RAILSTORM_URL||'http://127.0.0.1:8851';
const browser=await chromium.launch({headless:true});
const page=await browser.newPage({viewport:{width:1600,height:1200},reducedMotion:'reduce'});
const errors=[];
page.on('pageerror',e=>errors.push(e.message));
await page.goto(base);
await page.getByRole('button',{name:/Crisis lab/}).click();
const observed=[];
for(const scenario of (process.env.P2A_SCENARIOS?.split(',')||['CH-01','CH-02','CH-03','CH-04','CH-05','CH-06','CH-11'])){
  await page.getByLabel('Chaos scenario').selectOption(scenario);
  await page.getByRole('button',{name:'Run synthetic scenario'}).click();
  await page.getByText('Recorded run:').waitFor({timeout:120000});
  const text=await page.locator('.crisis-lab').textContent();
  assert(text.includes('PASS'));
  assert(!text.includes('FAIL'));
  const runId=(await page.locator('.crisis-lab p').filter({hasText:'Recorded run:'}).textContent()).match(/[0-9a-f]{32}/)?.[0];
  assert(runId);
  const artifact=await page.request.get(`${base}/api/v1/chaos/runs/${runId}`);
  assert(artifact.ok());
  const result=await artifact.json();
  assert(Object.values(result.audit).every(a=>a.independent.pass&&a.replay.match&&a.reconciliation.financial_errors===0));
  if(scenario==='CH-04'){
    assert(result.stages.some(s=>s.action==='pending_audit'&&s.audit.independent.pass));
    assert(result.final.payment.destination_final);
    await page.getByRole('button',{name:'Next recorded stage'}).click();
    await page.getByText('2 / 6').waitFor();
    await page.getByRole('button',{name:'Next recorded stage'}).click();
    await page.getByText('3 / 6').waitFor();
    const stageText=await page.locator('.crisis-lab p').filter({hasText:'At selected stage:'}).textContent();
    assert(stageText.includes('source final true · destination final false'));
    await page.screenshot({path:'../docs/screenshots/p2-a-instant-outage.png',fullPage:true});
  }
  if(scenario==='CH-03')await page.screenshot({path:'../docs/screenshots/p2-a-correspondent-outage.png',fullPage:true});
  observed.push({scenario,runId,independentAudits:Object.keys(result.audit).length,stages:result.stages.length});
}
assert.deepEqual(errors,[]);
fs.writeFileSync('../docs/P2_A_browser_observed.json',JSON.stringify({observed,pageErrors:errors},null,2));
await browser.close();
console.log('P2-A browser verified',observed.length,'scenario workflows, zero runtime errors');
