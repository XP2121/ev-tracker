const test = require('node:test');
const assert = require('node:assert/strict');
const {settings,aggregate,monthlyRows,seasonalRows} = require('../app/static/range.js');
const d=(year,month,km,cons,cost=0)=>({t:new Date(year,month-1,1),km,kwh0:km*cons/100,cost});
test('default full and practical range without charging loss',()=>{
  const a=aggregate([d(2026,2,200,14,100)],settings());
  assert.ok(Math.abs(a.full-200)<1e-9);
  assert.ok(Math.abs(a.practical-140)<1e-9);
  assert.ok(Math.abs(a.cons-14)<1e-9);
  assert.equal(a.cost100,50);
});
test('weighted mean by distance, not by entry count',()=>{
  const a=aggregate([d(2026,4,300,14),d(2026,4,100,22)],settings());
  assert.equal(a.cons,16);
  assert.equal(a.full,175);
});
test('less than minimum driven distance is insufficient data',()=>{
  const a=aggregate([d(2026,2,20,14)],settings());
  assert.equal(a.km,20);
  assert.equal(a.full,null);
  assert.equal(a.practical,null);
  assert.equal(a.enough,false);
});
test('winter of 2026 spans December 2025 and Jan-Feb 2026',()=>{
  const M=[d(2025,12,100,18),d(2026,1,100,20),d(2026,2,100,18),d(2026,12,100,25),d(2025,1,100,11)];
  assert.equal(seasonalRows(M,2026,'winter').length,3);
  assert.equal(seasonalRows(M,2027,'winter').length,1);
  assert.equal(seasonalRows(M,'all','winter').length,5);
});
test('all-years monthly means aggregate same calendar month',()=>{
  const M=[d(2025,1,100,15),d(2026,1,300,17),d(2026,2,200,12)];
  assert.equal(monthlyRows(M,'all',1).length,2);
  assert.equal(aggregate(monthlyRows(M,'all',1),settings()).cons,16.5);
  assert.equal(monthlyRows(M,2025,1).length,1);
});
test('setting SOC bounds changes projection without altering full range',()=>{
  const s=settings({battery_kwh:27,range_soc_high:90,range_soc_low:20,range_min_km:0});
  const a=aggregate([d(2026,6,120,18)],s);
  assert.ok(Math.abs(a.full-150)<1e-9);
  assert.ok(Math.abs(a.practical-105)<1e-9);
});
