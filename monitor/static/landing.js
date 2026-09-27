const BASE='/pricepulse';
function track(event_type,meta={}){
  fetch(BASE+'/api/product-event',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({event_type,meta}),keepalive:true}).catch(()=>{});
}
document.querySelectorAll('.lp-pilot-link').forEach(link=>{
  link.addEventListener('click',()=>track('pilot_click',{placement:link.textContent.trim()}));
});
const workspace=document.querySelector('a[href="/pricepulse/app"]');
if(workspace) workspace.addEventListener('click',()=>track('workspace_open',{source:'landing'}));
