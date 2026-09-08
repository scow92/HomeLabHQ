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
