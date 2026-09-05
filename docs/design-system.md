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
