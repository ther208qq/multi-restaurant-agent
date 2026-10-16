/**
 * Query bookkeeping for the 经营诊断 panel.
 *
 * Three problems this exists to solve, and all three cost real model calls,
 * because every request to ``/staff/diagnosis`` for a branch that has findings
 * runs the Agent:
 *
 *   * the start and end date are two independent inputs, so adjusting one range
 *     by hand is two requests;
 *   * responses are not ordered -- an earlier, slower request can land after a
 *     newer one and overwrite it;
 *   * leaving the tab and coming back remounts the panel, which re-asks a
 *     question that was already answered.
 *
 * The rules live here, as plain functions and small objects, rather than inline
 * in the component, because the frontend has no test runner of its own: keeping
 * this file free of React and of ``api`` lets ``node --test`` exercise it
 * directly. The panel is then only wiring.
 *
 * Nothing here decides *what* a diagnosis says. It decides when to ask, which
 * answer is allowed to win, and how long an answer stays worth keeping.
 */

/** How long to wait after the last change before asking. */
export const DEBOUNCE_MS = 400;

/**
 * A window that has already ended describes orders that are not going to
 * change any more, so its answer can be kept for a while.
 */
export const CLOSED_WINDOW_TTL_MS = 30 * 60 * 1000;

/**
 * A window that includes today is still being written to -- every new order
 * moves the totals -- so its answer goes stale quickly and is kept only briefly.
 */
export const OPEN_WINDOW_TTL_MS = 60 * 1000;

/**
 * Upper bound on retained entries. A session that clicks through many ranges
 * would otherwise grow the map without limit; entries expire anyway, so this
 * only bounds a long session.
 */
export const MAX_CACHE_ENTRIES = 50;

/**
 * What identifies the caller. Diagnosis answers are a function of the branch and
 * the window, so two staff members asking about the same branch would otherwise
 * share an entry -- but the module-level cache survives a logout that happens
 * without a page reload, and a branch-scoped staff member is not allowed to read
 * some branches at all. Folding the caller's identity into the key means a
 * shared entry can never be read across that boundary, whatever the service
 * layer does with it later.
 */
export function staffScopeId(staff) {
  if (!staff) return 'anonymous';
  const who = staff.id ?? staff.username ?? 'unknown';
  return `${who}#${staff.branch_id ?? 'all'}`;
}

/**
 * The cache key: every parameter that changes the answer, plus the caller.
 */
export function cacheKey({ scopeId, branchId, startDate, endDate }) {
  return [scopeId, branchId ?? 'all', startDate ?? '', endDate ?? ''].join('|');
}

/**
 * TTL for a window ending on ``endDate``, with today given as an ISO date string
 * -- callers pass the same date the panel sends to the API, so the two agree on
 * what "today" means even at a timezone boundary.
 */
export function cacheTtlMs(endDate, todayIso) {
  if (endDate && endDate < todayIso) return CLOSED_WINDOW_TTL_MS;
  return OPEN_WINDOW_TTL_MS;
}

/**
 * Whether a response is worth keeping. A refusal (``insufficient_data``) and a
 * clean window are both *answers*, not failures, and are cached like any other;
 * what is never cached is a shape we do not recognise, which includes anything
 * a failed request left behind.
 */
export function isCacheable(result) {
  return Boolean(
    result &&
      typeof result === 'object' &&
      typeof result.status === 'string' &&
      Array.isArray(result.findings)
  );
}

/**
 * A small expiring store. ``read`` takes a clock so tests can move time without
 * waiting for it.
 */
export function createDiagnosisCache({ maxEntries = MAX_CACHE_ENTRIES } = {}) {
  const entries = new Map();

  function prune(now) {
    for (const [key, entry] of entries) {
      if (entry.expiresAt <= now) entries.delete(key);
    }
    // Still full: Map iterates in insertion order, so the first key is the
    // oldest one written.
    while (entries.size > maxEntries) {
      entries.delete(entries.keys().next().value);
    }
  }

  return {
    /** The stored value, or ``undefined`` when absent or expired. */
    read(key, now = Date.now()) {
      const entry = entries.get(key);
      if (entry === undefined) return undefined;
      if (entry.expiresAt <= now) {
        entries.delete(key);
        return undefined;
      }
      return entry.value;
    },

    /** Always overwrites, so a forced refresh replaces what was there. */
    write(key, value, ttlMs, now = Date.now()) {
      entries.set(key, { value, expiresAt: now + ttlMs });
      prune(now);
    },

    clear() {
      entries.clear();
    },

    get size() {
      return entries.size;
    },
  };
}

/**
 * Hands out an increasing ticket per query. A response may only be applied when
 * its ticket is still the newest one, which is what stops a slow earlier request
 * from overwriting a newer result.
 */
export function createRequestGuard() {
  let latest = 0;
  return {
    begin() {
      latest += 1;
      return latest;
    },
    isCurrent(ticket) {
      return ticket === latest;
    },
    /** Retire everything in flight -- used when the panel goes away. */
    invalidate() {
      latest += 1;
    },
  };
}

/**
 * A trailing-edge debouncer: each ``schedule`` restarts the wait, so a burst of
 * changes produces one call. Timers are injectable so tests need no clock.
 *
 * Note what this cannot do: once a call has been made, cancelling the timer does
 * not cancel the model run behind it. Debouncing prevents calls; it does not
 * undo them.
 */
export function createDebouncedScheduler(
  delayMs,
  { setTimer = setTimeout, clearTimer = clearTimeout } = {}
) {
  let timer = null;
  return {
    schedule(fn) {
      if (timer !== null) clearTimer(timer);
      timer = setTimer(() => {
        timer = null;
        fn();
      }, delayMs);
    },
    cancel() {
      if (timer !== null) {
        clearTimer(timer);
        timer = null;
      }
    },
    get pending() {
      return timer !== null;
    },
  };
}
