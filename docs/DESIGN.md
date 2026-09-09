# Interface specification

Reference: [design-concept.png](design-concept.png), 1536 × 1024. It is a generated design reference, not a screenshot of shipped functionality.

Theme: quiet personal workshop. Warm off-white canvas, forest-green actions, sage selection, thin horizontal rules, editorial serif headings, system sans-serif controls. No remote fonts, image dependencies, charts, fake counters, or simulated online services.

Tokens: canvas `#faf9f6`; sidebar `#f0f1eb`; ink `#23352b`; muted `#6c7573`; action `#285640`; border `#d8dcd5`; panel `#e7ebe2`. Spacing multiples: 8, 12, 16, 24, 32, 48. Buttons use 8px corners. Project list is a ruled list, not a card grid.

Primary copy: unforge; Your projects; How ownership works; On this computer; New project; A home for what you build.; Your files. Your history. Your next idea.; Project; Last saved; Yours, all the way down.; Projects are ordinary folders with Git history. Keep working without an account or a cloud service.; Explore ownership; Local workspace · Optional Codex assistance.

Intentional deviations: the reference has fictional project rows; actual installation starts empty and displays only real projects. No fake macOS window dots in the browser UI. The ownership page and project editor/history/export views extend the same tokens and component families because they are required for the working lifecycle. Small screens use a compact top navigation rather than a fixed left rail.

Core path: create starter → try static preview → edit text → write file → inspect changes → save version → restore an earlier clean version → export committed history. Agent request creation writes a portable Markdown handoff. The separate Ask Codex action invokes an installed CLI in a disposable checkout, presents the proposal, and applies it only after review.


## Final visual verification

The interface was exercised through Codex Browser/IAB using real disposable local projects. The screenshots below show working controls and data, not a rendered mockup. Installation starts empty. Native viewport screenshots were used after raw CDP capture produced a tiled capture artifact; no product image was edited to hide a layout issue.

Evidence: [desktop.png](desktop.png) at 1536 × 1024, [mobile.png](mobile.png) at 390 × 844, and [mobile-editor.png](mobile-editor.png) at 390 × 844. The editor screenshot is a scrolled viewport. Both the generated reference and final screenshots were opened together with `view_image` for direct inspection. Home and editor document width remained 390 pixels in the mobile check. Temporary viewport overrides were reset.

| Comparison | Reference and rendered evidence | Resolution |
| --- | --- | --- |
| Layout | A 300px left rail, left-aligned main heading, right-aligned creation action, and ruled project rows. | Preserved. The third real example row and import action push the ownership section lower; lists grow with actual project count. |
| Copy | Main heading, supporting sentence, project labels, ownership heading, and primary actions match the reference. | The above-the-fold copy diff adds “Make it comfortable” for explicit display preferences and the working bundle import action. Status now says optional Codex assistance to describe the implemented integration. These are functional additions required by the expanded brief. |
| Typography | Editorial serif heading and project names with readable sans-serif controls. | Georgia and local system fonts implement the reference's type roles without remote font requests. Platform rendering and generated lettering are not pixel-identical. |
| Palette | Warm paper canvas, sage sidebar/selection, dark green text and actions, light rules. | Preserved through shared tokens, without image overlays or gradients. |
| Containers and icons | Open ruled list and one ownership band; outlined U mark, folder/book navigation, and directional controls. | Preserved. Shared SVG icons keep control geometry consistent; row arrows indicate opening a project. No artificial OS window controls. |
| Responsive behavior | Desktop reference supplies the visual system; small-screen continuation is required by the app. | Navigation becomes a compact top section. The heading wraps naturally; project timestamps are hidden, file tabs scroll internally, and the page has no horizontal overflow in exercised views. |

The final implementation was visually verified as faithful to the reference's design system with the documented functional additions. No unresolved clipping or horizontal-overflow defect was observed. The reference and implementation are not claimed to be pixel-identical.

Verified interactions: create → edit → save → restore → export/import, draft conflict handling, read-only metadata, display preferences, and simulated Codex start → review → apply → save. Separate live CLI integration evidence is recorded in [VALIDATION.md](VALIDATION.md).
