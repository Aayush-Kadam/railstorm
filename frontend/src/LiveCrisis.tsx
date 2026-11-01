import React,{useEffect,useState} from 'react';
type Data=Record<string,any>;
async function api(path:string,body?:unknown){const r=await fetch('/api/v1/live-crisis'+path,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const d=await r.json();if(!r.ok)throw Error(d.error?.code||JSON.stringify(d.detail||d));return d;}
export function LiveCrisis(){
 const [registry,setRegistry]=useState<Data>({}),[scenario,setScenario]=useState('instant'),[data,setData]=useState<Data|null>(null),[busy,setBusy]=useState(false),[error,setError]=useState('');
 useEffect(()=>{api('/scenarios').then(setRegistry).catch(e=>setError(String(e)));const id=localStorage.getItem('railstorm.live');if(id)api('/sessions/'+id).then(setData).catch(e=>setError(String(e)));},[]);
 async function act(action:string){setBusy(true);setError('');try{const d=action==='INITIALIZE'?await api('/sessions',{scenario,seed:20261030}):await api('/sessions/'+data?.session.id+'/actions',{action});setData(d);localStorage.setItem('railstorm.live',d.session.id);}catch(e){setError(String(e));}finally{setBusy(false);}}
 const payment=data?.financial.payments.payment;
 const audit=data?.session.audit;
 return <section className="live-crisis" data-testid="live-crisis"><div className="eyebrow">LIVE / SERVER-OWNED SYNTHETIC EXECUTION</div><h2>Live Crisis Lab</h2><p>Each action executes a native backend command. Refresh reloads persisted state. Recorded playback remains below.</p>
 <label>Live scenario<select aria-label="Live scenario" value={scenario} onChange={e=>setScenario(e.target.value)}>{Object.entries(registry).map(([k,v])=><option key={k} value={k}>{String(v)}</option>)}</select></label><button disabled={busy} onClick={()=>act('INITIALIZE')}>Initialize live scenario</button>
 {error&&<p role="alert" data-testid="live-error">{error}</p>}
 {data&&<><p>Session <span data-testid="live-session">{data.session.id}</span> · seed {data.session.seed} · created {data.session.created_at}</p>
 <div className="search">{[['ADVANCE','Advance one step'],['OUTAGE','Inject destination outage'],['LOSE_ACK','Inject lost acknowledgment'],['RESTORE','Restore participant'],['RETRY','Retry payment'],['RECOVER','Recover payment'],['AUDIT','Run financial audit'],['REPLAY','Replay durable history']].map(([a,label])=><button key={a} disabled={busy} onClick={()=>act(a)}>{label}</button>)}</div>
 <h3 data-testid="live-stage">{data.session.stage}</h3><p data-testid="live-fault">Fault: {data.session.fault}</p><p data-testid="live-finality">Source final: {String(payment?.source_final??'N/A')} · Destination final: {String(payment?.destination_final??'N/A')}</p>
 <p data-testid="live-obligation">Outstanding obligation: {payment?.obligation?.amount_minor??0} minor units</p>
 <p>Recovery: {data.session.recovery} · event head {data.event_head} · selected rail {data.route_decision?.bound_rail||data.route_decision?.evaluation.selected||payment?.rail||'Not started'}</p>
 <h3>Network and verified scope</h3><p>{data.banks.join(' · ')}</p><p>Currencies: {data.currencies.join(' / ')}</p><p>{data.supported_corridors.join(' · ')}</p>
 <h3>Account liquidity · integer minor units by currency</h3><table><thead><tr><th>Account</th><th>Posted</th><th>Reserved</th><th>Available</th></tr></thead><tbody>{Object.entries(data.financial.positions).filter(([k,v])=>{const p=v as Data;return p.posted||p.reserved||k.includes('MERIDIAN_SG');}).map(([k,v])=>{const p=v as Data;return <tr key={k}><td>{k}</td><td>{p.posted}</td><td>{p.reserved}</td><td>{p.posted-p.reserved-p.buffer}</td></tr>;})}</tbody></table>
 <h3>Payment and route inspector</h3><pre data-testid="live-payment">{JSON.stringify({payment,route:data.route_decision,reservations:data.financial.reservations,journals:data.financial.journals,correspondent:data.financial.correspondent},null,2)}</pre>
 <h3>Executed verification</h3><p data-testid="live-audit">{audit?(audit.native.financial_errors===0&&audit.independent.pass?'PASS':'FAIL'):'Audit not run for current state'}</p><pre>{JSON.stringify(audit,null,2)}</pre><p data-testid="live-replay">{data.session.replay?(data.session.replay.match?'PASS':'FAIL'):'Replay not run for current state'}</p><pre>{JSON.stringify(data.session.replay,null,2)}</pre>
 <h3>Persisted event history · last 100 events</h3><pre data-testid="live-events">{JSON.stringify(data.events,null,2)}</pre></>}
 </section>;
}
