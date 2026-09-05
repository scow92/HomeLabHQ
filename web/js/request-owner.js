// A latest-request lifetime within an existing authenticated session. Owners
// are local to a route/view; session generations remain exclusively in api.js.
"use strict";
import { getSessionGeneration, isCurrentSession } from "./api.js";

export function requestOwner(parentSignal) {
  let active = null;
  function invalidate() {
    const previous = active;
    active = null; // invalidate before abort handlers can resume
    previous?.abort();
  }
  parentSignal?.addEventListener("abort", invalidate, { once: true });
  function begin(presentationCurrent = () => true) {
    invalidate();
    const controller = new AbortController();
    if (parentSignal?.aborted) controller.abort();
    const generation = getSessionGeneration();
    active = controller;
    return {
      signal: controller.signal,
      current: () => active === controller && !parentSignal?.aborted && isCurrentSession(generation) && presentationCurrent(),
    };
  }
  return { begin, invalidate };
}
