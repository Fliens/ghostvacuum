/* Exercise target resolution without making device or planning requests. */
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync('addon/ghostvacuum/dashboard/observation.js', 'utf8');
const start = source.indexOf('  function appendPlanDrop(');
const end = source.indexOf('  async function finishDrop(', start);
assert.ok(start >= 0 && end > start);
const context = vm.createContext({
  draggedRoom: 'kitchen', dropTarget: {target:'stale'},
  currentModel: {rooms:[{id:'kitchen',group:'rest'}]},
  clearDrop() {context.dropTarget=null;},
});
vm.runInContext(source.slice(start,end),context);
let highlighted = false;
const area = dataset => ({dataset,querySelectorAll:()=>[],querySelector:()=>({classList:{add:()=>{highlighted=true;}}})});
const parent = area({group:'planned'});
for (const target of [null,'planned_manual','planned_auto']) {
  highlighted=false;
  const child=target?area({dropTarget:target}):null;
  context.element={closest:selector=>selector==='[data-drop-target]'?child:parent};
  vm.runInContext('locateDrop(element,100)',context);
  assert.equal(context.dropTarget?.target??null,target);
  assert.equal(highlighted,target!==null);
}
for(const target of ['rest','active']) {
  context.element={closest:selector=>selector==='[data-group]'?area({group:target}):null};
  vm.runInContext('locateDrop(element,100)',context);
  assert.equal(context.dropTarget.target,target);
}
console.log('Drop targets: planning parent rejected, manual/automatic/rest/active accepted');

const disabled={dataset:{room:'disabled'},getBoundingClientRect:()=>({top:200})};
context.currentModel.rooms.push({id:'disabled',enabled:false,group:'rest'});
const rest=area({group:'rest'});
rest.querySelectorAll=()=>[disabled];
context.element={closest:selector=>selector==='[data-group]'?rest:null};
vm.runInContext('locateDrop(element,210)',context);
assert.equal(context.dropTarget,null,'disabled area must not accept a room');
vm.runInContext('locateDrop(element,190)',context);
assert.equal(context.dropTarget.target,'rest','area above disabled rooms remains usable');
console.log('Disabled room area rejected');
