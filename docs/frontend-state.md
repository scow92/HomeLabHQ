# Frontend state ownership

Each feature module owns the state that drives its view. A module that needs
to perform work for another feature receives data and callbacks, or emits a
named UI event for router-level coordination; it does not import that feature's
mutable state or manipulate its DOM.

The client feature follows this boundary:

- `web/js/clients/store.js` owns the roster snapshot.
- `filters.js` owns filter and sort state.
- `api.js` contains client HTTP calls.
- `grid.js` renders the roster and emits user intents through callbacks.
- `actions.js` performs mutations supplied with a re-render callback.
- `index.js` is the only coordinator that combines the preceding modules.

The practical rule is: mutate the owning module's state, invoke that module's
render function, and never directly mutate another feature module's DOM.
