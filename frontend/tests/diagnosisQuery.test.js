/**
 * Unit tests for the 经营诊断 panel's query rules.
 *
 * These run on Node's built-in test runner -- ``npm test`` in ``frontend/`` --
 * because the frontend has no test framework: there is no vitest, jest or
 * testing-library in package.json, and adding one to test ~150 lines of pure
 * logic would be a much heavier change than the logic itself. Everything the
 * panel delegates to lives in ``diagnosisQuery.js`` with no React and no
 * ``api`` import, so it can be exercised here directly.
 *
 * What this file therefore does *not* cover: that the React component wires
 * these pieces up correctly (that the effect actually debounces, that unmount
 * really calls invalidate). Those are verified against a real browser by
 * counting the requests the page makes -- see the report accompanying this
 * change -- rather than in jsdom.
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import {
  CLOSED_WINDOW_TTL_MS,
  DEBOUNCE_MS,
  MAX_CACHE_ENTRIES,
  OPEN_WINDOW_TTL_MS,
  cacheKey,
  cacheTtlMs,
  createDebouncedScheduler,
  createDiagnosisCache,
  createRequestGuard,
  isCacheable,
  staffScopeId,
} from '../src/components/admin/diagnosisQuery.js';

/** A clock we control, so debounce tests do not have to sleep. */
function fakeTimers() {
  let nextId = 1;
  const timers = new Map();
  return {
    setTimer(fn, delay) {
      const id = nextId++;
      timers.set(id, { fn, delay });
      return id;
    },
    clearTimer(id) {
      timers.delete(id);
    },
    get pending() {
      return timers.size;
    },
    fire() {
      const due = [...timers.values()];
      timers.clear();
      for (const timer of due) timer.fn();
    },
  };
}

const aResult = (overrides = {}) => ({
  status: 'ok',
  findings: [{ metric: 'order_count' }],
  notes: [],
  ...overrides,
});

// --------------------------------------------------------------------------
// 防抖
// --------------------------------------------------------------------------

test('一次连续调整只产生一次调用', () => {
  const timers = fakeTimers();
  const scheduler = createDebouncedScheduler(DEBOUNCE_MS, timers);
  let calls = 0;

  // Six changes in a burst -- what the two date inputs actually produce.
  for (let i = 0; i < 6; i += 1) scheduler.schedule(() => { calls += 1; });

  assert.equal(timers.pending, 1, '后一次应当取消前一次的定时器');
  timers.fire();
  assert.equal(calls, 1);
});

test('一直在改就一直不发', () => {
  const timers = fakeTimers();
  const scheduler = createDebouncedScheduler(DEBOUNCE_MS, timers);
  let calls = 0;

  for (let i = 0; i < 5; i += 1) {
    scheduler.schedule(() => { calls += 1; });
    assert.equal(timers.pending, 1);
    assert.equal(calls, 0, '最后一次改动之前不应该有任何调用');
  }
});

test('取消之后不再触发（组件卸载 / 依赖变化走这条路）', () => {
  const timers = fakeTimers();
  const scheduler = createDebouncedScheduler(DEBOUNCE_MS, timers);
  let calls = 0;

  scheduler.schedule(() => { calls += 1; });
  assert.equal(scheduler.pending, true);
  scheduler.cancel();

  assert.equal(scheduler.pending, false);
  assert.equal(timers.pending, 0);
  timers.fire();
  assert.equal(calls, 0);
});

test('触发之后可以再次调度', () => {
  const timers = fakeTimers();
  const scheduler = createDebouncedScheduler(DEBOUNCE_MS, timers);
  let calls = 0;

  scheduler.schedule(() => { calls += 1; });
  timers.fire();
  scheduler.schedule(() => { calls += 1; });
  timers.fire();

  assert.equal(calls, 2);
  assert.equal(scheduler.pending, false, '触发后不应残留定时器');
});

// --------------------------------------------------------------------------
// 过期响应
// --------------------------------------------------------------------------

test('只有最新的请求能落地', () => {
  const guard = createRequestGuard();
  const slow = guard.begin();
  const fast = guard.begin();

  // 后发的先回来，先发的后回来 —— 正是线上看到的那次错乱。
  assert.equal(guard.isCurrent(fast), true);
  assert.equal(guard.isCurrent(slow), false, '较旧的响应必须被丢弃');
});

test('卸载后所有在途请求都作废', () => {
  const guard = createRequestGuard();
  const inFlight = guard.begin();

  guard.invalidate();

  assert.equal(guard.isCurrent(inFlight), false);
});

test('缓存命中也要推进序号，否则在途请求会盖掉它', () => {
  const guard = createRequestGuard();
  const cache = createDiagnosisCache();
  const key = 'k';

  const inFlight = guard.begin(); // 一个慢请求正在路上
  cache.write(key, aResult(), OPEN_WINDOW_TTL_MS);

  // 面板选中了一个已经有缓存的区间：同步取出，同时必须作废在途的那个。
  const ticket = guard.begin();
  assert.equal(cache.read(key) !== undefined, true);
  assert.equal(guard.isCurrent(ticket), true);
  assert.equal(guard.isCurrent(inFlight), false, '缓存的答案赢了，在途的不能翻盘');
});

