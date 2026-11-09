/* EV Tracker range calculations: battery energy and dashboard consumption only.
   No AC charging-loss factor is applied to driving range. */
(function(root) {
  'use strict';
  const DEFAULTS = Object.freeze({battery_kwh: 28, range_soc_high: 80, range_soc_low: 10, range_min_km: 100});
  const toNumber = (v, fallback) => v === undefined || v === null || v === '' || !Number.isFinite(Number(v)) ? fallback : Number(v);
  function settings(cfg = {}) {
    return {
      battery: toNumber(cfg.battery_kwh, DEFAULTS.battery_kwh),
      high: toNumber(cfg.range_soc_high, DEFAULTS.range_soc_high),
      low: toNumber(cfg.range_soc_low, DEFAULTS.range_soc_low),
      minKm: toNumber(cfg.range_min_km, DEFAULTS.range_min_km)
    };
  }
  function aggregate(rows, settingsValue) {
    const s = settingsValue || settings();
    const km = rows.reduce((a, x) => a + Number(x.km || 0), 0);
    const energy = rows.reduce((a, x) => a + Number(x.kwh0 || 0), 0);
    const cost = rows.reduce((a, x) => a + Number(x.cost || 0), 0);
    const enough = km >= s.minKm && km > 0 && energy > 0;
    const cons = enough ? energy / km * 100 : null;
    return {
      km, enough, cons,
      full: enough ? s.battery * 100 / cons : null,
      practical: enough ? s.battery * (s.high - s.low) / cons : null,
      cost100: enough ? cost / km * 100 : null
    };
  }
  function monthlyRows(monthly, selectedYear, month1Based) {
    return monthly.filter(r => r.t.getMonth() + 1 === month1Based &&
      (selectedYear === 'all' || r.t.getFullYear() === Number(selectedYear)));
  }
  function seasonalRows(monthly, selectedYear, season) {
    const months = {winter:[12,1,2], spring:[3,4,5], summer:[6,7,8], autumn:[9,10,11]}[season];
    if (!months) return [];
    if (selectedYear === 'all') return monthly.filter(r => months.includes(r.t.getMonth()+1));
    const year = Number(selectedYear);
    if (season === 'winter') return monthly.filter(r =>
      (r.t.getFullYear() === year - 1 && r.t.getMonth() === 11) ||
      (r.t.getFullYear() === year && r.t.getMonth() < 2));
    return monthly.filter(r => r.t.getFullYear() === year && months.includes(r.t.getMonth()+1));
  }
  root.EVRange = {DEFAULTS, settings, aggregate, monthlyRows, seasonalRows};
  if (typeof module !== 'undefined' && module.exports) module.exports = root.EVRange;
})(typeof globalThis !== 'undefined' ? globalThis : this);
