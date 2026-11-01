import React, {useEffect, useState} from 'react';
import {LiveCrisis} from './LiveCrisis';

type Data = Record<string, any>;

async function request(path:string, body?:unknown):Promise<Data>{
  const response=await fetch('/api/v1/chaos'+path,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const data=await response.json();
  if(!response.ok)throw new Error(data.error?.code||JSON.stringify(data.detail||'Request failed'));
  return data;
}

export function CrisisInspector(){
  const [registry,setRegistry]=useState<Data>({}),[scenario,setScenario]=useState('CH-04');
  const [result,setResult]=useState<Data|null>(null),[stage,setStage]=useState(0);
  const [busy,setBusy]=useState(false),[error,setError]=useState('');
  useEffect(()=>{request('/scenarios').then(setRegistry).catch(e=>setError(String(e)));},[]);
  async function run(){
    setBusy(true);setError('');setResult(null);setStage(0);
    try{const created=await request(`/scenarios/${scenario}/runs`,{seed:20261030,count:10});setResult(await request(`/runs/${created.id}`));}
    catch(e){setError(String(e));}finally{setBusy(false);}
  }
  const steps=result?.stages||[];
  const selected=steps[stage];
  const stagePayment=[...steps.slice(0,stage+1)].reverse().map((s:Data)=>s.payment||s.status?.payment).find(Boolean);
  const worlds:Data[]=Object.values(result?.durable_financial_state||{});
  const audits:Data[]=Object.values(result?.audit||{});
  const payments=worlds.flatMap(w=>w.payments||[]);
  const obligations=worlds.flatMap(w=>w.correspondent_obligations||[]).filter(o=>o.status==='OPEN');
  const instantPending=payments.filter(p=>p.data?.source_final&&!p.data?.destination_final&&p.data?.obligation);
  return <section className="panel crisis-lab">
    <LiveCrisis/>
    <div className="eyebrow">P2-A / NATIVE SYNTHETIC EVIDENCE</div>
    <h2>Adversarial crisis laboratory</h2>
    <p>Select a supported fixture. Run executes its predeclared faults and permitted recovery against new PostgreSQL worlds. Steps below are recorded durable states; moving between them does not rerun settlement.</p>
    <div className="search"><label>Scenario<select aria-label="Chaos scenario" value={scenario} onChange={e=>setScenario(e.target.value)}>{Object.entries(registry).map(([id,v])=><option key={id} value={id}>{id} · {(v as Data).name}</option>)}</select></label><button className="primary" disabled={busy||!Object.keys(registry).length} onClick={run}>{busy?'Running and auditing…':'Run synthetic scenario'}</button></div>
    {error&&<div className="error" role="alert">{error}</div>}
    {result&&<>
      <p><b>Recorded run:</b> {result.id} · {result.manifest.scenario_id} · seed {result.manifest.seed} · policy {result.manifest.routing_policy}</p>
      <div className="search"><button disabled={stage===0} onClick={()=>setStage(stage-1)}>Previous recorded stage</button><span>{steps.length?`${stage+1} / ${steps.length}`:'Final state'}</span><button disabled={stage>=steps.length-1} onClick={()=>setStage(stage+1)}>Next recorded stage</button></div>
      {selected&&<div className="trace" role="status"><b>{selected.action}</b><span>Virtual {selected.at_ms} ms</span><span>{selected.payment?.economic_status||selected.status?.payment?.economic_status||''}</span></div>}
      {stagePayment&&<p role="status"><b>At selected stage:</b> {stagePayment.economic_status} · source final {String(stagePayment.source_final??'N/A')} · destination final {String(stagePayment.destination_final??'N/A')} · outstanding instant obligation {stagePayment.obligation?.amount_minor??0} minor units.</p>}
      <div className="kpis"><div><span>Final payments</span><strong>{payments.length}</strong></div><div><span>Final open correspondent obligations</span><strong>{obligations.length}</strong></div><div><span>Final source-only obligations</span><strong>{instantPending.length}</strong></div><div><span>Final independent audit</span><strong>{audits.every(a=>a.independent?.pass)?'PASS':'FAIL'}</strong></div></div>
      <h3>Final recorded payment state after all recovery</h3><table><thead><tr><th>Payment</th><th>Rail</th><th>Economic status</th><th>Source final</th><th>Destination final</th></tr></thead><tbody>{payments.map((p,i)=><tr key={i}><td>{p.id}</td><td>{p.data.rail}</td><td>{p.data.economic_status}</td><td>{String(p.data.source_final??'N/A')}</td><td>{String(p.data.destination_final??'N/A')}</td></tr>)}</tbody></table>
      <h3>Obligations and financial verification</h3><p>Open correspondent payables: {obligations.length}. Instant source-only obligations: {instantPending.length}. Final state is read from native payment and obligation tables.</p>
      <pre className="verification">{JSON.stringify({audit:audits.map(a=>({independent:a.independent,replay:a.replay,financial_errors:a.reconciliation.financial_errors})),selected_stage:selected},null,2)}</pre>
    </>}
  </section>;
}
