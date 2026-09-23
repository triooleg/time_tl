const test = require('node:test');
const assert = require('node:assert/strict');

const {storageMetric} = require('../../src/time_tl/admin/static/admin.js');

test('storage metrics are rendered as used and total GiB with percent', () => {
  const metric = {value: [665719931, 1653562409, 40.2], unit: 'bytes'};
  assert.equal(storageMetric('RAM', metric), 'RAM: 0.62 / 1.54 ГБ (40.2%)');
});

test('storage metrics calculate a percent when the backend omits it', () => {
  const metric = {value: [536870912, 1073741824], unit: 'bytes'};
  assert.equal(storageMetric('Disk', metric), 'Disk: 0.50 / 1.00 ГБ (50.0%)');
});

test('storage metrics keep a clear fallback for missing or invalid data', () => {
  assert.equal(storageMetric('RAM', null), 'RAM: Метрики недоступні');
  assert.equal(storageMetric('Disk', {value: ['invalid', 1073741824, 0]}), 'Disk: Метрики недоступні');
  assert.equal(storageMetric('Disk', {value: [0, 0, 0]}), 'Disk: Метрики недоступні');
});
