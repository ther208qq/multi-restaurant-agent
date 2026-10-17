/**
 * Human-readable labels for the Agent's evidence references.
 *
 * ``f0.e2`` is the stable handle the server uses to validate a citation: it
 * names the third evidence item of the first finding, regardless of how the
 * prose around it is worded. That makes it ideal for the Agent and useless for
 * a restaurant manager, who has no way to know which metric ``f0`` is.
 *
 * The findings that the refs point into are already part of the same diagnosis
 * payload, so the panel can resolve each ref to the labels the Service wrote --
 * ``订单量 › 本期订单量`` -- without asking the server for a second
 * representation. The raw ref is kept only as a link target and tooltip, so a
 * claim can still be traced back to its evidence without a code being printed
 * in the middle of a sentence.
 *
 * Everything here is pure, so the Node test runner can exercise it directly
 * (see ``frontend/tests/evidenceRefs.test.js``).
 */

/**
 * Dish findings carry no name field: the Service labels every one of them
 * "菜品销量" and puts the dish name inside the evidence labels, so two dish
 * cards would otherwise be headed with identical words. Lift the name back out.
 */
export function findingTitle(finding) {
  if (!finding) return '证据';
  if (finding.menu_item_id === null || finding.menu_item_id === undefined) {
    return finding.label || '证据';
  }
  const named = (finding.evidence || []).find((item) => /^「.+?」/.test(item.label || ''));
  const match = named && named.label.match(/^「(.+?)」/);
  return match ? `${finding.label}：${match[1]}` : finding.label || '证据';
}

/**
 * ``订单量 › 本期订单量`` for a store-level fact, or
 * ``菜品销量：招牌牛肉面 › 本期销量`` for a dish. The finding part says *what
 * was judged*; the evidence part says *which fact supported it*.
 */
function caption(finding, evidence) {
  const findingLabel = findingTitle(finding);
  let evidenceLabel = String(evidence.label || '').trim();
  if (finding.menu_item_id !== null && finding.menu_item_id !== undefined) {
    // The dish name is already in the finding label, so repeating it inside
    // the evidence part would read as "招牌牛肉面 › 「招牌牛肉面」本期销量".
    const quoted = evidenceLabel.match(/^「.+?」/);
    if (quoted) evidenceLabel = evidenceLabel.slice(quoted[0].length).trim();
  }
  return evidenceLabel ? `${findingLabel} › ${evidenceLabel}` : findingLabel;
}

/**
 * A ref -> readable citation map for one diagnosis payload.
 *
 * The numeric part is not parsed out of the string: each evidence item is
 * visited in payload order and registered under its own ``ref``, so the map
 * stays correct even if the server ever changes the naming scheme. Only a
 * payload missing ``ref`` falls back to the positional ``fN.eM`` form.
 */
export function buildEvidenceIndex(findings) {
  const index = new Map();
  (findings || []).forEach((finding, findingIndex) => {
    (finding.evidence || []).forEach((evidence, evidenceIndex) => {
      const ref = evidence.ref || `f${findingIndex}.e${evidenceIndex}`;
      if (!index.has(ref)) {
        index.set(ref, { finding, evidence, text: caption(finding, evidence) });
      }
    });
  });
  return index;
}

/**
 * The label to show for a claim's citation. An unresolvable ref falls back to
 * the raw ref rather than disappearing: the server drops invalid citations
 * before they reach here, so this only fires if the payload and the analysis
 * were assembled from different runs -- worth seeing, not hiding.
 */
export function describeEvidenceRef(ref, index) {
  const entry = index instanceof Map ? index.get(ref) : null;
  return entry ? entry.text : ref;
}

/** The DOM id an evidence row carries so a citation can link to it. */
export function evidenceAnchorId(ref) {
  return `evidence-${ref}`;
}
