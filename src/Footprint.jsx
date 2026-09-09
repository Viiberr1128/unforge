import { useEffect, useState } from 'react';
import { api } from './api.js';

export default function Footprint({ id }) {
  const [report, setReport] = useState(null);
  const [error, setError] = useState('');
  useEffect(() => {
    let active = true;
    api(`/projects/${id}/insights`).then(value => { if (active) setReport(value); }).catch(e => {if (active) setError(e.message);});
    return () => { active = false; };
  }, [id]);
  if (!report) return <p role={error ? 'alert' : 'status'}>{error || 'Reading your project’s configuration…'}</p>;
  return <section className="footprint"><h2>Know what your software depends on.</h2><p>Local configuration clues, explained in plain language. A reference to a service does not prove it is connected or costing money.</p>
    <div className="service-list">{report.services.length ? report.services.map(service => <article key={service.id}><h3>{service.name}</h3><p>{service.meaning}</p><p className="next-step">{service.nextStep}</p><details><summary>Where this was found</summary><ul>{service.evidence.map((e, index) => <li key={index}><code>{e.path}:{e.line}</code></li>)}</ul></details></article>) : <div className="ownership-banner"><div><h2>No supported service references found.</h2><p>This is a starting point, not a guarantee of zero bills. Services can exist outside the files this scan can read.</p></div></div>}</div>
    <h2 className="architecture-heading">Choose the smallest home that fits.</h2><p>Three ways to operate an application. These are tradeoffs to evaluate, not a deployment or a price quote.</p>
    <div className="architecture-list">{report.architectures.map(option => <article key={option.id}><h3>{option.title}</h3><p>{option.fit}</p><p><strong>Tradeoff:</strong> {option.tradeoff}</p><p><strong>Cost:</strong> {option.cost}</p></article>)}</div>
    <details><summary>What this scan can and cannot tell you</summary><ul>{report.limits.map((limit, i) => <li key={i}>{limit}</li>)}</ul><p>{report.workflowCount} workflow files; {report.scheduledWorkflows} with schedules in the inspected files.</p></details>
  </section>;
}
