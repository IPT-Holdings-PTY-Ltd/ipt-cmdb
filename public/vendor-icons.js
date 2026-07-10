/* Local product marks are derived from the recorded vendor/model first, with safe generic fallbacks. */
(() => {
  let hoverCard;
  const productMark = asset => {
    const metadata = asset.metadata || {};
    const signal = `${metadata.vendor || ""} ${metadata.model || ""} ${asset.name || ""}`.toLowerCase();
    if (/veeam/.test(signal)) return {code:"V", label:"Veeam", className:"veeam", product:true};
    if (/postgres|sql/.test(signal)) return {code:"SQL", label:"PostgreSQL", className:"postgres", product:true};
    if (/microsoft|windows/.test(signal)) return {code:"MS", label:"Microsoft", className:"microsoft", product:true};
    if (/fortinet|fortigate/.test(signal)) return {code:"F", label:"Fortinet", className:"fortinet", product:true};
    if (/vmware|vsphere/.test(signal)) return {code:"VM", label:"VMware", className:"vmware", product:true};
    if (asset.type.toLowerCase().includes("network")) return {code:"NET", label:"Network device", className:"network", product:false};
    if (asset.type.toLowerCase().includes("server")) return {code:"SRV", label:"Server", className:"compute", product:false};
    if (asset.type.toLowerCase().includes("workstation")) return {code:"PC", label:"Workstation", className:"compute", product:false};
    return {code:"APP", label:asset.type, className:"generic", product:false};
  };
  const assetOwner = asset => { const metadata = asset.metadata || {}; return metadata.technicalOwner || metadata.serviceOwner || "No owner recorded"; };
  const assetStatus = asset => { const metadata = asset.metadata || {}; return `${metadata.operationalStatus || "unknown"} · ${metadata.criticality || "medium"} criticality`; };
  function ensureHoverCard(){if(hoverCard)return;hoverCard=document.createElement("section");hoverCard.id="ciHoverCard";hoverCard.className="ci-hover-card";hoverCard.setAttribute("role","tooltip");hoverCard.hidden=true;ciMap.append(hoverCard)}
  function showHover(node,asset){ensureHoverCard();const mark=productMark(asset),metadata=asset.metadata||{},mapBox=ciMap.getBoundingClientRect(),nodeBox=node.getBoundingClientRect(),links=currentRelationships.filter(link=>link.fromId===asset.id||link.toId===asset.id).length,impact=Math.max(0,impactFrom(asset.id).length-1),upstream=Math.max(0,upstreamOf(asset.id).length-1);hoverCard.innerHTML=`<div class="ci-hover-heading"><span class="ci-vendor-mark ${mark.className}">${esc(mark.code)}</span><div><b>${esc(asset.name)}</b><small>${esc(mark.label)} · ${esc(asset.type)}</small></div></div><dl><div><dt>Status</dt><dd>${esc(assetStatus(asset))}</dd></div><div><dt>Owner</dt><dd>${esc(assetOwner(asset))}</dd></div><div><dt>Source</dt><dd>${esc(asset.source || "manual")}${asset.lastSeen ? ` · seen ${esc(asset.lastSeen.slice(0,10))}` : ""}</dd></div><div><dt>Impact</dt><dd>${upstream} upstream · ${impact} downstream · ${links} direct links</dd></div>${metadata.site ? `<div><dt>Site</dt><dd>${esc(metadata.site)}</dd></div>` : ""}</dl><p>Click to inspect and pin this asset.</p>`;const desiredLeft=nodeBox.right-mapBox.left+12,desiredTop=nodeBox.top-mapBox.top-6;hoverCard.hidden=false;const maxLeft=Math.max(12,ciMap.clientWidth-hoverCard.offsetWidth-12),maxTop=Math.max(12,ciMap.clientHeight-hoverCard.offsetHeight-12);hoverCard.style.left=`${Math.min(desiredLeft,maxLeft)}px`;hoverCard.style.top=`${Math.min(Math.max(desiredTop,12),maxTop)}px`}
  function hideHover(){if(hoverCard)hoverCard.hidden=true}
  function decorateVendorMarks(){ciMap?.querySelectorAll(".ci-node").forEach(node=>{const asset=currentAssets.find(item=>item.id===node.dataset.ciId);if(!asset)return;const mark=productMark(asset),header=node.querySelector(".ci-header");node.classList.toggle("has-product-mark",mark.product);if(!node.querySelector(".ci-vendor-mark"))header.insertAdjacentHTML("afterbegin",`<span class="ci-vendor-mark ${mark.className}" aria-label="${esc(mark.label)}">${esc(mark.code)}</span>`);node.addEventListener("pointerenter",()=>showHover(node,asset));node.addEventListener("pointerleave",hideHover);node.addEventListener("focus",()=>showHover(node,asset));node.addEventListener("blur",hideHover)})}
  const baseRenderCiMap=renderCiMap;renderCiMap=()=>{baseRenderCiMap();requestAnimationFrame(decorateVendorMarks)};
  const baseShowPage=showPage;showPage=id=>{baseShowPage(id);if(id==="relationships")requestAnimationFrame(decorateVendorMarks)};
})();
