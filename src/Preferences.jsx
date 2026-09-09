import { Icon } from './ui.jsx';

export default function Preferences({ options, update, back }) {
  return <>
    <header className="page-top"><button className="text-button" onClick={back}><Icon name="back"/>Your projects</button></header>
    <section className="intro"><h1>Make yourself comfortable.</h1><p>A few choices about how you want to work.</p></section>
    <section className="preferences">
      <label className="preference-row"><div><h2>A little more room to read</h2><p>Increase the size of explanations and file text.</p></div><input type="checkbox" checked={options.largeText} onChange={e => update({...options,largeText:e.target.checked})}/></label>
      <label className="preference-row"><div><h2>Show technical details</h2><p>Show version identifiers and project paths throughout the workspace. Your files and export controls are always available.</p></div><input type="checkbox" checked={options.technical} onChange={e => update({...options,technical:e.target.checked})}/></label>
      <p className="note">These display preferences stay in this browser. They do not change your project, infer your abilities, or affect what an agent is allowed to do.</p>
    </section><footer>You can change your mind whenever you like.</footer>
  </>;
}
