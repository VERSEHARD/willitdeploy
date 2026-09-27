const BASE='/pricepulse';
function track(event_type,meta={}){
  fetch(BASE+'/api/product-event',{
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({event_type,meta}),
    keepalive:true
  }).catch(()=>{});
}
document.querySelectorAll('.lp-pilot-link').forEach((link,index)=>{
  link.addEventListener('click',()=>track('pilot_click',{placement:index,copy:link.textContent.trim().slice(0,80)}));
});
document.querySelectorAll('.lp-workspace-link').forEach((link,index)=>{
  link.addEventListener('click',()=>track('workspace_open',{source:'landing',placement:index}));
});
