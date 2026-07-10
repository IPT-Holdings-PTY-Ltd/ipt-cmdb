/* Operational attention queue for renewals and vendor end-of-life milestones. */
(() => {
  const watchDays = 90;
  const midnight = value => new Date(`${value}T00:00:00`);
  const daysUntil = value => Math.ceil((midnight(value) - new Date(new Date().toDateString())) / 86400000);
  const entries = () => currentAssets.flatMap(asset => {
    const meta = asset.metadata || {};
    if (["retired", "disposed"].includes(meta.lifecycle)) return [];
    return [["Renewal", meta.renewalDate], ["End of life", meta.endOfLifeDate]]
      .filter(([, date]) => date && daysUntil(date) <= watchDays)
      .map(([kind, date]) => ({asset, kind, date, days: daysUntil(date), owner: meta.technicalOwner || meta.serviceOwner || "No owner recorded"}));
  }).sort((left, right) => left.days - right.days || left.asset.name.localeCompare(right.asset.name));
  const dueText = entry => entry.days < 0 ? `${Math.abs(entry.days)} days overdue` : entry.days === 0 ? "Due today" : `${entry.days} days remaining`;
  const renderAttention = () => {
    const items = entries(), renewals = items.filter(item => item.kind === "Renewal"), eol = items.filter(item => item.kind === "End of life");
    metrics.innerHTML = `<article><b>${currentAssets.length}</b><span>CMDB records</span></article><article><b>${new Set(currentAssets.map(x => x.type)).size}</b><span>Record types</span></article><article><b>${renewals.length}</b><span>Renewals due in ${watchDays} days</span></article><article><b>${eol.length}</b><span>End-of-life risks</span></article>`;
    attentionQueue.innerHTML = items.length ? items.map(item => `<div class="attention-item ${item.days < 0 ? "overdue" : "upcoming"}"><span class="attention-kind">${esc(item.kind)}</span><div><b>${esc(item.asset.name)}</b><small>${esc(item.asset.type)} · owner: ${esc(item.owner)}</small></div><div class="attention-date">${esc(item.date)}<small>${esc(dueText(item))}</small></div></div>`).join("") : '<p class="ci-empty">No renewals or end-of-life milestones are due in the next 90 days.</p>';
  };
  const baseLoadAssets = loadAssets;
  loadAssets = async () => { await baseLoadAssets(); renderAttention(); };
})();