// --------------------------------------------------------------------------
// 缓存
// --------------------------------------------------------------------------

test('命中返回原值，未命中返回 undefined', () => {
  const cache = createDiagnosisCache();
  const value = aResult();

  assert.equal(cache.read('miss'), undefined);
  cache.write('hit', value, OPEN_WINDOW_TTL_MS);
  assert.equal(cache.read('hit'), value);
});

test('过期条目读不到，且会被清掉', () => {
  const cache = createDiagnosisCache();
  cache.write('k', aResult(), 1000, 0);

  assert.notEqual(cache.read('k', 999), undefined);
  assert.equal(cache.read('k', 1000), undefined, 'TTL 到点即失效');
  assert.equal(cache.size, 0, '读到时顺手删除');
});

test('写入会覆盖，刷新拿到的就是新的', () => {
  const cache = createDiagnosisCache();
  const first = aResult({ status: 'insufficient_data', findings: [] });
  const second = aResult();

  cache.write('k', first, OPEN_WINDOW_TTL_MS);
  cache.write('k', second, OPEN_WINDOW_TTL_MS);

  assert.equal(cache.read('k'), second);
  assert.equal(cache.size, 1);
});

test('已结束的区间留得久，含今天的区间留得短', () => {
  assert.equal(cacheTtlMs('2026-10-15', '2026-10-16'), CLOSED_WINDOW_TTL_MS);
  assert.equal(cacheTtlMs('2026-10-16', '2026-10-16'), OPEN_WINDOW_TTL_MS);
  assert.equal(cacheTtlMs('2026-10-20', '2026-10-16'), OPEN_WINDOW_TTL_MS);
});

test('条目数有上限，写入时清掉过期的', () => {
  const cache = createDiagnosisCache({ maxEntries: 3 });
  for (let i = 0; i < 3; i += 1) cache.write(`k${i}`, aResult(), 1000, 0);

  // 时间越过 TTL 后再写一条：过期的先被清掉，不会撑爆。
  cache.write('fresh', aResult(), 1000, 5000);

  assert.equal(cache.size, 1);
  assert.equal(cache.read('fresh', 5000) !== undefined, true);
});

test('超过上限且没有过期条目时，淘汰最旧的一条', () => {
  const cache = createDiagnosisCache({ maxEntries: 3 });
  for (let i = 0; i < 5; i += 1) cache.write(`k${i}`, aResult(), OPEN_WINDOW_TTL_MS);

  assert.equal(cache.size, 3);
  assert.equal(cache.read('k0'), undefined);
  assert.equal(cache.read('k4') !== undefined, true);
});

// --------------------------------------------------------------------------
// 失败不入缓存
// --------------------------------------------------------------------------

test('完整响应才可缓存', () => {
  assert.equal(isCacheable(aResult()), true);
  assert.equal(isCacheable(aResult({ status: 'insufficient_data', findings: [] })), true);
});

test('残缺或失败的结果不入缓存', () => {
  assert.equal(isCacheable(null), false);
  assert.equal(isCacheable(undefined), false);
  assert.equal(isCacheable(''), false);
  assert.equal(isCacheable({ detail: '门店 #999 不存在。' }), false, '错误体没有 status');
  assert.equal(isCacheable({ status: 'ok' }), false, '缺 findings 视为不完整');
  assert.equal(isCacheable({ status: 'ok', findings: null }), false);
});

// --------------------------------------------------------------------------
// 键与身份隔离
// --------------------------------------------------------------------------

test('键包含门店与两个日期', () => {
  const base = { scopeId: 's', branchId: 1, startDate: '2026-10-10', endDate: '2026-10-16' };
  assert.equal(cacheKey(base), 's|1|2026-10-10|2026-10-16');
  assert.notEqual(cacheKey(base), cacheKey({ ...base, branchId: 2 }));
  assert.notEqual(cacheKey(base), cacheKey({ ...base, startDate: '2026-10-11' }));
  assert.notEqual(cacheKey(base), cacheKey({ ...base, endDate: '2026-10-15' }));
});

test('不同身份不会共用条目', () => {
  const admin = staffScopeId({ id: 1, branch_id: null });
  const scoped = staffScopeId({ id: 2, branch_id: 1 });

  assert.notEqual(admin, scoped);
  assert.equal(staffScopeId(null), 'anonymous');

  const window = { branchId: 1, startDate: '2026-10-10', endDate: '2026-10-16' };
  const cache = createDiagnosisCache();
  cache.write(cacheKey({ scopeId: admin, ...window }), aResult(), OPEN_WINDOW_TTL_MS);

  assert.equal(cache.read(cacheKey({ scopeId: scoped, ...window })), undefined);
});

test('同一个员工换门店口径后也是另一个身份', () => {
  assert.notEqual(
    staffScopeId({ id: 7, branch_id: null }),
    staffScopeId({ id: 7, branch_id: 3 })
  );
});
