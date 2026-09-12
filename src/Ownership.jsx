import { Icon } from './ui.jsx';

export default function Ownership({ back }) {
  return <>
    <header className="page-top"><button className="text-button" onClick={back}><Icon name="back"/>Your projects</button><span className="quiet">The ownership promise</span></header>
    <section className="intro"><h1>Leave whenever you like.</h1><p>Your software should outlive the tool you used to build it.</p></section>
    <div className="ownership-details">
      <article><span className="number">01</span><div><h2>Ordinary files. A complete history.</h2><p>Your projects live in folders on your computer. Saved versions are Git commits. Open them with your editor, work with another agent, or use Git directly. Unforge does not need to be running.</p></div></article>
      <article><span className="number">02</span><div><h2>Take your saved work with you.</h2><p>Export a project as a Git bundle. It contains committed files, requests, and version history. Another computer with Git can open it without an Unforge account.</p><code>git clone your-project.bundle my-project</code><p className="note">A bundle excludes unsaved work and is not a backup of external databases, credentials, or large-file storage. Keep a separate backup on another device or service.</p></div></article>
      <article><span className="number">03</span><div><h2>No invisible cloud bill.</h2><p>The core workspace runs on your computer without hosting or an AI account. Installation downloads open-source dependencies. Optional Codex work uses your configured account only when you start a job.</p><p className="note">Your computer uses power and storage. Codex jobs use your account’s limits or charges; their time limit is not a hard dollar cap. Unforge does not provision hosting.</p></div></article>
      <article><span className="number">04</span><div><h2>An honest starting point.</h2><p>This alpha creates projects, imports supported Git bundles, edits text, previews a static page, saves versions, restores clean projects, and exports history. It can run an installed Codex CLI in a separate project copy and bring back a reviewed proposal. Request files are also available as portable handoffs.</p><p>Hosted deployment, direct repository import, and peer synchronization are not implemented yet. GitHub can host Unforge’s source, but it is not required to run it.</p></div></article>
    </div>
    <footer>Built for humans. Owned by you. <span className="quiet">A local workspace. Ask an AI to help if you want.</span></footer>
  </>;
}
