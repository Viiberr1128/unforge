import { Icon } from './ui.jsx';
import { useEffect, useState } from 'react';
import { api } from './api.js';
import { providerSummary } from './agentWorkState.js';

export default function Preferences({ options, update, back }) {
  const [build, setBuild] = useState(null), [agent, setAgent] = useState(null);
  useEffect(() => { api('/build').then(setBuild).catch(() => {}); api('/agent/status').then(setAgent).catch(() => {}); }, []);
  return <>
    <header className="page-top"><button className="text-button" onClick={back}><Icon name="back"/>Your projects</button></header>
    <section className="intro"><h1>Preferences</h1></section>
    <section className="preferences">
      <label className="preference-row"><h2>Larger text</h2><input type="checkbox" checked={options.largeText} onChange={e => update({...options,largeText:e.target.checked})}/></label>
      <label className="preference-row"><h2>Show technical details</h2><input type="checkbox" checked={options.technical} onChange={e => update({...options,technical:e.target.checked})}/></label>
      {build && <section><h2>Installed build</h2><p>Unforge {build.version} · <code>{build.sourceRevision?.slice(0, 12) || 'Revision unavailable'}</code>{build.sourceDirty ? ' · Uncommitted source' : ''}</p><p>{build.signing} signed · {build.notarized === true ? 'notarized' : build.notarized === false ? 'not notarized' : 'notarization recorded in release receipt'}</p><details><summary>Capabilities</summary><p>{build.capabilities?.join(', ')}</p></details></section>}
      {agent && <section><h2>AI work</h2><p>{(agent.anyAvailable ?? agent.available) ? 'Ready' : 'Not ready'}</p><details><summary>Check connections</summary>{(agent.providers || []).map(provider => <div key={provider.id}><p>{provider.label}: {providerSummary(provider)}</p>{provider.lanes?.length > 0 && <ul>{provider.lanes.map(lane => <li key={lane.id}>{lane.id}: {lane.state}</li>)}</ul>}</div>)}{options.technical && <pre>{JSON.stringify({retention:agent.retention,historyErrors:agent.historyErrors},null,2)}</pre>}</details></section>}
    </section>
  </>;
}
