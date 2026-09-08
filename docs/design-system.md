# Shared UI foundations

The three stylesheets keep their established base → components → views order.

| Role | Contract |
|---|---|
| `--accent`, `--green`, `--amber`, `--red` | Chart lines, status fills and decorative borders |
| `--accent-text`, `--green-text`, `--amber-text`, `--red-text` | Small semantic text on page, raised surfaces and corresponding tinted badges |
| `--action-fill`, `--success-fill`, `--accent-ink` | Filled controls and their contrasting labels |
| `--control-border`, `--focus` | Interactive boundaries and visible keyboard focus, distinct from decorative `--border` |
| `--target-min`, `--target-touch` | 24px minimum native disclosure target and 44px preferred touch target |
| Existing `--font`, `--radius`, surface tokens | Retained typography, radius and surface foundation |

`field`/`fieldError` retain visible captions and associate help/validation.
`radioChoice` uses native radio behavior. `disclosureState` connects native
buttons to their controlled regions. The modal stack owns focus, inert state and
scroll locking; consumers do not independently change body overflow.

Text pair coverage and distinct control contrast checks are in
`e2e/contrast.spec.mjs`; exact captured ratios are in
[the M03 evidence](ui-review/m03-contrast.json). Responsive breakpoints remain
the existing component rules; this tranche does not introduce a second breakpoint
system or rename spacing/type scales unrelated to a finding. Native radio/button
controls remain usable without any new runtime dependency.

The final coarse-pointer rule in `views.css` owns text-field sizing after all
feature rules: 16px text and at least 44px height for inputs/selects/textareas,
excluding native checkbox/radio/range/color controls. Small secondary actions
use `--target-min`; frequent coarse-pointer buttons use `--target-touch` without
enlarging their icons. Modal headers wrap full resource identities and actions.
The independent AP binding toggle shows a grey dot for unbound clients, green
for clients bound here and amber for clients bound to another AP, alongside its
text label. The full button remains a 44px target. It stays focusable while
pending, exposes `aria-disabled`/`aria-busy`, and rejects repeated activation. Computed sizing and
keyboard coverage are in `e2e/touch-sizing.spec.mjs`; physical-device and actual
browser-zoom acceptance remain open under M04.

Device cards support a deliberate 450ms touch hold followed by dragging. Moving
more than 10px or scrolling before the hold cancels it, so normal taps and page
scrolling remain available. The card body suppresses the native touch callout
and text selection; action controls retain the existing styling and 44px coarse
pointer targets. Long device titles wrap within their header without covering
controls. No global button/field compaction or refresh-status hiding is applied.

Native desktop drag and explicit Up/Down buttons remain available. A focused
card also supports Alt+ArrowUp/ArrowDown and Enter/Space for details. Keyboard
reordering retains focus on the activated control, or on its card if that control
becomes disabled at the boundary. Each completed changed order saves once;
unchanged holds and cancelled gestures do not save. A short post-drag activation
guard also protects action buttons. Escape, pointer interruption, route/filter
changes, account disposal and page hiding cancel the gesture and remove its
transient listeners/timer. Native drag's pointer handoff and normal touch capture
release are distinguished from interruption. The existing API persists order;
no new ordering store is introduced.

These are retained local behaviours formalised after PR #50, with accessibility
and lifecycle corrections, not newly selected refactor-plan findings. The
[retained hunk audit](ui-review/device-touch.json) records included and omitted
changes and the limits of the earlier physical iPhone Safari preview acceptance.
