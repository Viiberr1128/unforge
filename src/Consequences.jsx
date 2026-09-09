export default function Consequences({ report, compact = false }) {
  if (!report) return null;
  const proposed = report.scope === 'proposal';
  return <section className="consequences">
    <h2>{proposed ? 'What comes with this change?' : 'What comes with this project?'}</h2>
    <p>{report.summary}</p>
    <p className="note">Read from source files. Connected accounts, actual usage, prices, and deployment settings still need verification.</p>
    {!!report.dependencies?.added?.length && <div className="impact-line"><strong>Added dependencies</strong><ul>{report.dependencies.added.map(item => <li key={`${item.path}:${item.name}`}><code>{item.name}</code> <span className="quiet">in {item.path}</span></li>)}</ul></div>}
    {!!report.dependencies?.removed?.length && <div className="impact-line"><strong>Removed dependencies</strong><ul>{report.dependencies.removed.map(item => <li key={`${item.path}:${item.name}`}><code>{item.name}</code> <span className="quiet">in {item.path}</span></li>)}</ul></div>}
    {!!report.newServices?.length && <p className="notice">New service references: {report.newServices.map(item => item.name).join(', ')}. Check their purpose, ongoing cost, and data needs before applying.</p>}
    <div className="care-rows">{report.chores?.map(item => <article key={item.id}><h3>{item.title}</h3><p>{item.reason}</p>{!!item.alternatives?.length && <p className="next-step">Consider: {item.alternatives.join(' ')}</p>}{!!item.evidence?.length && <details><summary>Where this came from</summary><ul>{item.evidence.map((entry, index) => <li key={index}><code>{entry.path}:{entry.line}</code></li>)}</ul></details>}</article>)}</div>
    {!report.chores?.length && <p>No supported maintenance patterns were detected in the inspected files. This does not prove the project has no operating costs or chores.</p>}
    {!compact && <details><summary>Inspection limits & architecture choices</summary>{report.limits?.map((item, index) => <p className="note" key={index}>{item}</p>)}<div className="architecture-list">{report.architectures?.map(item => <article key={item.id}><h3>{item.title}</h3><p>{item.fit}</p><p>{item.tradeoff}</p><p>{item.cost}</p></article>)}</div></details>}
  </section>;
}
