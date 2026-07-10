/* Root/MSP aggregate view: the API enforces the same company scope as all other reads. */
(() => {
  const isRootRole = () => ["platform_admin", "msp_operator"].includes(me?.role);
  const dueText = item => item.days < 0 ? `${Math.abs(item.days)} days overdue` : item.days === 0 ? "Due today" : `${item.days} days remaining`;
  const renderRootAttention = (items, customers) => {
    const renewals = items.filter(item => item.kind === "Renewal"), eol = items.filter(item => item.kind === "End of life"), overdue = items.filter(item => item.days < 0);
    rootMetrics.innerHTML = `<article><b>${customers.length}</b><span>Managed customers</span></article><article><b>${overdue.length}</b><span>Overdue</span></article><article><b>${renewals.length}</b><span>Renewals in 90 days</span></article><article><b>${eol.length}</b><span>End-of-life risks</span></article>`;
    customerEstate.innerHTML = customers.map(customer => `<button class="customer-card" type="button" onclick="openCustomerWorkspace('${customer.companyId}')"><h3>${esc(customer.companyName)}</h3><p>${customer.assetCount} ${customer.assetCount === 1 ? "asset" : "assets"} · ${customer.criticalCount} critical</p><div class="estate-counts"><span>${customer.attentionCount} attention</span><span class="estate-open">Open workspace →</span>${customer.overdueCount?`<span class="estate-overdue">${customer.overdueCount} overdue</span>`:''}</div></button>`).join("") || '<p class="ci-empty">No managed customers.</p>';
    rootAttention.innerHTML = items.length ? items.map(item => `<div class="attention-item ${item.days < 0 ? "overdue" : "upcoming"}"><span class="attention-kind">${esc(item.kind)}</span><div><b>${esc(item.assetName)}</b><small>${esc(item.companyName)} · ${esc(item.assetType)} · ${esc(item.criticality)} · owner: ${esc(item.owner)}</small></div><div class="attention-date">${esc(item.date)}<small>${esc(dueText(item))}</small></div></div>`).join("") : '<p class="ci-empty">No customer renewals or end-of-life milestones are due in the next 90 days.</p>';
  };
  const loadRootAttention = async () => { if (isRootRole()) { const [items, customers] = await Promise.all([api("/api/root-attention"), api("/api/root-overview")]); renderRootAttention(items, customers); } };
  window.loadRootAttention = loadRootAttention;
  window.openCustomerWorkspace = async companyId => { currentCompany = companyId; company.value = companyId; await switchCompany(); };
  const syncRootAdminNavigation = () => {
    const rootContext = isRootRole() && selected() === "__root__";
    document.querySelector('[data-page="msp-overview"]').hidden = true;
    document.querySelector('[data-page="branding"]').hidden = !rootContext;
    document.querySelector('[data-page="database"]').hidden = !(rootContext && me?.role === "platform_admin");
    document.querySelector('[data-page="integrations"]').hidden = !rootContext;
  };
  window.syncRootAdminNavigation = syncRootAdminNavigation;
  const baseShowApp = showApp;
  showApp = user => { baseShowApp(user); syncRootAdminNavigation(); };
  const baseShowPage = showPage;
  showPage = id => { baseShowPage(id); if (id === "msp-overview") { pageTitle.textContent = "MSP overview"; crumb.textContent = "MSP / ALL CUSTOMERS"; } };
})();
