# Design informed by real friction

These patterns come from the conversation context, accessible task summaries, and selected prior task messages available while shaping Unforge. They are not an exhaustive review of anyone's history or evidence that every user has the same needs. Examples are generalized; no private project records or billing details are reproduced here.

| Recurring difficulty | Design response | Evidence the response helps |
| --- | --- | --- |
| A small tool accumulates unexpected service costs. | Start locally and expose recognized dependencies before suggesting infrastructure. | The person can explain which services are necessary and which facts remain unknown. |
| Development vocabulary gets between a person and their goal. | Use concrete actions such as save a version and restore, with Git details available underneath. | A beginner can complete a useful change without a vocabulary lesson. |
| An agent conversation ends and the original intention is lost. | Store a request as a portable project file. | A different authorized agent can read the request without the original chat. |
| Repeated checks waste time and compete for local resources. | Keep checks explicit and focused; introduce shared execution coordination only when implemented. | Required checks finish without duplicate heavy jobs. The alpha does not claim an execution queue. |
| Nobody can tell whether a change is saved, tested, or live. | Keep these states distinct and report only observed evidence. | A saved commit is never presented as a deployed application. |
| Experimentation risks losing a version that worked. | Preserve ordinary Git history and make restoration a new recorded change. | Earlier commits remain available after a restore. |
| A green check is mistaken for proof that the feature works. | Describe the scope of a check and retain room for functional validation. | Release notes distinguish automated checks from exercised behavior and unresolved limitations. |
| Controls crowd the content or an input gets trapped behind another panel. | Use compact mobile navigation, scrollable file tabs, and focused dialogs. | The person can type, scroll, and finish the task at a narrow viewport. |
| Setup works once but cannot be reproduced elsewhere. | Ship a built interface, document runtime requirements, and verify an extracted release. | A fresh installation starts without depending on the original developer's files. |

The intended result is less technical work for the owner, not another layer of ceremony. Each added capability should simplify an actual task, explain its limits, and preserve the person's ability to leave.
