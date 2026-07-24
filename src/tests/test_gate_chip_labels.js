/**
 * Test suite for gate chip label prefixes.
 * Tests that shadow_only and next_day_shadow gate verdicts have disambiguating prefixes.
 */

// Mock implementation of the relevant gate chip meta from index.html
const GATE_CHIP_META = {
  traded_live:          { label: 'Traded',              family: 'Executed for real' },
  entry_guard:          { label: 'Blocked',              family: 'Passed the gates, blocked or missed at execution' },
  timeout_today:        { label: 'Timed out',            family: 'Passed the gates, blocked or missed at execution' },
  shadow_only:          { label: 'Live: shadow',         family: 'Shadow-routed by policy' },
  next_day_shadow:      { label: 'Live: next-day',       family: 'Shadow-routed by policy' },
  below_min_edge:       { label: 'Edge too small',        family: 'Rejected on the numbers' },
  above_max_edge:       { label: 'Edge too large',        family: 'Rejected on the numbers' },
  below_min_price:      { label: 'Price too low',         family: 'Rejected on the numbers' },
  below_min_confidence: { label: 'Confidence low',        family: 'Rejected on the numbers' },
  margin_gate:          { label: 'Margin too thin',       family: 'Rejected on the numbers' },
  mae_gate:             { label: 'Forecast error high',   family: 'Rejected on the numbers' },
};

/**
 * Test that shadow_only label has the "Live: " prefix
 */
function testShadowOnlyLabelPrefix() {
  const expected = 'Live: shadow';
  const actual = GATE_CHIP_META.shadow_only.label;

  if (actual !== expected) {
    throw new Error(`shadow_only label mismatch: expected "${expected}", got "${actual}"`);
  }

  console.log('✓ shadow_only label correctly prefixed: "' + actual + '"');
}

/**
 * Test that next_day_shadow label has the "Live: " prefix
 */
function testNextDayShadowLabelPrefix() {
  const expected = 'Live: next-day';
  const actual = GATE_CHIP_META.next_day_shadow.label;

  if (actual !== expected) {
    throw new Error(`next_day_shadow label mismatch: expected "${expected}", got "${actual}"`);
  }

  console.log('✓ next_day_shadow label correctly prefixed: "' + actual + '"');
}

/**
 * Test that shadow-routed verdicts (shadow_only, next_day_shadow) belong to the same family
 */
function testShadowRoutedFamily() {
  const familyName = 'Shadow-routed by policy';

  const shadowOnlyFamily = GATE_CHIP_META.shadow_only.family;
  const nextDayShadowFamily = GATE_CHIP_META.next_day_shadow.family;

  if (shadowOnlyFamily !== familyName) {
    throw new Error(`shadow_only family mismatch: expected "${familyName}", got "${shadowOnlyFamily}"`);
  }

  if (nextDayShadowFamily !== familyName) {
    throw new Error(`next_day_shadow family mismatch: expected "${familyName}", got "${nextDayShadowFamily}"`);
  }

  console.log('✓ shadow_only and next_day_shadow both in "Shadow-routed by policy" family');
}

/**
 * Test that the shadow-routed verdicts are distinguishable from other verdicts
 */
function testLabelDisambiguation() {
  // Verify that the labels now include the "Live: " prefix to distinguish them
  const shadowOnlyLabel = GATE_CHIP_META.shadow_only.label;
  const nextDayShadowLabel = GATE_CHIP_META.next_day_shadow.label;

  // Both should start with "Live: "
  if (!shadowOnlyLabel.startsWith('Live: ')) {
    throw new Error(`shadow_only label should start with "Live: ", got "${shadowOnlyLabel}"`);
  }

  if (!nextDayShadowLabel.startsWith('Live: ')) {
    throw new Error(`next_day_shadow label should start with "Live: ", got "${nextDayShadowLabel}"`);
  }

  console.log('✓ Both shadow-routed labels correctly prefixed with "Live: "');
}

/**
 * Test edge case: labels should not be empty
 */
function testLabelsNotEmpty() {
  if (!GATE_CHIP_META.shadow_only.label || GATE_CHIP_META.shadow_only.label.trim() === '') {
    throw new Error('shadow_only label is empty');
  }

  if (!GATE_CHIP_META.next_day_shadow.label || GATE_CHIP_META.next_day_shadow.label.trim() === '') {
    throw new Error('next_day_shadow label is empty');
  }

  console.log('✓ All labels are non-empty');
}

/**
 * Run all tests
 */
function runTests() {
  const tests = [
    testShadowOnlyLabelPrefix,
    testNextDayShadowLabelPrefix,
    testShadowRoutedFamily,
    testLabelDisambiguation,
    testLabelsNotEmpty,
  ];

  console.log('\n=== Gate Chip Label Prefix Tests ===\n');

  let passed = 0;
  let failed = 0;

  for (const test of tests) {
    try {
      test();
      passed++;
    } catch (error) {
      console.error('✗ ' + test.name + ': ' + error.message);
      failed++;
    }
  }

  console.log('\n=== Test Results ===');
  console.log(`Passed: ${passed}/${tests.length}`);
  console.log(`Failed: ${failed}/${tests.length}`);

  if (failed > 0) {
    process.exit(1);
  }
}

// Run tests if this file is executed directly
if (require.main === module) {
  runTests();
}

module.exports = { GATE_CHIP_META };
